# Agent Relay on Kubernetes (kind)

Manifests for running the relay and PostgreSQL on a local
[kind](https://kind.sigs.k8s.io/) cluster, in the `agent-relay` namespace.

| File | Contents |
| --- | --- |
| `kind-cluster.yaml` | Single-node cluster `agent-relay` (allows cgroup v1 hosts, see below) |
| `kustomization.yaml` | Namespace, labels, and the generated DB/enrollment Secrets |
| `postgres.yaml` | `postgres` Service + StatefulSet with a 1Gi PVC and `pg_isready` probes |
| `agent-relay.yaml` | `agent-relay` Service + Deployment: waits for PostgreSQL, readiness on `/ready`, liveness on `/health` |

## Deploy

Requires Docker, `kind`, and `kubectl` (`winget install Kubernetes.kind`;
Docker Desktop ships `kubectl`).

```bash
kind create cluster --config k8s/kind-cluster.yaml
docker build -t agent-relay:local .
kind load docker-image agent-relay:local --name agent-relay
kubectl --context kind-agent-relay apply -k k8s/
kubectl --context kind-agent-relay -n agent-relay rollout status statefulset/postgres
kubectl --context kind-agent-relay -n agent-relay rollout status deployment/agent-relay
```

The image uses `imagePullPolicy: Never`, so after rebuilding it, load it again
and run `kubectl -n agent-relay rollout restart deployment/agent-relay`.
The CI workflow (`act push`, see the main README) does this with a unique image
tag per run instead of `:local`.

## Dashboard and integration test

Port 8000 and 5432 may already be taken by `docker compose`, so forward to
other ports:

```bash
kubectl --context kind-agent-relay -n agent-relay port-forward svc/agent-relay 18000:8000
kubectl --context kind-agent-relay -n agent-relay port-forward svc/postgres 15432:5432
```

Open <http://127.0.0.1:18000/> for the dashboard. Run the API integration test
against the cluster (it reads the relay's database through the second
forward, and creates its `agent_relay_test` database there too):

```bash
RELAY_TEST_BASE_URL=http://127.0.0.1:18000 \
RELAY_TEST_TARGET_DATABASE_URL=postgresql+psycopg://agent_relay:agent_relay@127.0.0.1:15432/agent_relay \
RELAY_TEST_DATABASE_URL=postgresql+psycopg://agent_relay:agent_relay@127.0.0.1:15432/agent_relay_test \
uv run pytest test_integration_api.py -v
```

A port-forward is bound to one pod; restart it if that pod is replaced.

## Notes

- **cgroup v1:** Docker Desktop on WSL 2 may run cgroup v1, which kubelet
  1.35+ refuses by default. `kind-cluster.yaml` sets `failCgroupV1: false`.
  Moving WSL to cgroup v2 is the long-term fix.
- **`kind load` of `postgres:17-alpine`** can fail with `content digest ...
  not found` under Docker Desktop's containerd image store. Don't preload it;
  the node pulls it from Docker Hub.
- **Credentials** in `kustomization.yaml` are the same local-only defaults as
  `compose.yaml`. Change them for anything beyond local use.
- Clean up with `kind delete cluster --name agent-relay` (this also deletes
  the PVC data).
