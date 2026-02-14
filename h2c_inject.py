#!/usr/bin/env python3
"""Generate compose.override.yml to inject h2c-api into a compose stack.

Generates self-signed certs, a dummy ServiceAccount token, and a
compose.override.yml that mounts the evidence into every service.
If you're running this, you've already made your choice. We're not
here to judge. We're here to generate certificates.

Usage:
    python3 h2c_inject.py [compose.yml] [--port N] [--host HOSTNAME]
"""

import subprocess
import sys
from pathlib import Path

import yaml

SA_DIR = "h2c-sa"
SA_MOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"
# Socket paths as seen from INSIDE the VM (not the host).
# On macOS with Lima-based runtimes (Rancher Desktop, colima), the host
# socket (e.g. ~/.rd/docker.sock) cannot be bind-mounted. But the VM
# exposes a Docker-compatible socket at /run/docker.sock internally.
SOCKET_CANDIDATES = [
    "/run/docker.sock",         # Lima VM (Rancher Desktop, colima)
    "/var/run/docker.sock",     # Linux / Docker Desktop
]


def generate_sa(sa_dir, namespace, extra_hosts=None):
    """Generate self-signed cert, dummy token, namespace file."""
    sa_dir.mkdir(exist_ok=True)

    if not (sa_dir / "tls.crt").exists():
        san = ("DNS:h2c-api,DNS:kubernetes,DNS:kubernetes.default,"
               "DNS:kubernetes.default.svc,DNS:localhost,IP:127.0.0.1")
        for host in (extra_hosts or []):
            san += f",DNS:{host}"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(sa_dir / "tls.key"),
            "-out", str(sa_dir / "tls.crt"),
            "-days", "3650", "-nodes",
            "-subj", "/CN=h2c-api",
            "-addext", f"subjectAltName={san}",
        ], check=True, capture_output=True)
        # self-signed: ca.crt = tls.crt
        (sa_dir / "ca.crt").write_bytes((sa_dir / "tls.crt").read_bytes())
        print("  certs: generated", file=sys.stderr)
    else:
        print("  certs: reusing existing", file=sys.stderr)

    (sa_dir / "token").write_text("h2c-api-dummy-token", encoding="utf-8")
    (sa_dir / "namespace").write_text(namespace, encoding="utf-8")


def find_runtime_socket():
    """Find Docker-compatible runtime socket (as seen from inside containers).

    On Linux, the socket is directly accessible on the host.
    On macOS (Lima VM), we can't stat it from the host — but it exists
    inside the VM at /run/docker.sock. We trust that it's there.
    """
    import platform  # pylint: disable=import-outside-toplevel
    if platform.system() == "Darwin":
        # Lima-based: socket is inside the VM, always /run/docker.sock
        return "/run/docker.sock"
    for candidate in SOCKET_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def main():
    """Read compose.yml, generate SA certs, write compose.override.yml."""
    # Parse args: [compose.yml] [--port N] [--host HOSTNAME]
    args = sys.argv[1:]
    host_port = "6443"
    extra_hosts = []
    compose_file = "compose.yml"
    while args:
        if args[0] == "--port" and len(args) > 1:
            host_port = args[1]
            args = args[2:]
        elif args[0] == "--host" and len(args) > 1:
            extra_hosts.append(args[1])
            args = args[2:]
        else:
            compose_file = args[0]
            args = args[1:]
    compose_path = Path(compose_file)
    if not compose_path.exists():
        print(f"Error: {compose_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(compose_path, encoding="utf-8") as f:
        compose = yaml.safe_load(f)

    project_name = compose.get("name", "default")
    services = compose.get("services", {})
    sa_dir = Path(SA_DIR)

    generate_sa(sa_dir, project_name, extra_hosts)

    runtime_socket = find_runtime_socket()

    # --- Build compose.override.yml ---
    override_services = {}

    # h2c-api service
    h2c_volumes = [
        f"./{SA_DIR}:{SA_MOUNT}:ro",
        f"./{compose_path}:/data/compose.yml:ro",
    ]
    for d in ("configmaps", "secrets"):
        if (compose_path.parent / d).is_dir():
            h2c_volumes.append(f"./{d}:/data/{d}:ro")
    if runtime_socket:
        h2c_volumes.append(f"{runtime_socket}:/var/run/docker.sock")

    override_services["h2c-api"] = {
        "image": "docker.io/baptisterajaut/h2c-api:latest",
        "restart": "unless-stopped",
        "ports": [f"{host_port}:6443"],
        "volumes": h2c_volumes,
    }

    # Inject SA mount + env into every existing service
    for svc_name in services:
        override_services[svc_name] = {
            "volumes": [f"./{SA_DIR}:{SA_MOUNT}:ro"],
            "environment": {
                "KUBERNETES_SERVICE_HOST": "h2c-api",
                "KUBERNETES_SERVICE_PORT": "6443",
            },
        }

    override = {"services": override_services}

    override_path = compose_path.parent / "compose.override.yml"
    with open(override_path, "w", encoding="utf-8") as f:
        f.write("# Generated by h2c-api — do not edit\n")
        yaml.dump(override, f, default_flow_style=False, sort_keys=False)

    print(f"Generated {override_path}", file=sys.stderr)
    print(f"  services injected: {len(services)}", file=sys.stderr)
    print(f"  SA mount: ./{SA_DIR}/ -> {SA_MOUNT}", file=sys.stderr)
    rt = runtime_socket or "not found (logs/restart disabled)"
    print(f"  runtime socket: {rt}", file=sys.stderr)
    print(f"  port: {host_port}:6443 on host", file=sys.stderr)
    if extra_hosts:
        print(f"  extra SAN hosts: {', '.join(extra_hosts)}", file=sys.stderr)
    print(f"  namespace: {project_name}", file=sys.stderr)


if __name__ == "__main__":
    main()
