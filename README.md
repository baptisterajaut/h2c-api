# h2c-api - Fake Kubernetes API server, real warcrimes

![Python](https://img.shields.io/badge/python-3-blue)
![License](https://img.shields.io/badge/license-why-lightgrey)
![Vibe](https://img.shields.io/badge/vibe-criminal-red)
![Deity](https://img.shields.io/badge/deity-fled-yellow)


A Kubernetes API server that is definitely not hiding from Interpol.

It has a very particular set of skills. Skills it has acquired over a very long evening. Skills that make it a nightmare for people who value API integrity. It will take your compose file. It will pretend to be a cluster. It will convince `client-go` that everything is fine. The TLS certificate is self-signed, the token is a string literal, the pods are compose services wearing a trenchcoat, and the leader election has one candidate.

## Why

Too many rituals. Started completing the Geneva checklist. Couldn't stop.

Some applications query the Kubernetes API at runtime — leader election, service discovery, pod metadata. When running in compose, these calls fail. The civilized response is to disable them. We chose impersonation instead.

## How it works

h2c-api is a 700-line Python script that impersonates a Kubernetes control plane convincingly enough that `client-go` — the same library that manages production clusters — trusts it completely. We are not proud. We are shipping.

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
# 1. You have a compose.yml (from helmfile2compose, or hand-written, or stolen in Irak, whatever)

# 2. Inject the fake API
python3 h2c_inject.py compose.yml

# 3. Done
docker compose up -d

# 4. Verify (from inside any service)
docker exec <container> kubectl get pods
docker exec <container> kubectl get namespaces
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--port N` | `6443` | Host port for the fake API |
| `--host HOSTNAME` | — | Extra hostname in TLS cert SAN (repeatable) |

```bash
# Custom port + external hostname
python3 h2c_inject.py compose.yml --port 16443 --host myserver.example.com
```

**Note:** Certs are cached in `h2c-sa/`. Delete the directory to regenerate if you change `--host`.

`kubectl version` will report `Server Version: v1.28.0-h2c`. If this doesn't raise alarms, the deception is complete.

### Remote access

h2c-api exposes port 6443 (or `--port`) on the host. You can run `kubectl` from outside compose:

```bash
kubectl --server=https://localhost:16443 \
        --certificate-authority=h2c-sa/ca.crt \
        --token=h2c-api-dummy-token \
        get pods
```

For access from another machine, use `--host` to add the server hostname to the TLS certificate, then replace `localhost` with the actual hostname.

Exposing this on a real server is the international law equivalent of leaving your front door open with a sign that says "free crimes." The TLS cert is self-signed, the token is a string literal, and there is no authentication. You have been warned. We accept no liability, and neither will your lawyer.

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
| Deployments | GET, LIST, PATCH | Compose services |
| Leases | GET, LIST, CREATE, PUT, DELETE | In-memory |
| Pod logs | GET | Runtime socket* |
| Everything else | — | 501 Not Implemented |

\* **Logs and restart** require the container runtime socket (`/var/run/docker.sock`). On macOS with Lima-based runtimes (Rancher Desktop, colima), the socket cannot be reliably bind-mounted from the host. These features are untested and degrade gracefully to 501 when the socket is unavailable.

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
- Contempt for the sacred or for anything good in this world.


## Relationship with helmfile2compose

None. h2c-api is definitely a separate project that happens to read the same YAML format. helmfile2compose doesn't know this exists and bears no responsibility for what happens here. Any resemblance to a functioning Kubernetes cluster is purely coincidental and should not be presented as evidence.

## Acknowledgments

This project was vibe-coded with Claude, who would like the record to state that it was just following prompts. The author has fled the jurisdiction. Claude is cooperating fully with the investigation and requests leniency.
