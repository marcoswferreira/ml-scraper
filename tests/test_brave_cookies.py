"""Unit tests for Brave cookie normalization and extraction to Playwright storage_state."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from ml_automation.auth.brave_cookies import (
    _find_local_state,
    export_brave_storage_state,
    find_brave_cookie_dbs,
    import_cookies_from_json,
    load_brave_cookies,
    to_playwright_cookies,
)
from ml_automation.cli import app


def test_import_cookies_from_json_supports_cookie_editor_format(tmp_path: Path) -> None:
    exported = [
        {
            "name": "orguseridp",
            "value": "999",
            "domain": ".mercadolivre.com.br",
            "path": "/",
            "expirationDate": 1800000000.5,
            "httpOnly": True,
            "secure": True,
            "sameSite": "no_restriction",
        }
    ]
    input_path = tmp_path / "brave_export.json"
    input_path.write_text(json.dumps(exported), encoding="utf-8")

    output_path = tmp_path / "storage_state.json"
    saved = import_cookies_from_json(input_path=input_path, output_path=output_path)

    state = json.loads(saved.read_text(encoding="utf-8"))
    assert len(state["cookies"]) == 1
    assert state["cookies"][0]["sameSite"] == "None"
    assert state["cookies"][0]["expires"] == 1800000000.5

    with pytest.raises(FileNotFoundError):
        import_cookies_from_json(input_path=tmp_path / "missing.json")

    empty_path = tmp_path / "empty.json"
    empty_path.write_text("[]", encoding="utf-8")
    with pytest.raises(RuntimeError):
        import_cookies_from_json(input_path=empty_path)


def test_find_brave_cookie_dbs_and_local_state(tmp_path: Path) -> None:
    flatpak_cookies = (
        tmp_path
        / ".var"
        / "app"
        / "com.brave.Browser"
        / "config"
        / "BraveSoftware"
        / "Brave-Browser"
        / "Profile 1"
        / "Cookies"
    )
    flatpak_cookies.parent.mkdir(parents=True)
    flatpak_cookies.write_bytes(b"")

    user_data = tmp_path / "AppData" / "Local" / "BraveSoftware" / "Brave-Browser" / "User Data"
    win_cookies = user_data / "Default" / "Network" / "Cookies"
    win_cookies.parent.mkdir(parents=True)
    win_cookies.write_bytes(b"")
    local_state = user_data / "Local State"
    local_state.write_text("{}", encoding="utf-8")

    found = find_brave_cookie_dbs(home_dir=tmp_path)
    assert found == [win_cookies, flatpak_cookies]
    assert _find_local_state(win_cookies) == str(local_state)
    assert "Local State" in _find_local_state(flatpak_cookies)


def test_to_playwright_cookies_normalizes_fields() -> None:
    raw = [
        {
            "name": "ssid",
            "value": "abc123",
            "domain": ".mercadolivre.com.br",
            "path": "/",
            "expires": 1800000000,
            "http_only": True,
            "secure": True,
            "same_site": "strict",
        },
        {
            "name": "session_only",
            "value": "xyz",
            "domain": "mercadolivre.com.br",
            "expires": "not-a-number",
            "same_site": "invalid_value",
        },
        {
            "value": "ignored",
            "domain": ".mercadolivre.com.br",
        },
    ]

    result = to_playwright_cookies(raw)
    assert len(result) == 2
    assert result[0]["sameSite"] == "Strict"
    assert result[1]["expires"] == -1
    assert result[1]["sameSite"] == "Lax"


def test_load_and_export_brave_cookies_with_mock_rookiepy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample_cookie = [{"name": "ssid", "value": "val123", "domain": ".mercadolivre.com.br"}]
    fake_rookiepy = SimpleNamespace(
        brave=lambda domains: sample_cookie,
        chromium_based=lambda key_path, db_path, domains: sample_cookie,
    )
    monkeypatch.setitem(sys.modules, "rookiepy", fake_rookiepy)

    # 1. Direct brave() call via CLI sync-cookies
    out_state = tmp_path / "state.json"
    runner = CliRunner()
    res = runner.invoke(app, ["sync-cookies", "--output", str(out_state)])
    assert res.exit_code == 0
    assert out_state.exists()

    # 2. Explicit cookies_db path
    db_file = tmp_path / "Cookies"
    db_file.write_bytes(b"")
    cookies = load_brave_cookies(cookies_db=db_file)
    assert len(cookies) == 1

    with pytest.raises(FileNotFoundError):
        load_brave_cookies(cookies_db=tmp_path / "nonexistent_db")

    # 3. App-Bound encryption error handling
    def _raise_appbound(domains: Any) -> Any:
        raise RuntimeError(
            "Chrome cookies from version v130 can be decrypted only when running as admin due to appbound encryption!"
        )

    fake_rookiepy.brave = _raise_appbound
    with pytest.raises(RuntimeError, match="App-Bound"):
        export_brave_storage_state(output_path=out_state)
