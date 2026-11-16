"""Unit tests for Brave cookie normalization to Playwright storage_state."""

import json
from pathlib import Path

from ml_automation.auth.brave_cookies import (
    find_brave_cookie_dbs,
    import_cookies_from_json,
    to_playwright_cookies,
)


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


def test_find_brave_cookie_dbs_discovers_flatpak_and_profiles(tmp_path: Path) -> None:
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

    win_cookies = (
        tmp_path
        / "AppData"
        / "Local"
        / "BraveSoftware"
        / "Brave-Browser"
        / "User Data"
        / "Default"
        / "Network"
        / "Cookies"
    )
    win_cookies.parent.mkdir(parents=True)
    win_cookies.write_bytes(b"")

    found = find_brave_cookie_dbs(home_dir=tmp_path)
    assert found == [win_cookies, flatpak_cookies]


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
            "expires": None,
            "same_site": "invalid_value",
        },
        {
            # Missing name should be skipped
            "value": "ignored",
            "domain": ".mercadolivre.com.br",
        },
    ]

    result = to_playwright_cookies(raw)
    assert len(result) == 2

    first, second = result
    assert first == {
        "name": "ssid",
        "value": "abc123",
        "domain": ".mercadolivre.com.br",
        "path": "/",
        "expires": 1800000000.0,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Strict",
    }
    assert second["expires"] == -1
    assert second["sameSite"] == "Lax"
    assert second["httpOnly"] is False
