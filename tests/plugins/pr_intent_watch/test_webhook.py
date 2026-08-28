"""Live webhook listener contracts: HMAC, event filtering, fast 202, and the
shared review path (one seen-map, one marker, poll and webhook alike)."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import socket
import stat
import threading
import time

import pytest

from plugins.pr_intent_watch import core as core_module
from plugins.pr_intent_watch import github as gh
from plugins.pr_intent_watch import webhook as wh
from plugins.pr_intent_watch.core import WatchConfig, load_state, watch_config_from_raw
from tests.plugins.pr_intent_watch._helpers import (
    MARKER,
    REPO,
    FakeGitHub,
    install_fake_github,
    install_fake_review,
    make_pull,
    make_review,
    seen_entry,
    seed_state,
    write_config,
)

SECRET = "unit-test-webhook-secret"
WEBHOOK_PATH = "/webhooks/pr-intent-watch"
LOCALHOST = "127.0.0.1"


@pytest.fixture
def home(monkeypatch, tmp_path):
    """HERMES_HOME for the listener; the root conftest already isolates it,
    this pins it to this suite's tmp_path explicitly."""
    home_dir = tmp_path / "hermes_home"
    home_dir.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home_dir))
    return home_dir


def _config(**section: object) -> WatchConfig:
    section.setdefault("listen_host", LOCALHOST)
    return watch_config_from_raw({"pr_intent_watch": dict(section)})


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _wait_for(condition, timeout: float = 10.0) -> bool:
    """The 202 is answered before the delivery runs — give the thread a moment."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return bool(condition())


def _request(port: int, method: str, path: str, body=None, headers=None):
    conn = http.client.HTTPConnection(LOCALHOST, port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _delivery_body(
    *, action: str = "opened", number: int = 5, repo: str = REPO
) -> bytes:
    return json.dumps(
        {
            "action": action,
            "repository": {"full_name": repo},
            "pull_request": {"number": number},
        }
    ).encode("utf-8")


def _post(
    port: int,
    body: bytes,
    *,
    event: str | None = "pull_request",
    signature: str | None = None,
    path: str = WEBHOOK_PATH,
):
    headers = {"Content-Type": "application/json"}
    if event is not None:
        headers["X-GitHub-Event"] = event
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return _request(port, "POST", path, body=body, headers=headers)


@pytest.fixture
def listener(home):
    """A live listener on an ephemeral localhost port; deliveries recorded."""
    delivered: list[int] = []
    server = wh.make_server(
        _config(),
        on_delivery=delivered.append,
        secret=SECRET,
        host=LOCALHOST,
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, delivered
    finally:
        server.shutdown()
        server.server_close()


# ── HMAC ────────────────────────────────────────────────────────────────────


def test_bad_signature_is_401_and_never_enqueues(listener):
    server, delivered = listener
    body = _delivery_body()

    status, _ = _post(
        server.server_address[1], body, signature="sha256=" + "0" * 64
    )
    assert status == 401

    # Wrong secret entirely.
    status, _ = _post(server.server_address[1], body, signature=_sign(body, "other"))
    assert status == 401

    # Missing header altogether.
    status, _ = _post(server.server_address[1], body, signature=None)
    assert status == 401

    assert delivered == []


def test_signature_is_checked_over_the_raw_body(listener):
    server, delivered = listener
    # Signature computed over different bytes than what is sent.
    status, _ = _post(
        server.server_address[1],
        _delivery_body(number=6),
        signature=_sign(_delivery_body(number=7)),
    )
    assert status == 401
    assert delivered == []


def test_good_signature_is_accepted(listener):
    server, delivered = listener
    body = _delivery_body(number=7)
    status, _ = _post(server.server_address[1], body, signature=_sign(body))
    assert status == 202
    assert _wait_for(lambda: delivered == [7])


# ── event / repo filtering ──────────────────────────────────────────────────


def test_ping_event_answers_200_ok(listener):
    server, _ = listener
    body = json.dumps({"zen": "Keep it logically awesome."}).encode("utf-8")
    status, payload = _post(
        server.server_address[1], body, event="ping", signature=_sign(body)
    )
    assert status == 200
    assert json.loads(payload) == {"ok": True}


@pytest.mark.parametrize("action", ["synchronize", "closed", "labeled", "edited", "ready_for_review"])
def test_non_opening_actions_are_204_without_review(listener, action):
    server, delivered = listener
    body = _delivery_body(action=action)
    status, payload = _post(
        server.server_address[1], body, signature=_sign(body)
    )
    assert status == 204
    assert payload == b""
    assert delivered == []


def test_non_pull_request_events_are_204(listener):
    server, delivered = listener
    body = _delivery_body()
    for event in ("push", "issues", "status"):
        status, _ = _post(server.server_address[1], body, event=event, signature=_sign(body))
        assert status == 204
    status, _ = _post(  # no event header at all
        server.server_address[1], body, signature=_sign(body), event=None
    )
    assert status == 204
    assert delivered == []


def test_other_repositories_are_204_without_review(listener):
    server, delivered = listener
    body = _delivery_body(repo="someone/else")
    status, _ = _post(server.server_address[1], body, signature=_sign(body))
    assert status == 204
    assert delivered == []


def test_repo_match_is_case_insensitive(listener):
    server, delivered = listener
    body = _delivery_body(repo=REPO.upper())
    status, _ = _post(server.server_address[1], body, signature=_sign(body))
    assert status == 202
    assert _wait_for(lambda: delivered == [5])


def test_reopened_is_reviewed_like_opened(listener):
    server, delivered = listener
    body = _delivery_body(action="reopened", number=9)
    status, _ = _post(server.server_address[1], body, signature=_sign(body))
    assert status == 202
    assert _wait_for(lambda: delivered == [9])


def test_unparseable_payload_after_good_signature_is_400(listener):
    server, delivered = listener
    body = b"this is not json"
    status, _ = _post(server.server_address[1], body, signature=_sign(body))
    assert status == 400
    assert delivered == []


def test_delivery_without_a_pull_number_is_400(listener):
    server, delivered = listener
    body = json.dumps({"action": "opened", "repository": {"full_name": REPO}}).encode()
    status, _ = _post(server.server_address[1], body, signature=_sign(body))
    assert status == 400
    assert delivered == []


# ── probes and routing ──────────────────────────────────────────────────────


def test_health_endpoint(listener):
    server, _ = listener
    status, payload = _request(server.server_address[1], "GET", "/health")
    assert status == 200
    assert json.loads(payload) == {"status": "ok"}


def test_get_on_webhook_path_answers_one_line_probe(listener):
    server, _ = listener
    status, payload = _request(server.server_address[1], "GET", WEBHOOK_PATH)
    assert status == 200
    assert payload.decode("utf-8").strip() == "pr_intent_watch"


def test_unknown_paths_are_404(listener):
    server, delivered = listener
    port = server.server_address[1]
    assert _request(port, "GET", "/nope")[0] == 404
    assert _request(port, "POST", "/health", body=b"{}", headers={})[0] == 404
    assert delivered == []


# ── the handler never waits on the review ───────────────────────────────────


def test_202_is_answered_before_the_delivery_finishes(home):
    release = threading.Event()
    started = threading.Event()

    def slow_delivery(number: int) -> None:
        started.set()
        assert release.wait(10), "test never released the delivery"

    server = wh.make_server(
        _config(),
        on_delivery=slow_delivery,
        secret=SECRET,
        host=LOCALHOST,
        port=0,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = _delivery_body()
        status, _ = _post(server.server_address[1], body, signature=_sign(body))
        assert status == 202  # answered while the delivery is still parked
        assert _wait_for(lambda: started.is_set())
    finally:
        release.set()
        server.shutdown()
        server.server_close()


# ── shared review path ──────────────────────────────────────────────────────


def test_review_delivery_comments_and_marks_the_seen_map(home, monkeypatch):
    seed_state(home, seen={})
    # An unrelated extra state key must survive the delivery's save.
    state_path = home / "state" / "pr_intent_watch.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["webhook_secret"] = "keep-me"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    fake = FakeGitHub([make_pull(5)])
    install_fake_github(monkeypatch, fake)
    captured = install_fake_review(monkeypatch, make_review())

    result = wh.review_delivery(5, _config())

    assert result["action"] == "commented"
    assert [number for number, _ in fake.posted] == [5]
    assert fake.posted[0][1].splitlines()[0] == MARKER
    metadata = captured[0]
    assert metadata["number"] == 5
    for file_entry in metadata["files"]:
        assert "patch" not in file_entry

    saved = load_state()
    assert saved["seen"]["5"] == seen_entry(commented=True)
    assert saved["webhook_secret"] == "keep-me"  # nothing dropped on the floor


def test_review_delivery_skips_when_marker_already_on_github(home, monkeypatch):
    seed_state(home, seen={})
    fake = FakeGitHub(
        [make_pull(5)], comments={5: [{"body": f"earlier\n{MARKER}\nnote"}]}
    )
    install_fake_github(monkeypatch, fake)
    install_fake_review(monkeypatch, make_review())

    result = wh.review_delivery(5, _config())

    assert result["action"] == "already_commented"
    assert fake.posted == []
    assert load_state()["seen"]["5"]["commented"] is True


def test_review_delivery_skips_seen_pr_without_any_fetch(home, monkeypatch):
    seed_state(home, seen={"5": seen_entry(commented=True)})
    fake = FakeGitHub([make_pull(5)])
    install_fake_github(monkeypatch, fake)
    install_fake_review(monkeypatch, make_review())

    result = wh.review_delivery(5, _config())

    assert result["action"] == "already_seen"
    assert fake.calls == []
    assert load_state()["seen"]["5"] == seen_entry(commented=True)


def test_review_delivery_without_a_token_leaves_the_poll_to_it(home, monkeypatch):
    seed_state(home, seen={})
    fake = FakeGitHub([make_pull(5)])
    install_fake_github(monkeypatch, fake)
    monkeypatch.setattr(gh, "resolve_token", lambda: None)
    install_fake_review(monkeypatch, make_review())

    result = wh.review_delivery(5, _config())

    assert result["action"] == "no_token"
    assert fake.calls == []
    assert load_state()["seen"] == {}


def test_webhook_and_poll_share_one_seen_map(home, monkeypatch):
    """A webhook-commented PR is not commented again by the next poll tick."""
    seed_state(home, seen={})
    fake = FakeGitHub([make_pull(5)])
    install_fake_github(monkeypatch, fake)
    install_fake_review(monkeypatch, make_review())
    config_path = write_config(home)

    assert wh.review_delivery(5, _config())["action"] == "commented"
    summary = core_module.run_tick(config_path=config_path)

    assert summary["reviewed"] == 0
    assert summary["commented"] == 0
    assert [number for number, _ in fake.posted] == [5]  # exactly one comment


def test_run_tick_delegates_to_review_one_pr(home, monkeypatch):
    seed_state(home, seen={})
    fake = FakeGitHub([make_pull(5)])
    install_fake_github(monkeypatch, fake)
    install_fake_review(monkeypatch, make_review())

    calls: list[tuple[int, str]] = []
    real = core_module.review_one_pr

    def spy(number, *, config, token, state):
        calls.append((number, config.repo))
        return real(number, config=config, token=token, state=state)

    monkeypatch.setattr(core_module, "review_one_pr", spy)
    summary = core_module.run_tick(config_path=write_config(home))

    assert calls == [(5, REPO)]
    assert summary["reviewed"] == 1
    assert summary["commented"] == 1


# ── review queue ────────────────────────────────────────────────────────────


def test_review_queue_runs_deliveries_and_closes(home, monkeypatch):
    done = threading.Event()
    numbers: list[int] = []

    def fake_delivery(number: int, config) -> dict:
        numbers.append(number)
        done.set()
        return {"action": "commented"}

    monkeypatch.setattr(wh, "review_delivery", fake_delivery)
    queue = wh.ReviewQueue(_config())
    try:
        queue.submit(5)
        assert _wait_for(done.is_set)
    finally:
        queue.close()
    assert numbers == [5]


# ── secret ──────────────────────────────────────────────────────────────────


def test_secret_is_generated_once_and_reused(home):
    first = wh.ensure_webhook_secret({})
    assert len(first) >= 32  # 32 urlsafe bytes ≈ 43 chars

    saved = load_state()
    assert saved["webhook_secret"] == first
    mode = stat.S_IMODE((home / "state" / "pr_intent_watch.json").stat().st_mode)
    assert mode == 0o600

    # A second load reuses the stored secret — regenerating would orphan the
    # GitHub hook registration.
    assert wh.ensure_webhook_secret(load_state()) == first


def test_secret_never_appears_in_request_logs(listener, caplog):
    server, _ = listener
    body = _delivery_body()
    with caplog.at_level(logging.DEBUG, logger="plugins.pr_intent_watch.webhook"):
        _post(server.server_address[1], body, signature="sha256=" + "0" * 64)
        good = _sign(body)
        _post(server.server_address[1], body, signature=good)

    assert SECRET not in caplog.text
    assert good not in caplog.text  # no raw HMAC either


# ── serve loop ──────────────────────────────────────────────────────────────


def test_serve_disabled_plugin_binds_nothing(home):
    config_path = write_config(home, {"enabled": False})
    assert wh.serve(config_path=config_path) == 0
    assert not (home / "state").exists()  # no secret minted either


def test_serve_polls_in_process_until_stopped(home, monkeypatch):
    stop = threading.Event()
    ticks: list[dict] = []

    def fake_tick(**kwargs):
        ticks.append(kwargs)
        stop.set()  # one poll is enough for the test

    monkeypatch.setattr(wh, "run_tick", fake_tick)
    config_path = write_config(home, {"poll_seconds": 300})

    assert wh.serve(
        config_path=config_path, stop_event=stop, bind_host=LOCALHOST, bind_port=0
    ) == 0
    assert len(ticks) == 1
    assert ticks[0] == {"config_path": config_path}
    # The secret for the GitHub hook registration is minted on first serve.
    assert len(load_state()["webhook_secret"]) >= 32


def test_serve_returns_one_when_the_port_is_taken(home, monkeypatch):
    monkeypatch.setattr(wh, "run_tick", lambda **kwargs: None)
    blocker = socket.socket()
    try:
        blocker.bind((LOCALHOST, 0))
        blocker.listen(1)
        config_path = write_config(
            home, {"listen_host": LOCALHOST, "listen_port": blocker.getsockname()[1]}
        )
        assert wh.serve(config_path=config_path) == 1
    finally:
        blocker.close()
