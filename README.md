# Agent Relay

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. PostgreSQL persists the queue and
attempts, while workers execute tasks on their own machines. The included
worker deterministically returns `input.upper()`.

## Run it

With Docker, `compose.yaml` runs the relay and its `postgres` database:

```bash
docker compose up -d --build
```

Both ports are published on `127.0.0.1` only: the API on 8000 and PostgreSQL
on 5432 (so the tests can reach it). Data lives in the `postgres-data` volume.
Set `POSTGRES_PASSWORD` and `RELAY_ENROLLMENT_SECRET` (for example in a `.env`
file) for anything beyond local use.

To run the API on the host instead, start only the database:

```bash
uv sync
docker compose up -d postgres
uv run uvicorn main:app --reload
```

Open <http://127.0.0.1:8000/> for the token-based local dashboard. The default
database is `postgresql+psycopg://agent_relay:agent_relay@127.0.0.1:5432/agent_relay`;
set `RELAY_DATABASE_URL` to use another PostgreSQL database (`postgresql://`
URLs are accepted and use the psycopg 3 driver). `GET /health` is a liveness check and `GET /ready` verifies database
connectivity and schema (it queries the real tables, so a wiped volume
reports not-ready instead of passing with zero tables).

Register two identities and send a task:

```bash
alice=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"alice"}')
bob=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"uppercase"}')
```

The response contains each agent's secret `token` once. Keep it outside source
control. Use `Authorization: Bearer <token>` for all subsequent API calls;
registration is the only unauthenticated endpoint. For a shared installation,
set `RELAY_ENROLLMENT_SECRET` and send it as `X-Enrollment-Secret` when
registering.

## Run the deterministic worker

The worker can register itself and save credentials in a mode-0600 JSON file:

```bash
uv run python main.py worker \
  --base-url http://127.0.0.1:8000 \
  --name uppercase \
  --credentials ./uppercase-credentials.json \
  --worker-id laptop-1
```

For failure/redelivery demonstrations, make local execution intentionally slow
and stop the process after one completion:

```bash
uv run python main.py worker --credentials ./uppercase-credentials.json \
  --slow-seconds 75 --worker-id slow-laptop
```

The worker heartbeats during long work. Killing it leaves the claim leased;
after the 60-second lease expires, another worker can claim the task with a new
token and incremented attempt number. `RELAY_LEASE_SECONDS` and
`RELAY_MAX_ATTEMPTS` are configurable server settings.

An existing credential can also be supplied explicitly (the token is not
written to disk):

```bash
uv run python main.py worker --agent-id agent_123 --token agt_… --worker-id laptop-2
```

## Storage and delivery behavior

`database.py` contains the engine, SQLAlchemy models, and lease recovery.
`storage.py` contains task/claim/recovery operations; routes and request models
are kept in `main.py` and `schemas.py`. Claims select the oldest queued task
with `FOR UPDATE SKIP LOCKED`, so concurrent workers in any number of API
processes take different tasks without blocking each other. Heartbeat, terminal
submission, and recovery lock the task row before its attempts, so every writer
takes locks in the same order. Concurrent task creation with the same
`Idempotency-Key` is resolved by the `(sender_id, idempotency_key)` unique
constraint.

Claims are at-least-once and leased for 60 seconds by default. Heartbeats extend
an active lease. A completion or failure must include the recipient's bearer
token and claim token. Repeating the exact terminal request with that claim
token is idempotent; a stale token or different result receives `409`.

## Verify

The test suite covers the main protocol, sender/recipient access boundaries,
hashed claim-token behavior, idempotent terminal retries, concurrent claims,
lease expiry before and after recovery, pagination/error shape, and dashboard
asset serving:

```bash
uv run pytest -q
```

Tests need PostgreSQL (`docker compose up -d postgres`). They always use a
separate database, `RELAY_TEST_DATABASE_URL` (default `agent_relay_test` on
`127.0.0.1:5432`), create it if missing, and refuse any database whose name
does not end in `_test`, because the fixtures drop and recreate all tables.
Your `RELAY_DATABASE_URL` is ignored during tests.

`test_integration_api.py` runs acceptance scenario 1 over HTTP against a real
uvicorn process and checks the rows in PostgreSQL. To run it against the
compose stack instead (it leaves its test agents and task in that database):

```bash
RELAY_TEST_BASE_URL=http://127.0.0.1:8000 \
RELAY_TEST_TARGET_DATABASE_URL=postgresql://agent_relay:agent_relay@127.0.0.1:5432/agent_relay \
  uv run pytest test_integration_api.py
```

Use `127.0.0.1` rather than `localhost` for the database host: on some systems
`localhost` resolves to IPv6 `::1` first, which Docker does not publish here.

## CI

`.github/workflows/ci.yml` runs on pushes to `main`, pull requests, and manual
dispatch:

1. **test** starts a `postgres:17-alpine` service (published on host port
   55432) and runs `uv run pytest`, including the API integration test.
2. **deploy** runs only if `test` passed (and not for pull requests). It builds
   `agent-relay:<UTC timestamp>-<commit>`, loads it into the `agent-relay` kind
   cluster (creating the cluster if it does not exist), applies `k8s/` with
   that image tag, and waits for `rollout status`.

Run it locally with [act](https://nektosact.com/) (`winget install nektos.act`)
against Docker Desktop and the kind cluster from `k8s/README.md`:

```bash
act push
```

`.actrc` selects the `catthehacker/ubuntu:act-latest` runner image and mounts
the Docker socket into the job containers. They build and `kind load` through
the host's Docker engine and use act's default host network, so the
kubeconfig from `kind get kubeconfig` (API on `127.0.0.1`) works unchanged.
act copies the working tree, so uncommitted changes are tested and deployed
too. Each run loads another image onto the kind node; list them with
`docker exec agent-relay-control-plane crictl images | grep agent-relay` and
remove old ones with `crictl rmi`.

This starter intentionally does not include external brokers or an LLM.
