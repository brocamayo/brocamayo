import base64
import json

from socialq.platforms import youtube as yt

CHANNELS = {
    "UC_silent": {"id": "UC_silent", "snippet": {"title": "The Silent Catalyst", "customUrl": "@thesilentcatalyst"}},
    "UC_broc": {"id": "UC_broc", "snippet": {"title": "Broc Mayo", "customUrl": "@brocmayo"}},
}


class FakeYouTube:
    def __init__(self, mine_id):
        self.mine_id = mine_id
        self.kwargs = {}

    def channels(self):
        return self

    def list(self, **kwargs):
        self.kwargs = kwargs
        return self

    def execute(self):
        if self.kwargs.get("mine"):
            return {"items": [CHANNELS[self.mine_id]]}
        handle = self.kwargs["forHandle"]
        return {"items": [c for c in CHANNELS.values() if c["snippet"]["customUrl"] == handle]}


class FakeCreds:
    def __init__(self, email):
        body = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
        self.id_token = f"h.{body}.s"


def login(monkeypatch, config, account, mine_id, answer="y", want=None, email="me@gmail.com"):
    token = config.path(config.settings(account, "youtube")["token_file"])

    def fake_credentials(cfg, acct, interactive=False, login_hint=None):
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("{}")
        return FakeCreds(email)

    monkeypatch.setattr(yt, "_credentials", fake_credentials)
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: FakeYouTube(mine_id))
    def fake_input(prompt):
        if answer is None:
            raise AssertionError("should not ask when --channel is given")
        return answer

    monkeypatch.setattr("builtins.input", fake_input)
    return yt.authorize(config, account, want=want), token


def test_login_shows_email_and_channel(monkeypatch, config, capsys):
    ok, _ = login(monkeypatch, config, "main", "UC_broc", email="brocmayo@gmail.com")
    out = capsys.readouterr().out
    assert ok and "Signed in as:  brocmayo@gmail.com" in out and "Broc Mayo (@brocmayo)" in out
    assert yt.saved_channel(config, "main")["id"] == "UC_broc"


def test_wanted_handle_matches_without_asking(monkeypatch, config):
    ok, _ = login(monkeypatch, config, "main", "UC_broc", answer=None, want="brocmayo")
    assert ok and yt.saved_channel(config, "main")["title"] == "Broc Mayo"


def test_wanted_handle_mismatch_explains_which_row_to_pick(monkeypatch, config, capsys):
    ok, token = login(monkeypatch, config, "main", "UC_silent", answer=None, want="@brocmayo")
    out = capsys.readouterr().out
    assert not ok and not token.exists()
    assert "@brocmayo is the channel named \"Broc Mayo\"" in out
    assert "--google-id" in out


def test_same_channel_warning_and_saying_no_logs_out(monkeypatch, config, capsys):
    login(monkeypatch, config, "main", "UC_broc")
    capsys.readouterr()
    login(monkeypatch, config, "second", "UC_broc", answer="y")
    assert "account 'main' is logged in to this same channel" in capsys.readouterr().out
    ok, token = login(monkeypatch, config, "second", "UC_silent", answer="n")
    assert not ok and not token.exists()


def test_accounts_command_shows_channel(monkeypatch, config, project, capsys):
    from socialq.cli import main

    login(monkeypatch, config, "main", "UC_broc")
    capsys.readouterr()
    main(["-c", str(project / "config.yaml"), "accounts"])
    out = capsys.readouterr().out
    assert "youtube    logged in to channel: Broc Mayo" in out


def test_google_id_is_passed_as_login_hint(monkeypatch, config, project):
    import google_auth_oauthlib.flow as gflow

    (project / "credentials" / "youtube_client_secret.json").write_text("{}")
    seen = {}

    class FakeFlow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            return cls()

        def run_local_server(self, **kwargs):
            seen.update(kwargs)
            raise KeyboardInterrupt  # stop before any real network

    monkeypatch.setattr(gflow, "InstalledAppFlow", FakeFlow)
    try:
        yt._credentials(config, "main", interactive=True, login_hint="110447739688482854681")
    except KeyboardInterrupt:
        pass
    assert seen["login_hint"] == "110447739688482854681" and seen["prompt"] == "consent"
