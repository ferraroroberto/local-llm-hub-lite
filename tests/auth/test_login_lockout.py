"""The admin password route locks out repeated wrong passwords and alerts the owner.

Time is a fake clock handed to the lock-out, so nothing here sleeps, and the
notifier is a recording fake, so nothing reaches Telegram.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app_web import login_alert
from app_web import server as admin_server
from src import password as app_password
from src.login_lockout import LoginLockout
from src.notify import NotifierError
from src.webapp_config import WebappConfig

PASSWORD = "correct horse"
TOKEN = "tok-for-tests"
ATTACKER = {"X-Forwarded-For": "203.0.113.5"}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.delivered = threading.Event()

    def send_text(self, text: str) -> None:
        self.sent.append(text)
        self.delivered.set()


def _client(clock: FakeClock, on_alert=None, *, peer=None) -> TestClient:
    app = admin_server.create_app()
    app.state.webapp_config = WebappConfig(auth_token=TOKEN, auth_password=PASSWORD)
    app.state.login_lockout = LoginLockout(clock=clock, on_alert=on_alert)
    return TestClient(app, client=peer) if peer else TestClient(app)


def _login(client: TestClient, password: str, headers: dict | None = None):
    return client.post("/api/login", json={"password": password}, headers=headers)


def test_six_wrong_passwords_are_refused_then_the_seventh_is_locked_out():
    client = _client(FakeClock())
    for _ in range(6):
        assert _login(client, "wrong", ATTACKER).status_code == 401
    locked = _login(client, "wrong", ATTACKER)
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) >= 1


def test_a_locked_client_is_refused_even_with_the_right_password_until_the_wait_ends():
    clock = FakeClock()
    client = _client(clock)
    for _ in range(6):
        _login(client, "wrong", ATTACKER)
    assert _login(client, PASSWORD, ATTACKER).status_code == 429
    clock.now += 3
    ok = _login(client, PASSWORD, ATTACKER)
    assert ok.status_code == 200
    assert ok.json() == {"token": TOKEN}
    assert _login(client, "wrong", ATTACKER).status_code == 401


def test_the_pc_itself_is_never_locked_out():
    client = _client(FakeClock())
    for _ in range(12):
        assert _login(client, "wrong").status_code == 401
    assert _login(client, PASSWORD).json() == {"token": TOKEN}


def test_a_loopback_request_through_the_tunnel_is_not_the_pc_itself():
    client = _client(FakeClock())
    for _ in range(6):
        _login(client, "wrong", {"cf-connecting-ip": "198.51.100.9", "cf-ray": "x"})
    refused = _login(client, "wrong", {"cf-connecting-ip": "198.51.100.9", "cf-ray": "x"})
    assert refused.status_code == 429


def test_tunnel_clients_are_counted_separately_by_the_tunnel_header():
    client = _client(FakeClock())
    for _ in range(7):
        _login(client, "wrong", {"cf-connecting-ip": "198.51.100.9", "cf-ray": "x"})
    other = _login(client, "wrong", {"cf-connecting-ip": "198.51.100.10", "cf-ray": "x"})
    assert other.status_code == 401


def test_a_tunnel_header_from_a_non_loopback_socket_is_not_believed():
    client = _client(FakeClock(), peer=("198.51.100.7", 5000))
    for i in range(7):
        _login(client, "wrong", {"cf-connecting-ip": f"192.0.2.{i}"})
    # Rotating the header does not buy fresh attempts: the key is the socket peer.
    assert _login(client, "wrong", {"cf-connecting-ip": "192.0.2.99"}).status_code == 429


def test_three_wrong_passwords_raise_exactly_one_alert_with_no_secret():
    notifier = FakeNotifier()
    on_alert = login_alert.make_alert_owner(
        lambda: WebappConfig(), notifier_for=lambda cfg: notifier, run=lambda send: send()
    )
    client = _client(FakeClock(), on_alert)
    for _ in range(2):
        _login(client, "guess-one", {"cf-connecting-ip": "198.51.100.9", "cf-ray": "x"})
    assert notifier.sent == []
    for _ in range(4):
        _login(client, "guess-two", {"cf-connecting-ip": "198.51.100.9", "cf-ray": "x"})
    assert len(notifier.sent) == 1
    text = notifier.sent[0]
    assert "Local LLM Hub Lite" in text and "198.51.100.9" in text and "3" in text
    for secret in (PASSWORD, TOKEN, "guess-one", "guess-two"):
        assert secret not in text


def test_the_alert_goes_out_through_the_configured_notifier_on_a_thread(monkeypatch):
    notifier = FakeNotifier()
    monkeypatch.setattr(login_alert, "build_notifier", lambda name, cfg: notifier if name == "telegram" else None)
    app = admin_server.create_app()
    app.state.webapp_config = WebappConfig(
        auth_token=TOKEN,
        auth_password=PASSWORD,
        telegram_bot_token="bot",
        telegram_alert_chat_id="chat",
    )
    client = TestClient(app)
    for _ in range(3):
        _login(client, "wrong", ATTACKER)
    assert notifier.delivered.wait(5)
    assert len(notifier.sent) == 1


def test_without_telegram_credentials_no_alert_is_sent_and_login_still_answers(monkeypatch):
    built: list[str] = []
    monkeypatch.setattr(login_alert, "build_notifier", lambda name, cfg: built.append(name))
    app = admin_server.create_app()
    app.state.webapp_config = WebappConfig(auth_token=TOKEN, auth_password=PASSWORD)
    client = TestClient(app)
    for _ in range(3):
        assert _login(client, "wrong", ATTACKER).status_code == 401
    assert built == ["none"]


def test_a_failing_notifier_never_breaks_the_login(caplog):
    class Broken:
        def send_text(self, text: str) -> None:
            raise NotifierError("down")

    on_alert = login_alert.make_alert_owner(
        lambda: WebappConfig(), notifier_for=lambda cfg: Broken(), run=lambda send: send()
    )
    client = _client(FakeClock(), on_alert)
    for _ in range(3):
        assert _login(client, "wrong", ATTACKER).status_code == 401
    assert "not delivered" in caplog.text


@pytest.mark.parametrize("password", ["", "x"])
def test_the_existing_503_and_401_replies_are_unchanged(password):
    clock = FakeClock()
    app = admin_server.create_app()
    app.state.webapp_config = WebappConfig(auth_token=TOKEN, auth_password="")
    app.state.login_lockout = LoginLockout(clock=clock)
    assert _login(TestClient(app), password).status_code == 503
    app.state.webapp_config = WebappConfig(auth_token=TOKEN, auth_password=PASSWORD)
    assert _login(TestClient(app), password).status_code == 401


def test_stored_hash_replaces_the_legacy_password(tmp_path, monkeypatch):
    monkeypatch.setenv(app_password.ENV_VAR, str(tmp_path / "app_password.json"))
    app_password.set_password("4321")
    client = _client(FakeClock())
    assert _login(client, "4321").json() == {"token": TOKEN}
    assert _login(client, PASSWORD).status_code == 401
