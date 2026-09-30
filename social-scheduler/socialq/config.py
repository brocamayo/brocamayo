"""Loads config.yaml and .env, and resolves paths relative to the project folder."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

PLATFORMS = ("youtube", "instagram", "tiktok", "x")
DEFAULT_ACCOUNT = "main"


@dataclass
class Account:
    name: str
    platforms: list[str]
    delay_minutes: int = 0              # post this long after the scheduled time
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class Config:
    base_dir: Path
    timezone: ZoneInfo
    media_dir: Path
    queue_file: Path
    state_file: Path
    slots: list[str]
    max_attempts: int
    accounts: dict[str, Account]
    retry_delay_minutes: int = 30
    defaults: dict[str, dict[str, Any]] = field(default_factory=dict)

    def account(self, name: str | None) -> Account:
        if name is None:
            if len(self.accounts) == 1:
                return next(iter(self.accounts.values()))
            raise ValueError(f"Pick an account with --account ({', '.join(self.accounts)})")
        if name not in self.accounts:
            raise ValueError(f"Unknown account {name!r}; config.yaml has: {', '.join(self.accounts)}")
        return self.accounts[name]

    def enabled(self, account: str, platform: str) -> bool:
        acct = self.accounts.get(account)
        return bool(acct and platform in acct.platforms)

    def settings(self, account: str, platform: str) -> dict[str, Any]:
        """Shared platform settings, overlaid with the account's own, plus a per-account token file."""
        merged = copy.deepcopy(self.defaults.get(platform, {}))
        merged.update(self.accounts[account].overrides.get(platform, {}))
        merged.setdefault("token_file", f"credentials/{account}/{platform}_token.json")
        return merged

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


def _parse_accounts(raw: dict[str, Any], defaults: dict[str, dict[str, Any]]) -> dict[str, Account]:
    raw_accounts = raw.get("accounts")
    if not raw_accounts:
        # Single-account layout: platforms turned on with `enabled: true`.
        platforms = [p for p in PLATFORMS if defaults.get(p, {}).get("enabled")]
        return {DEFAULT_ACCOUNT: Account(DEFAULT_ACCOUNT, platforms)}
    accounts = {}
    for name, body in raw_accounts.items():
        body = body or {}
        name = str(name)
        if "/" in name or not name.strip():
            raise ValueError(f"Account name {name!r} can't be empty or contain '/'")
        platforms = body.get("platforms") or [p for p in PLATFORMS if p in body]
        unknown = set(platforms) - set(PLATFORMS)
        if unknown:
            raise ValueError(f"Account {name}: unknown platform(s) {sorted(unknown)}")
        accounts[name] = Account(
            name=name,
            platforms=list(platforms),
            delay_minutes=int(body.get("delay_minutes", 0)),
            overrides={p: dict(body[p] or {}) for p in PLATFORMS if p in body},
        )
    return accounts


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

    defaults = {p: dict(raw.get(p) or {}) for p in PLATFORMS}
    return Config(
        base_dir=base_dir,
        timezone=ZoneInfo(raw.get("timezone", "UTC")),
        media_dir=resolve("media_dir", "videos"),
        queue_file=resolve("queue_file", "queue.yaml"),
        state_file=resolve("state_file", "state.json"),
        slots=list(raw.get("slots") or []),
        max_attempts=int(raw.get("max_attempts", 3)),
        retry_delay_minutes=int(raw.get("retry_delay_minutes", 30)),
        accounts=_parse_accounts(raw, defaults),
        defaults=defaults,
    )
