# h2c-api

Fake Kubernetes API server backed by `compose.yml`.

Feed it a compose file, it pretends to be a cluster. Apps using `client-go`, `kubernetes-python`, or `kubectl` get plausible responses. The TLS certificate is self-signed, the token is a string literal, the pods are compose services wearing a trenchcoat, and the leader election has one candidate. This is what the Geneva checklist looks like when you follow it to the letter and miss the point entirely.

## Why

Some applications query the Kubernetes API at runtime — leader election, service discovery, pod metadata. When running in compose (via [helmfile2compose](https://github.com/baptisterajaut/helmfile2compose) or otherwise), these calls fail with connection refused.

h2c-api fixes this by committing fraud at the API level. It's a 350-line Python script that impersonates a Kubernetes control plane convincingly enough that `client-go` — the same library that manages production clusters — trusts it completely. We are not proud. We are shipping.

## How it works

Two files:

| File | Runs on | Does what |
|------|---------|-----------|
| `h2c_inject.py` | Host | Reads `compose.yml`, generates self-signed certs + dummy SA token, writes `compose.override.yml` |
| `h2c_api.py` | Container | Serves a fake k8s API (HTTPS, port 6443) from the compose data |

Docker Compose automatically merges `compose.yml` + `compose.override.yml`. Every service gets:
- A fake ServiceAccount mount at `/var/run/secrets/kubernetes.io/serviceaccount/`
- `KUBERNETES_SERVICE_HOST=h2c-api` and `KUBERNETES_SERVICE_PORT=6443`

Client libraries see valid TLS, a real CA cert, and a real token file. They don't ask questions. Neither should you.

## Usage

```bash
# 1. You have a compose.yml (from helmfile2compose, or hand-written, or whatever)

# 2. Inject the fake API
python3 h2c_inject.py compose.yml

# 3. Done
docker compose up -d

# 4. Verify (from inside any service)
docker exec <container> kubectl get pods
docker exec <container> kubectl get namespaces
```

`kubectl version` will report `Server Version: v1.28.0-h2c`. If this doesn't raise alarms, the deception is complete.

## Supported endpoints

| Endpoint | Verbs | Source |
|----------|-------|--------|
| `/version` | GET | Static |
| `/api`, `/apis`, `/api/v1` | GET | Static (discovery) |
| Namespaces | GET, LIST | Project name |
| Pods | GET, LIST | Compose services |
| Services | GET, LIST | Compose services |
| Endpoints | LIST | Compose services + ports |
| ConfigMaps | GET, LIST | `configmaps/` directory |
| Secrets | GET, LIST | `secrets/` directory |
| Leases | GET, LIST, CREATE, PUT, DELETE | In-memory |
| Everything else | — | 501 Not Implemented |

Watch (`?watch=true`) is explicitly unsupported and returns 501. This is where we draw the line. The line is arbitrary but it exists.

## Leader election

Leases are stubbed in-memory. Since compose runs single replicas, there's no contention — the first (and only) candidate always wins. Democracy with one voter. Turnout is excellent.

## Configuration

Environment variables for the container:

| Variable | Default | Description |
|----------|---------|-------------|
| `H2C_COMPOSE` | `/data/compose.yml` | Compose file path |
| `H2C_DATA_DIR` | `/data` | Base dir for `configmaps/` and `secrets/` |
| `H2C_PORT` | `6443` | Listen port |
| `H2C_SA_DIR` | `/var/run/secrets/kubernetes.io/serviceaccount` | TLS cert/key path |

## Requirements

- **h2c_inject.py** (host): Python 3, PyYAML, `openssl` CLI
- **h2c_api.py** (container): Python 3, PyYAML (included in image)

## Image

```bash
docker pull baptisterajaut/h2c-api:latest
```

Build locally:
```bash
./build.sh         # builds and pushes :latest
./build.sh v0.1    # builds and pushes :v0.1
```

## Relationship with helmfile2compose

None. h2c-api is a separate project that happens to read the same YAML format. helmfile2compose doesn't know this exists and bears no responsibility for what happens here. Any resemblance to a functioning Kubernetes cluster is purely coincidental and should not be presented as evidence.
