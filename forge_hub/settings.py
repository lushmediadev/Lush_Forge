"""Configuration for the Forge gateway; deployment values come from environment."""

from dataclasses import dataclass
from pathlib import Path
import os
from urllib.parse import urlsplit


WORKER_IDS = ("forge1", "forge2")


def _worker_url(name: str, default: str) -> str:
    value = os.environ.get(name, default).rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "http" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"{name} must be an HTTP origin without credentials")
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError(f"{name} must not include path, query, or fragment")
    return value


@dataclass(frozen=True)
class Settings:
    database_path: Path
    public_origin: str
    secure_cookie: bool
    worker_urls: dict[str, str]
    worker_keys: dict[str, str]


def load_settings() -> Settings:
    database_path = Path(os.environ.get("FORGE_HUB_DB_PATH", "./data/forgehub.sqlite3")).resolve()
    public_origin = os.environ.get("FORGE_HUB_PUBLIC_ORIGIN", "https://ai.lushmedia.net").rstrip("/")
    parsed = urlsplit(public_origin)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path:
        raise ValueError("FORGE_HUB_PUBLIC_ORIGIN must be a bare http(s) origin")
    secure_cookie = os.environ.get("FORGE_HUB_SECURE_COOKIE", "1") != "0"
    return Settings(
        database_path=database_path,
        public_origin=public_origin,
        secure_cookie=secure_cookie,
        worker_urls={
            "forge1": _worker_url("FORGE_HUB_FORGE1_URL", "http://127.0.0.1:18386"),
            "forge2": _worker_url("FORGE_HUB_FORGE2_URL", "http://127.0.0.1:18387"),
        },
        worker_keys={
            "forge1": os.environ.get("FORGE_HUB_FORGE1_KEY", ""),
            "forge2": os.environ.get("FORGE_HUB_FORGE2_KEY", ""),
        },
    )
