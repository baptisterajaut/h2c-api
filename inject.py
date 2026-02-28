#!/usr/bin/env python3
"""Inject dekube-fakeapi into a compose stack.

Dual-mode:
  - **Transform** — loaded by dekube as an extension, injects directly into
    compose_services. No override file, no manual step.
  - **Standalone CLI** — generates compose.override.yml + secrets/dekube-api/ certs.
    Same behavior as the original inject.py.

If you're running this, you've already made your choice. We're not
here to judge. We're here to generate certificates.

Usage (standalone):
    python3 inject.py [compose.yml] [--expose-host-port [N]] [--host HOSTNAME]
"""
# pylint: disable=import-outside-toplevel

import base64
import datetime
import sys
from pathlib import Path

SA_DIR = "secrets/dekube-api"
SA_MOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"
DEKUBE_API_IMAGE = "python:3-alpine"
DEKUBE_API_URL = ("https://raw.githubusercontent.com"
                  "/baptisterajaut/dekube-fakeapi/main/dekube_api.py")
SOCKET_CANDIDATES = [
    "/run/docker.sock",         # Linux / Lima VM internal
    "/var/run/docker.sock",     # Linux / Docker Desktop
    "~/.rd/docker.sock",        # Rancher Desktop (macOS)
    "~/.docker/run/docker.sock",  # Docker Desktop (macOS)
]


def _log(msg):
    print(f"  [fake-apiserver] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Shared logic (used by both modes)
# ---------------------------------------------------------------------------

def generate_sa(sa_dir, namespace, extra_hosts=None):
    """Generate self-signed cert (via cryptography), dummy token, namespace file."""
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import ipaddress

    sa_dir.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    san_entries = [
        x509.DNSName("dekube-api"),
        x509.DNSName("kubernetes"),
        x509.DNSName("kubernetes.default"),
        x509.DNSName("kubernetes.default.svc"),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
    ]
    for host in (extra_hosts or []):
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            san_entries.append(x509.DNSName(host))

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dekube-api")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .sign(key, hashes.SHA256())
    )

    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)

    (sa_dir / "tls.key").write_bytes(key_pem)
    (sa_dir / "tls.crt").write_bytes(cert_pem)
    (sa_dir / "ca.crt").write_bytes(cert_pem)  # self-signed: ca.crt = tls.crt

    (sa_dir / "token").write_text("dekube-api-dummy-token", encoding="utf-8")
    (sa_dir / "namespace").write_text(namespace, encoding="utf-8")


def _test_socket_mount(socket_path):
    """Test if a socket can actually be bind-mounted into a container.

    On macOS with Lima-based runtimes (Rancher Desktop, colima), the host
    socket exists but can't traverse the VM filesystem — nerdctl fails at
    container creation with 'operation not supported'. The only reliable
    test is to try.
    """
    import subprocess
    for runtime in ("docker", "nerdctl"):
        try:
            r = subprocess.run(
                [runtime, "run", "--rm",
                 "-v", f"{socket_path}:/tmp/sock",
                 DEKUBE_API_IMAGE, "test", "-S", "/tmp/sock"],
                capture_output=True, timeout=30,
            )
            return r.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return False


def find_runtime_socket():
    """Find Docker-compatible runtime socket that can be bind-mounted.

    Tries each candidate, expands ~ paths, and verifies with an actual
    container mount test. This catches Lima VMs where host Unix sockets
    exist but can't be bind-mounted into containers.
    """
    for raw in SOCKET_CANDIDATES:
        candidate = Path(raw).expanduser()
        if candidate.exists():
            if _test_socket_mount(str(candidate)):
                return str(candidate)
            _log(f"socket {candidate} exists but cannot be mounted, skipping")
    return None


def generate_kubeconfig(sa_dir, host, port, output_dir):
    """Generate a self-contained kubeconfig file for host access."""
    import yaml

    ca_b64 = base64.b64encode((sa_dir / "ca.crt").read_bytes()).decode()
    token = (sa_dir / "token").read_text(encoding="utf-8")
    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{
            "name": "dekube",
            "cluster": {
                "server": f"https://{host}:{port}",
                "certificate-authority-data": ca_b64,
            },
        }],
        "users": [{
            "name": "dekube",
            "user": {"token": token},
        }],
        "contexts": [{
            "name": "dekube",
            "context": {"cluster": "dekube", "user": "dekube"},
        }],
        "current-context": "dekube",
    }
    kubeconfig_path = output_dir / f"kubeconfig-{host}.conf"
    with open(kubeconfig_path, "w", encoding="utf-8") as f:
        f.write("# Generated by dekube-api — do not edit\n")
        yaml.dump(kubeconfig, f, default_flow_style=False, sort_keys=False)
    return kubeconfig_path


def build_dekube_api_service(sa_dir, compose_path, runtime_socket, host_port=None):
    """Return the dekube-api compose service dict."""
    api_volumes = [
        f"./{sa_dir}:{SA_MOUNT}:ro",
        f"./{compose_path}:/data/compose.yml:ro",
    ]
    for d in ("configmaps", "secrets"):
        if (compose_path.parent / d).is_dir():
            api_volumes.append(f"./{d}:/data/{d}:ro")
    if runtime_socket:
        api_volumes.append(f"{runtime_socket}:/var/run/docker.sock")

    svc = {
        "image": DEKUBE_API_IMAGE,
        "restart": "unless-stopped",
        "command": [
            "sh", "-c",
            f"pip install --no-cache-dir -q pyyaml"
            f" && wget -qO /tmp/dekube_api.py {DEKUBE_API_URL}"
            f" && python3 /tmp/dekube_api.py",
        ],
        "volumes": api_volumes,
    }
    if host_port:
        svc["ports"] = [f"{host_port}:6443"]
    return svc


def build_injection(sa_dir):
    """Return the {volumes, environment} dict to merge into each service."""
    return {
        "volumes": [f"./{sa_dir}:{SA_MOUNT}:ro"],
        "environment": {
            "KUBERNETES_SERVICE_HOST": "dekube-api",
            "KUBERNETES_SERVICE_PORT": "6443",
        },
    }


# ---------------------------------------------------------------------------
# Transform class (dekube extension mode)
# ---------------------------------------------------------------------------

class DekubeApiInject:  # pylint: disable=too-few-public-methods  # contract: one class, one method
    """Inject fake Kubernetes API server into compose stack."""

    name = "fake-apiserver"
    priority = 9000  # after everything, including fix-permissions (8000)

    def transform(self, compose_services, ingress_entries, ctx):  # pylint: disable=unused-argument  # Transform contract signature
        """Add dekube-api service and inject SA mount + env into every service."""
        cfg = ctx.config.get("dekube-api") or {}
        extra_hosts = cfg.get("hosts") or []
        host_port = cfg.get("expose-host-port")

        namespace = ctx.config.get("name", "default")
        sa_dir = Path(ctx.output_dir) / SA_DIR

        generate_sa(sa_dir, namespace, extra_hosts)

        runtime_socket = find_runtime_socket()

        # Compose path — dekube writes compose.yml in output_dir
        compose_path = Path(ctx.output_dir) / ctx.config.get("compose_file", "compose.yml")

        # Add dekube-api service
        compose_services["dekube-api"] = build_dekube_api_service(
            Path(SA_DIR), compose_path, runtime_socket, host_port)
        _log(f"added dekube-api service (socket: {runtime_socket or 'not found'})")

        # Inject SA + env into every existing service
        injection = build_injection(Path(SA_DIR))
        injected = 0
        for svc_name, svc in compose_services.items():
            if svc_name == "dekube-api":
                continue
            svc.setdefault("volumes", []).extend(injection["volumes"])
            svc.setdefault("environment", {}).update(injection["environment"])
            injected += 1
        _log(f"injected SA mount + env into {injected} services")

        # Kubeconfig if host port requested
        if host_port:
            kube_host = extra_hosts[0] if extra_hosts else "localhost"
            kc_path = generate_kubeconfig(
                sa_dir, kube_host, host_port, Path(ctx.output_dir))
            _log(f"kubeconfig: {kc_path}")


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

def main():
    """Read compose.yml, generate SA certs, write compose.override.yml."""
    import yaml

    # Parse args: [compose.yml] [--expose-host-port [N]] [--host HOSTNAME]
    args = sys.argv[1:]
    host_port = None
    extra_hosts = []
    compose_file = "compose.yml"
    while args:
        if args[0] == "--expose-host-port":
            host_port = "6443"
            if len(args) > 1 and args[1].isdigit():
                host_port = args[1]
                args = args[1:]
            args = args[1:]
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

    override_services["dekube-api"] = build_dekube_api_service(
        sa_dir, compose_path, runtime_socket, host_port)

    injection = build_injection(sa_dir)
    for svc_name in services:
        override_services[svc_name] = {
            "volumes": list(injection["volumes"]),
            "environment": dict(injection["environment"]),
        }

    override = {"services": override_services}

    override_path = compose_path.parent / "compose.override.yml"
    with open(override_path, "w", encoding="utf-8") as f:
        f.write("# Generated by dekube-api — do not edit\n")
        yaml.dump(override, f, default_flow_style=False, sort_keys=False)

    kubeconfig_path = None
    if host_port:
        kube_host = extra_hosts[0] if extra_hosts else "localhost"
        kubeconfig_path = generate_kubeconfig(
            sa_dir, kube_host, host_port, compose_path.parent)

    print(f"Generated {override_path}", file=sys.stderr)
    print(f"  services injected: {len(services)}", file=sys.stderr)
    print(f"  SA mount: ./{SA_DIR}/ -> {SA_MOUNT}", file=sys.stderr)
    rt = runtime_socket or "not found (logs/restart disabled)"
    print(f"  runtime socket: {rt}", file=sys.stderr)
    port_info = f"{host_port}:6443 on host" if host_port else "not exposed"
    print(f"  port: {port_info}", file=sys.stderr)
    if kubeconfig_path:
        print(f"  kubeconfig: {kubeconfig_path}", file=sys.stderr)
    if extra_hosts:
        print(f"  extra SAN hosts: {', '.join(extra_hosts)}", file=sys.stderr)
    print(f"  namespace: {project_name}", file=sys.stderr)
    print(f"\n  Remember to .gitignore: {SA_DIR} compose.override.yml kubeconfig-*.conf"
          "\n  Also consider a fake identity. Failing to do so may greet"
          " you with consequences.", file=sys.stderr)


if __name__ == "__main__":
    main()
