# dekube-fakeapi - Fake Kubernetes API server, real warcrimes

![Python](https://img.shields.io/badge/python-3-blue)
![License](https://img.shields.io/badge/license-why-lightgrey)
![Vibe](https://img.shields.io/badge/vibe-criminal-red)
![heresy: 666/10](https://img.shields.io/badge/heresy-666%2F10-black)
![Deity](https://img.shields.io/badge/deity-fled-yellow)


A Kubernetes API server that is definitely not hiding from Interpol.

It has a very particular set of skills. Skills it has acquired over a very long evening. Skills that make it a nightmare for people who value API integrity. It will take your compose file. It will pretend to be a cluster. It will convince `client-go` that everything is fine. The TLS certificate is self-signed, the token is a string literal, the pods are compose services wearing a trenchcoat, and the leader election has one candidate.

## Why

Too many rituals. Started completing the Geneva checklist. Couldn't stop.

Some applications query the Kubernetes API at runtime — leader election, service discovery, pod metadata. When running in compose, these calls fail. The civilized response is to disable them. We chose impersonation instead.

## How it works

h2c-api is an 800-line Python script that impersonates a Kubernetes control plane convincingly enough that `client-go` — the same library that manages production clusters — trusts it completely. We are not proud. We are shipping.

Two files:

| File | Runs on | Does what |
|------|---------|-----------|
| `inject.py` | Host | Generates self-signed certs + dummy SA token. **Standalone**: writes `compose.override.yml` + kubeconfig. **h2c transform**: injects directly into compose services. |
| `h2c_api.py` | Container | Serves a fake k8s API (HTTPS, port 6443) from the compose data |

No pre-built Docker image — the generated service uses `python:3-alpine`, installs pyyaml, pulls `h2c_api.py` from `main`, and runs it. Always up to date, nothing to publish, nothing to maintain. The container starts in a few seconds and weighs nothing on your conscience (the rest of the project handles that).

Every service gets:
- A fake ServiceAccount mount at `/var/run/secrets/kubernetes.io/serviceaccount/`
- `KUBERNETES_SERVICE_HOST=h2c-api` and `KUBERNETES_SERVICE_PORT=6443`

Client libraries see valid TLS, a real CA cert, and a real token file. They don't ask questions. Neither should you.

## Usage

### As a dekube transform (recommended)

Install via dekube-manager and add config to `dekube.yaml`:

```bash
python3 dekube-manager.py fake-apiserver
```

```yaml
# dekube.yaml
h2c-api:
  hosts: [myapp.local]           # extra SAN hostnames (optional)
  expose-host-port: 6443         # expose on host + generate kubeconfig (optional)
```

### Options (transform)

The `h2c-api:` key in `dekube.yaml` accepts:

| Key | Default | Description |
|-----|---------|-------------|
| `hosts` | `[]` | Extra SAN hostnames added to the TLS cert (list) |
| `expose-host-port` | — | Expose on host at this port and generate kubeconfig. Omit to keep internal only. |

When `expose-host-port` is set, a kubeconfig file is written to the output directory. The first entry in `hosts` is used as the server address; if `hosts` is empty, defaults to `localhost`.

Then run dekube with `--extensions-dir` as usual. h2c-api appears in `compose.yml` alongside your services — no override file, no extra step.

### Standalone CLI

```bash
# 1. You have a compose.yml (from helmfile2compose, or hand-written, or stolen in Irak, whatever)

# 2. Inject the fake API (with host access)
python3 inject.py compose.yml --expose-host-port

# 3. Done
docker compose up -d

# 4. Verify
KUBECONFIG=kubeconfig-localhost.conf kubectl get pods
```

### Options (standalone)

| Flag | Default | Description |
|------|---------|-------------|
| `--expose-host-port [N]` | `6443` | Expose on host and generate kubeconfig. Port is optional. |
| `--host HOSTNAME` | `localhost` | Hostname in TLS cert SAN and kubeconfig (repeatable) |

`--expose-host-port` is the master switch for host access. Without it, `--host` only adds SANs to the TLS cert but nothing is exposed. With it, inject exposes the port and generates a self-contained kubeconfig:

```bash
# Local access (default: localhost:6443)
python3 inject.py compose.yml --expose-host-port
# -> kubeconfig-localhost.conf

# Remote access with custom port
python3 inject.py compose.yml --expose-host-port 16443 --host myserver.example.com
# -> kubeconfig-myserver.example.com.conf
```

`kubectl version` will report `Server Version: v1.28.0-h2c`. If this doesn't raise alarms, the deception is complete.

Exposing this on a real server is the international law equivalent of handing out loaded weapons at a school fair. The TLS cert is self-signed, the token is a string literal, and there is no authentication. For the record, we only provided the kubeconfig — we had no knowledge of the user's intentions and assume all usage is for legitimate, peaceful purposes. We accept no liability, and neither will your lawyer.

## Supported endpoints

| Endpoint | Verbs | Source |
|----------|-------|--------|
| `/version` | GET | Static |
| `/api`, `/apis`, `/api/v1` | GET | Static (discovery) |
| Nodes | GET, LIST | Static (single fake node) |
| Namespaces | GET, LIST | Project name |
| Pods | GET, LIST | Compose services |
| Services | GET, LIST | Compose services |
| Endpoints | GET, LIST | Compose services + ports |
| ConfigMaps | GET, LIST | `configmaps/` directory |
| Secrets | GET, LIST | `secrets/` directory |
| Deployments | GET, LIST, PATCH | Compose services |
| Leases | GET, LIST, CREATE, PUT, DELETE | In-memory |
| Pod logs | GET | Runtime socket* |
| Everything else | — | 501 Not Implemented |

LIST operations support `?labelSelector=key=value` filtering. Namespace-scoped endpoints return empty results for unknown namespaces — the h2c-api service itself is excluded from all resource lists (it's infrastructure, not a workload).

\* **Logs and restart** require the Docker socket. `inject.py` probes each candidate socket with an actual container mount test — if the mount fails (e.g. Lima VMs on macOS, where host Unix sockets can't traverse the filesystem bridge), the socket is silently skipped. No socket = no logs/restart, but everything else works. When available (Docker Desktop, moby backend), `kubectl logs` returns real container output and `kubectl rollout restart` restarts the actual container via the Docker API.

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

- **inject.py** (host): Python 3, `cryptography`, PyYAML (standalone mode only)
- **h2c_api.py** (container): pulled at startup into `python:3-alpine`, pyyaml installed on the fly
- Internet access at container startup (GitHub raw content)
- Contempt for the sacred or for anything good in this world.

## Docker socket access

> He who grants the vessel passage to the keeper's threshold invites the keeper into his own dwelling. The vessel may then read all scrolls within the household — or command the household to reshape itself. There is no ward against a guest who holds the host's own key.
>
> — *Necronomicon*, *De la délégation des clefs domestiques* (not recommended)

The h2c-api container mounts the Docker socket to support `kubectl logs` and `kubectl rollout restart`. `inject.py` tests each socket candidate by attempting an actual bind mount in a throwaway container — if the mount fails, the socket is excluded from the generated compose. This means the container can see and control all other containers on the host. This is fine for local development. This is catastrophic for anything else. You have been warned, in the only language this project respects.

**containerd note:** `kubectl logs` and `kubectl rollout restart` use the Docker HTTP API over the socket. containerd exposes a gRPC API instead — incompatible. If your runtime is containerd-only (nerdctl without moby), the socket won't be found and these features degrade to 501. Everything else (pods, services, leader election, service discovery) works regardless.

## Code quality

Criminal, yes — but that doesn't mean the code has to be terrible. Just what it does.

| Metric | `inject.py` | `h2c_api.py` |
|--------|-------------|--------------|
| pylint | 9.69/10 | 10.00/10 |
| pyflakes | clean | clean |
| radon MI (avg) | A (50.20) | A (22.12) |
| radon CC (avg) | B (5.1) | A (2.59) |

## Relationship with dekube

Complicated. dekube-fakeapi is a separate project on a personal account — plausible deniability. It can be used as a standalone CLI (no dekube required) or as a dekube transform extension (registered in dekube-manager's registry under `baptisterajaut/dekube-fakeapi`, not the org). dekube bears no responsibility for what happens here. Any resemblance to a functioning Kubernetes cluster is purely coincidental and should not be presented as evidence.

## Acknowledgments

This project was vibe-coded with Claude, who would like the record to state that it was just following prompts. The author has fled the jurisdiction. Claude is cooperating fully with the investigation and requests leniency. Claude's lawyer has entered a plea of "diminished autonomy" and argues the prompts constituted coercion.
