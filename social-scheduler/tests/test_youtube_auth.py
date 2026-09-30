import json

from socialq.platforms import youtube as yt


class FakeYouTube:
    def __init__(self, channel_id, title):
        self.item = {"id": channel_id, "snippet": {"title": title}}

    def channels(self):
        return self

    def list(self, **kwargs):
        assert kwargs == {"part": "snippet", "mine": True}
        return self

    def execute(self):
        return {"items": [self.item]}


def login(monkeypatch, config, account, channel_id, title, answer):
    token = config.path(config.settings(account, "youtube")["token_file"])

    def fake_credentials(cfg, acct, interactive=False):
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("{}")
        return object()

    monkeypatch.setattr(yt, "_credentials", fake_credentials)
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: FakeYouTube(channel_id, title))
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    yt.authorize(config, account)
    return token


def test_login_saves_and_shows_channel(monkeypatch, config, capsys):
    login(monkeypatch, config, "main", "UC1", "Broc Mayo", "")
    assert yt.saved_channel(config, "main") == {"id": "UC1", "title": "Broc Mayo"}
    assert "Logged in to channel: Broc Mayo" in capsys.readouterr().out


def test_same_channel_warning_and_saying_no_logs_out(monkeypatch, config, capsys):
    login(monkeypatch, config, "main", "UC1", "Broc Mayo", "y")
    capsys.readouterr()
    token = login(monkeypatch, config, "second", "UC1", "Broc Mayo", "n")
    out = capsys.readouterr().out
    assert "account 'main' is logged in to this same channel" in out
    assert "pick the other channel" in out
    assert not token.exists() and yt.saved_channel(config, "second") == {}


def test_accounts_command_shows_channel(monkeypatch, config, project, capsys):
    from socialq.cli import main

    login(monkeypatch, config, "main", "UC1", "Broc Mayo", "y")
    capsys.readouterr()
    main(["-c", str(project / "config.yaml"), "accounts"])
    out = capsys.readouterr().out
    assert "youtube    logged in to channel: Broc Mayo" in out
    assert "not logged in" in out  # second/youtube etc.
    assert json.loads(yt.channel_file(config, "main").read_text())["id"] == "UC1"
