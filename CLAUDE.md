# dekube-fakeapi

Fake Kubernetes API server backed by compose.yml. Separate project from dekube — dekube doesn't know this exists. Plausible deniability.

## What it is

A Python HTTP(S) server that reads a docker-compose.yml (produced by dekube or anyone) and responds to Kubernetes API requests with plausible fake data. Apps running in compose that use client-go / kubernetes-python get working responses instead of connection refused.

## Architecture

Two files, two roles:

- **`h2c_api.py`** — the fake apiserver. Runs inside a container. Reads `/data/compose.yml` + `/data/configmaps/` + `/data/secrets/`. Serves HTTPS on port 6443 if certs are present, HTTP otherwise.
- **`inject.py`** — dual-mode: standalone CLI (generates `compose.override.yml`) or dekube transform extension (injects directly into `compose_services`). Generates self-signed certs via `cryptography`, dummy SA token, and env vars for every service.

They share zero code. The only shared contract is "compose.yml is YAML".

## Supported endpoints

**Discovery** (required for client libraries to boot): `/api`, `/api/v1`, `/apis`, `/apis/apps/v1`, `/apis/coordination.k8s.io/v1`, `/version`. Includes short names (`svc`, `ep`, `deploy`, `po`, `no`, `ns`, `cm`).

**Core (read-only):** nodes, namespaces, pods, services, endpoints, configmaps, secrets — list and get. Deployments — list, get, patch (triggers container restart via Docker API).

**Leases (read-write, in-memory):** create, get, update, delete. Leader election stub — single replica = always the leader.

**Filtering:** LIST operations support `?labelSelector=key=value`. Namespace-scoped endpoints only return resources for the project namespace — other namespaces return empty lists. The h2c-api service itself is excluded from all resource lists.

**Everything else:** 501 Not Implemented. Watch (`?watch=true`) explicitly rejected.

## Workflow

```bash
# Lint
pylint h2c_api.py inject.py
pyflakes h2c_api.py inject.py

# Test end-to-end
# 1. Have a compose.yml (from dekube or hand-written)
# 2. python3 inject.py compose.yml
# 3. docker compose up -d
# 4. docker exec <container> kubectl get pods
```

## Config

All via environment variables (in the container):

| Variable | Default | Description |
|----------|---------|-------------|
| `H2C_COMPOSE` | `/data/compose.yml` | Path to compose file |
| `H2C_DATA_DIR` | `/data` | Base dir for configmaps/ and secrets/ |
| `H2C_PORT` | `6443` | Listen port |
| `H2C_SA_DIR` | `/var/run/secrets/kubernetes.io/serviceaccount` | Path to TLS cert + key |

## Runtime

No Docker image to build or publish. The generated compose service uses `python:3-alpine`, installs pyyaml, pulls `h2c_api.py` from main at startup.

## Dependencies

- `h2c_api.py`: pyyaml (installed at container startup)
- `inject.py`: cryptography + pyyaml (host, pyyaml only for standalone CLI mode — deferred import)
- No shared dependencies with dekube

## The Hague status

This project simulates a Kubernetes control plane with ~800 lines of Python and a self-signed certificate. The token is fake, the pods are lies, the leases are in-memory, and the TLS handshake is a formality. client-go doesn't know. client-go doesn't need to know.
