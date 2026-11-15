"""Push a message to Broc's phone through ntfy (NTFY_TOPIC in .env). Best effort."""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("socialq")


def push(title: str, body: str, tags: str = "") -> bool:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title.encode()}
    if tags:
        headers["Tags"] = tags
    try:
        resp = requests.post(f"{server}/{topic}", data=body[:3800].encode(), timeout=15, headers=headers)
        return resp.ok
    except requests.RequestException as exc:
        log.warning("couldn't send a notification to your phone: %s", exc)
        return False
