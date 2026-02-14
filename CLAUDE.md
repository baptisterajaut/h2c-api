# h2c-api

Fake Kubernetes API server backed by compose.yml. Separate project from helmfile2compose — h2c doesn't know this exists. Plausible deniability.

## What it is

A Python HTTP(S) server that reads a docker-compose.yml (produced by h2c or anyone) and responds to Kubernetes API requests with plausible fake data. Apps running in compose that use client-go / kubernetes-python get working responses instead of connection refused.

## Architecture

Two files, two roles:

- **`h2c_api.py`** — the fake apiserver. Runs inside a container. Reads `/data/compose.yml` + `/data/configmaps/` + `/data/secrets/`. Serves HTTPS on port 6443 if certs are present, HTTP otherwise.
- **`h2c_inject.py`** — the CLI. Runs on the host. Reads `compose.yml`, generates self-signed certs (`h2c-sa/`), writes `compose.override.yml` that injects the fake SA mount + env vars into every service.

They share zero code. The only shared contract is "compose.yml is YAML".

## Supported endpoints

**Discovery** (required for client libraries to boot): `/api`, `/api/v1`, `/apis`, `/apis/coordination.k8s.io/v1`, `/version`

**Core (read-only):** namespaces, pods, services, endpoints, configmaps, secrets — list and get.

**Leases (read-write, in-memory):** create, get, update, delete. Leader election stub — single replica = always the leader.

**Everything else:** 501 Not Implemented. Watch (`?watch=true`) explicitly rejected.

## Workflow

```bash
# Lint
pylint h2c_api.py h2c_inject.py
pyflakes h2c_api.py h2c_inject.py

# Build
nerdctl build -t h2c-api:latest .

# Test end-to-end
# 1. Have a compose.yml (from h2c or hand-written)
# 2. python3 h2c_inject.py compose.yml
# 3. nerdctl compose up -d
# 4. nerdctl exec <container> kubectl get pods
```

## Config

All via environment variables (in the container):

| Variable | Default | Description |
|----------|---------|-------------|
| `H2C_COMPOSE` | `/data/compose.yml` | Path to compose file |
| `H2C_DATA_DIR` | `/data` | Base dir for configmaps/ and secrets/ |
| `H2C_PORT` | `6443` | Listen port |
| `H2C_SA_DIR` | `/var/run/secrets/kubernetes.io/serviceaccount` | Path to TLS cert + key |

## Dependencies

- `h2c_api.py`: pyyaml (in container)
- `h2c_inject.py`: pyyaml + openssl CLI (on host)
- No shared dependencies with h2c

## The Hague status

This project simulates a Kubernetes control plane with ~350 lines of Python and a self-signed certificate. The token is fake, the pods are lies, the leases are in-memory, and the TLS handshake is a formality. client-go doesn't know. client-go doesn't need to know.

## Image

Docker Hub: `baptisterajaut/h2c-api`

Build and push: `./build.sh`
