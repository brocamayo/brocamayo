import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from socialq.config import load_config  # noqa: E402


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A throwaway project folder with config, videos dir and no network sleeps."""
    (tmp_path / "videos").mkdir()
    (tmp_path / "credentials").mkdir()
    (tmp_path / "config.yaml").write_text(
        """
timezone: America/Los_Angeles
slots: ["mon,wed,fri 17:00"]
max_attempts: 2
youtube: {enabled: true}
instagram: {enabled: true}
tiktok: {enabled: true, mode: direct, chunk_size_mb: 5}
"""
    )
    monkeypatch.setattr("time.sleep", lambda s: None)
    for var in ("INSTAGRAM_USER_ID", "INSTAGRAM_ACCESS_TOKEN", "NOTIFY_WEBHOOK_URL"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture
def config(project):
    return load_config(project / "config.yaml")


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.content = b"x"
        self.text = str(self._body)

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._body


class FakeSession:
    """Records calls and answers them from a list of (method, url-substring, response)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, **kwargs):
        body = kwargs.get("data")
        if hasattr(body, "read"):
            kwargs["data"] = body.read()
        self.calls.append((method, url, kwargs))
        for i, (m, fragment, resp) in enumerate(self.routes):
            if m == method and fragment in url:
                if isinstance(resp, list):
                    return resp.pop(0) if len(resp) > 1 else resp[0]
                return resp
        raise AssertionError(f"unexpected {method} {url}")
