"""Extract authenticated Mercado Livre cookies from Brave into Playwright storage_state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_ML_DOMAINS: tuple[str, ...] = (
    "mercadolivre.com.br",
    "mercadolibre.com",
    "mercadopago.com.br",
)

_VALID_SAME_SITE = {"Strict", "Lax", "None"}


def _normalize_same_site(value: Any) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        mapping = {
            "strict": "Strict",
            "lax": "Lax",
            "none": "None",
            "no_restriction": "None",
            "unspecified": "Lax",
        }
        if normalized in mapping:
            return mapping[normalized]
    return "Lax"


def _normalize_expires(value: Any) -> float:
    if value is None:
        return -1
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return -1
    return numeric if numeric > 0 else -1


def to_playwright_cookies(raw_cookies: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert raw cookie dicts (from rookiepy or similar) into Playwright cookie dicts."""
    playwright_cookies: list[dict[str, Any]] = []
    for cookie in raw_cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        domain = cookie.get("domain")
        if not name or value is None or not domain:
            continue

        playwright_cookies.append(
            {
                "name": str(name),
                "value": str(value),
                "domain": str(domain),
                "path": str(cookie.get("path") or "/"),
                "expires": _normalize_expires(cookie.get("expires", cookie.get("expirationDate"))),
                "httpOnly": bool(cookie.get("http_only", cookie.get("httpOnly", False))),
                "secure": bool(cookie.get("secure", False)),
                "sameSite": _normalize_same_site(cookie.get("same_site", cookie.get("sameSite"))),
            }
        )
    return playwright_cookies


def find_brave_cookie_dbs(home_dir: Path | None = None) -> list[Path]:
    """Locate Brave Cookies SQLite files across Windows, Linux native, Flatpak, and Snap installs."""
    home = home_dir or Path.home()
    base_dirs = (
        home / "AppData" / "Local" / "BraveSoftware" / "Brave-Browser" / "User Data",
        home / ".config" / "BraveSoftware" / "Brave-Browser",
        home / ".var" / "app" / "com.brave.Browser" / "config" / "BraveSoftware" / "Brave-Browser",
        home / "snap" / "brave" / "current" / ".config" / "BraveSoftware" / "Brave-Browser",
    )
    found: list[Path] = []
    for base in base_dirs:
        if not base.exists():
            continue
        for pattern in ("*/Network/Cookies", "*/Cookies"):
            for cookies_path in sorted(base.glob(pattern)):
                if cookies_path.is_file() and cookies_path not in found:
                    found.append(cookies_path)
    return found


def _find_local_state(cookies_db: Path) -> str:
    """Locate Chromium 'Local State' file relative to a Cookies SQLite path."""
    for parent in cookies_db.parents:
        candidate = parent / "Local State"
        if candidate.is_file():
            return str(candidate)
    return str(cookies_db.parent / "Local State")


def load_brave_cookies(
    domains: Sequence[str] = DEFAULT_ML_DOMAINS,
    cookies_db: Path | None = None,
) -> list[dict[str, Any]]:
    """Read Brave cookies for the given domains using rookiepy."""
    try:
        import rookiepy  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "Dependência 'rookiepy' não encontrada. Instale com: pip install rookiepy"
        ) from exc

    domain_list = list(domains)
    if cookies_db is not None:
        if not cookies_db.exists():
            raise FileNotFoundError(f"Arquivo de cookies não encontrado: {cookies_db}")
        key_path = _find_local_state(cookies_db)
        raw = rookiepy.chromium_based(key_path, str(cookies_db), domain_list)
        return to_playwright_cookies(raw)

    try:
        raw = rookiepy.brave(domain_list)
        return to_playwright_cookies(raw)
    except RuntimeError as exc:
        err_msg = str(exc)
        if "appbound encryption" in err_msg.lower() or "running as admin" in err_msg.lower():
            raise RuntimeError(
                "No Windows com Brave/Chromium v130+, a criptografia App-Bound exige "
                "executar o terminal como Administrador para 'sync-cookies', OU usar "
                "'ml-automation login' (login interativo) / 'ml-automation import-cookies'."
            ) from exc
        candidates = find_brave_cookie_dbs()
        for candidate in candidates:
            key_path = _find_local_state(candidate)
            raw = rookiepy.chromium_based(key_path, str(candidate), domain_list)
            cookies = to_playwright_cookies(raw)
            if cookies:
                return cookies
        if candidates:
            return []
        raise RuntimeError(
            "Arquivo 'Cookies' do Brave não encontrado nos caminhos padrão "
            "(Windows, Linux nativo, Flatpak ou Snap). "
            "Passe manualmente via: ml-automation sync-cookies --cookies-db <caminho>"
        ) from exc


def export_brave_storage_state(
    output_path: Path = Path(".state/storage_state.json"),
    domains: Sequence[str] = DEFAULT_ML_DOMAINS,
    cookies_db: Path | None = None,
) -> Path:
    """Extract Brave cookies and write a Playwright-compatible storage_state.json."""
    cookies = load_brave_cookies(domains=domains, cookies_db=cookies_db)
    if not cookies:
        raise RuntimeError(
            "Nenhum cookie do Mercado Livre encontrado no Brave. "
            "Verifique se você está autenticado no navegador Brave."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    storage_state = {"cookies": cookies, "origins": []}
    output_path.write_text(json.dumps(storage_state, indent=2), encoding="utf-8")
    return output_path


def import_cookies_from_json(
    input_path: Path,
    output_path: Path = Path(".state/storage_state.json"),
) -> Path:
    """Convert exported browser cookies JSON (e.g. Cookie-Editor) to Playwright storage_state."""
    if not input_path.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {input_path}")

    raw_data = json.loads(input_path.read_text(encoding="utf-8"))
    raw_cookies = raw_data.get("cookies", []) if isinstance(raw_data, dict) else raw_data
    cookies = to_playwright_cookies(raw_cookies)
    if not cookies:
        raise RuntimeError(f"Nenhum cookie válido encontrado em: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    storage_state = {"cookies": cookies, "origins": []}
    output_path.write_text(json.dumps(storage_state, indent=2), encoding="utf-8")
    return output_path
