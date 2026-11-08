"""Settings, from command-line arguments, environment variables or a .env file.

Command-line arguments win over environment variables, which win over the .env file
(``./.env`` by default). Empty values count as unset, so a launcher can pass
``OPENAUDIBLE_PASSWORD=""`` without it meaning "no password".
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

DEFAULT_URL = "http://localhost:8000"
DEFAULT_DOWNLOAD_DIR = "~/Downloads/OpenAudible"
DEFAULT_MP3_BITRATE = "96k"


class ConfigError(RuntimeError):
    pass


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    # An unfilled launcher placeholder such as "${user_config.password}" is not a value.
    if value.startswith("${") and value.endswith("}"):
        return default
    return value or default


def load_env_file(path: Path) -> list[str]:
    """Put OPENAUDIBLE_* values from a dotenv file into os.environ, unless already set.

    Returns the names it set.
    """
    loaded = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key.startswith("OPENAUDIBLE_") and not _env(key):
            os.environ[key] = value
            loaded.append(key)
    return loaded


@dataclass
class Settings:
    url: str = field(default_factory=lambda: _env("OPENAUDIBLE_URL", DEFAULT_URL))
    username: str = field(default_factory=lambda: _env("OPENAUDIBLE_USERNAME"), repr=False)
    password: str = field(default_factory=lambda: _env("OPENAUDIBLE_PASSWORD"), repr=False)
    download_dir: Path = field(
        default_factory=lambda: Path(_env("OPENAUDIBLE_DOWNLOAD_DIR", DEFAULT_DOWNLOAD_DIR)).expanduser()
    )
    mp3_bitrate: str = field(default_factory=lambda: _env("OPENAUDIBLE_MP3_BITRATE", DEFAULT_MP3_BITRATE))

    def __post_init__(self) -> None:
        self.url = self.url.rstrip("/")
        if "://" not in self.url:
            self.url = "http://" + self.url

    def credentials(self) -> tuple[str, str] | None:
        """(username, password) for HTTP Basic auth, or None when no username is set."""
        if not self.username:
            if self.password:
                raise ConfigError("OPENAUDIBLE_PASSWORD is set but OPENAUDIBLE_USERNAME is not")
            return None
        return self.username, self.password

    def apply_args(self, argv: Sequence[str] | None = None) -> "Settings":
        """Override fields from command-line arguments."""
        p = argparse.ArgumentParser(
            prog="openaudible-mcp",
            description="MCP server for an OpenAudible library. Every option can also be set "
            "with the environment variable shown.",
        )
        p.add_argument("--url", help=f"OpenAudible web server URL (OPENAUDIBLE_URL, default {DEFAULT_URL})")
        p.add_argument("--username", help="web server user (OPENAUDIBLE_USERNAME)")
        p.add_argument(
            "--password",
            help="web server password (OPENAUDIBLE_PASSWORD). Command-line arguments are visible "
            "to other local users; prefer the variable or --password-file",
        )
        p.add_argument("--password-file", type=Path, help="read the password from this file")
        p.add_argument(
            "--env-file",
            type=Path,
            help="dotenv file with OPENAUDIBLE_* settings (OPENAUDIBLE_ENV_FILE, default ./.env if present)",
        )
        p.add_argument("--download-dir", help=f"where audio is saved (OPENAUDIBLE_DOWNLOAD_DIR, default {DEFAULT_DOWNLOAD_DIR})")
        p.add_argument("--mp3-bitrate", help=f"bitrate for local MP3s (OPENAUDIBLE_MP3_BITRATE, default {DEFAULT_MP3_BITRATE})")
        a = p.parse_args(argv)
        env_file = a.env_file or (Path(_env("OPENAUDIBLE_ENV_FILE")) if _env("OPENAUDIBLE_ENV_FILE") else None)
        if env_file or Path(".env").is_file():
            path = (env_file or Path(".env")).expanduser()
            try:
                if load_env_file(path):
                    fresh = Settings()
                    for name in ("url", "username", "password", "download_dir", "mp3_bitrate"):
                        setattr(self, name, getattr(fresh, name))
            except OSError as e:
                p.error(f"cannot read env file {path}: {e}")
        if a.url:
            self.url = a.url
        if a.username:
            self.username = a.username
        if a.password:
            self.password = a.password
        if a.password_file:
            try:
                self.password = a.password_file.expanduser().read_text().strip()
            except OSError as e:
                p.error(f"cannot read --password-file: {e}")
        if a.download_dir:
            self.download_dir = Path(a.download_dir).expanduser()
        if a.mp3_bitrate:
            self.mp3_bitrate = a.mp3_bitrate
        self.__post_init__()
        return self
