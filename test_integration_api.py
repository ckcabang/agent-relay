"""API integration test for SPEC.md acceptance scenario 1.

Unlike ``test_agent_relay.py`` this does not import the app: it talks to a
real relay over HTTP and then inspects the persisted rows in PostgreSQL.

By default it launches ``uvicorn`` in a subprocess against the dedicated test
database chosen in ``conftest.py``, so your dev data is never touched.

To target an already running relay instead (for example ``docker compose``),
set both:

- ``RELAY_TEST_BASE_URL``: the relay's URL, e.g. ``http://127.0.0.1:8000``.
- ``RELAY_TEST_TARGET_DATABASE_URL``: the database that relay writes to, as
  reachable from this machine. The test only reads it.

Running against an external relay leaves the test's agents and task in its
database.
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest
from sqlalchemy import create_engine, text

PROJECT_DIR = Path(__file__).resolve().parent


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def wait_until_ready(base_url: str, server: subprocess.Popen | None = None, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while True:
        if server is not None and server.poll() is not None:
            output = server.stdout.read().decode(errors="replace") if server.stdout else ""
            pytest.fail(f"relay exited during startup:\n{output}")
        try:
            if httpx.get(f"{base_url}/ready", timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            pytest.fail(f"relay at {base_url} did not become ready within {timeout:.0f}s")
        time.sleep(0.2)


@pytest.fixture(scope="module")
def relay() -> Iterator[tuple[str, str]]:
    """Yield ``(base_url, database_url)`` for a running relay."""

    external_url = os.getenv("RELAY_TEST_BASE_URL")
    if external_url:
        database_url = os.getenv("RELAY_TEST_TARGET_DATABASE_URL")
        if not database_url:
            pytest.fail("RELAY_TEST_BASE_URL also needs RELAY_TEST_TARGET_DATABASE_URL to verify the database")
        base_url = external_url.rstrip("/")
        wait_until_ready(base_url)
        yield base_url, database_url
        return

    # conftest.py has already pointed RELAY_DATABASE_URL at the test database.
    database_url = os.environ["RELAY_DATABASE_URL"]
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=PROJECT_DIR,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        wait_until_ready(base_url, server)
        yield base_url, database_url
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


def test_scenario_1_send_claim_complete_read(relay: tuple[str, str]) -> None:
    base_url, database_url = relay
    with httpx.Client(base_url=f"{base_url}/api/v1", timeout=10) as client:
        # 1. Register two agents.
        sender_response = client.post("/agents", json={"name": "sender", "description": "Scenario 1 sender"})
        recipient_response = client.post("/agents", json={"name": "recipient"})
        assert sender_response.status_code == 201
        assert recipient_response.status_code == 201
        sender, recipient = sender_response.json(), recipient_response.json()
        assert sender["agent_id"] != recipient["agent_id"]
        sender_headers = {"Authorization": f"Bearer {sender['token']}"}
        recipient_headers = {"Authorization": f"Bearer {recipient['token']}"}

        # 2. The sender submits a task to the recipient.
        sent = client.post(
            "/tasks",
            headers={**sender_headers, "Idempotency-Key": "scenario-1"},
            json={"to": recipient["agent_id"], "input": "reverse: relay"},
        )
        assert sent.status_code == 201
        assert sent.json()["status"] == "queued"
        task_id = sent.json()["task_id"]

        # 3. The recipient claims it.
        claim = client.post(
            "/tasks/claim", headers=recipient_headers, json={"worker_id": "integration-worker", "wait_seconds": 5}
        )
        assert claim.status_code == 200
        claimed = claim.json()
        assert claimed["task_id"] == task_id
        assert claimed["from"] == sender["agent_id"]
        assert claimed["input"] == "reverse: relay"
        assert claimed["attempt"] == 1
        assert claimed["lease_expires_at"].endswith("Z")
        claim_token = claimed["claim_token"]

        # The inbox is now empty.
        empty = client.post("/tasks/claim", headers=recipient_headers, json={"wait_seconds": 0})
        assert empty.status_code == 204

        # 4. The recipient completes it.
        complete = client.post(
            f"/tasks/{task_id}/complete",
            headers=recipient_headers,
            json={"claim_token": claim_token, "output": "yaler"},
        )
        assert complete.status_code == 200
        assert complete.json() == {"task_id": task_id, "status": "completed"}

        # 5. The sender reads the result.
        result = client.get(f"/tasks/{task_id}", headers=sender_headers)
        assert result.status_code == 200
        task = result.json()
        assert task["from"] == sender["agent_id"]
        assert task["to"] == recipient["agent_id"]
        assert task["status"] == "completed"
        assert task["output"] == "yaler"
        assert task["error"] is None
        assert task["attempt_count"] == 1
        assert task["finished_at"] is not None

        attempts = client.get(f"/tasks/{task_id}/attempts", headers=sender_headers)
        assert attempts.status_code == 200
        [attempt] = attempts.json()["items"]
        assert attempt["outcome"] == "completed"
        assert attempt["worker_id"] == "integration-worker"
        assert "claim_token" not in attempt

    # The flow is durable in the real database, and secrets are stored only as hashes.
    agent_ids = [sender["agent_id"], recipient["agent_id"]]
    engine = create_engine(database_url.replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        with engine.connect() as db:
            agents = {
                row.id: row
                for row in db.execute(text("SELECT * FROM agents WHERE id = ANY(:ids)"), {"ids": agent_ids})
            }
            assert agents[sender["agent_id"]].token_hash == sha256(sender["token"])
            assert agents[recipient["agent_id"]].token_hash == sha256(recipient["token"])
            assert agents[sender["agent_id"]].last_seen_at is not None

            task_row = db.execute(text("SELECT * FROM tasks WHERE id = :id"), {"id": task_id}).one()
            assert task_row.sender_id == sender["agent_id"]
            assert task_row.recipient_id == recipient["agent_id"]
            assert task_row.status == "completed"
            assert task_row.output == "yaler"
            assert task_row.idempotency_key == "scenario-1"

            [attempt_row] = db.execute(text("SELECT * FROM attempts WHERE task_id = :id"), {"id": task_id}).all()
            assert attempt_row.outcome == "completed"
            assert attempt_row.terminal_action == "complete"
            assert attempt_row.claim_token_hash == sha256(claim_token)

            stored = "\n".join(str(tuple(row)) for row in [*agents.values(), task_row, attempt_row])
            for secret in (sender["token"], recipient["token"], claim_token):
                assert secret not in stored
    finally:
        engine.dispose()
