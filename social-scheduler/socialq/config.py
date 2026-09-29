"""Loads config.yaml and .env, and resolves paths relative to the project folder."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

PLATFORMS = ("youtube", "instagram", "tiktok")


@dataclass
class Config:
    base_dir: Path
    timezone: ZoneInfo
    media_dir: Path
    queue_file: Path
    state_file: Path
    slots: list[str]
    max_attempts: int
    retry_delay_minutes: int = 30
    youtube: dict[str, Any] = field(default_factory=dict)
    instagram: dict[str, Any] = field(default_factory=dict)
    tiktok: dict[str, Any] = field(default_factory=dict)

    def platform(self, name: str) -> dict[str, Any]:
        return getattr(self, name)

    def enabled(self, name: str) -> bool:
        return bool(self.platform(name).get("enabled", False))

    def path(self, value: str | os.PathLike) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else self.base_dir / p


def _load_dotenv(base_dir: Path) -> None:
    env_file = base_dir / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv is optional; fall back to a minimal parser
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))
        return
    load_dotenv(env_file, override=False)


def load_config(config_path: str | os.PathLike = "config.yaml") -> Config:
    config_path = Path(config_path).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(
            f"{config_path} not found. Copy config.example.yaml to config.yaml to get started."
        )
    base_dir = config_path.parent
    _load_dotenv(base_dir)
    raw = yaml.safe_load(config_path.read_text()) or {}

    def resolve(key: str, default: str) -> Path:
        p = Path(raw.get(key, default)).expanduser()
        return p if p.is_absolute() else base_dir / p

    return Config(
        base_dir=base_dir,
        timezone=ZoneInfo(raw.get("timezone", "UTC")),
        media_dir=resolve("media_dir", "videos"),
        queue_file=resolve("queue_file", "queue.yaml"),
        state_file=resolve("state_file", "state.json"),
        slots=list(raw.get("slots") or []),
        max_attempts=int(raw.get("max_attempts", 3)),
        retry_delay_minutes=int(raw.get("retry_delay_minutes", 30)),
        youtube=raw.get("youtube") or {},
        instagram=raw.get("instagram") or {},
        tiktok=raw.get("tiktok") or {},
    )
