#!/usr/bin/env python3
"""h2c-api: fake Kubernetes API server. You should not be reading this.

If you are reading this, something has gone wrong — either in your
infrastructure, or in your life choices. This file impersonates a
Kubernetes control plane using a compose.yml and wishful thinking.

Pods are services. Services are services. Secrets are files on disk.
The leader election has one candidate. The token is a string literal.
No auth, no watch, no webhooks. 501 for everything else.
"""
# pylint: disable=missing-function-docstring,unused-argument
# Route handlers have a fixed signature (state, match, body, qs) and are
# self-documenting by their route pattern. Suppressing both globally.

import base64
import http.client
import json
import os
import re
import socket
import ssl
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote

import yaml


# ---------------------------------------------------------------------------
# Container runtime client (Docker-compatible API over Unix socket)
# ---------------------------------------------------------------------------

class _UnixConnection(http.client.HTTPConnection):
    """HTTP connection over a Unix domain socket."""

    def __init__(self, socket_path):
        super().__init__("localhost")
        self._socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._socket_path)


class RuntimeClient:
    """Minimal Docker-compatible API client for logs and restart."""

    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.available = Path(socket_path).exists()

    def _request(self, method, path):
        try:
            conn = _UnixConnection(self.socket_path)
            conn.request(method, path)
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
            return resp.status, data
        except (ConnectionRefusedError, OSError) as exc:
            print(f"[h2c-api] runtime socket error: {exc}", file=sys.stderr)
            return 0, b""

    def find_container(self, project, service):
        """Find container ID by compose project + service labels."""
        filters = json.dumps({"label": [
            f"com.docker.compose.project={project}",
            f"com.docker.compose.service={service}",
        ]})
        status, data = self._request(
            "GET", f"/containers/json?filters={quote(filters)}")
        if status != 200:
            return None
        containers = json.loads(data)
        return containers[0]["Id"] if containers else None

    def get_logs(self, container_id, tail="100"):
        """Get container logs (demuxed)."""
        status, data = self._request(
            "GET",
            f"/containers/{container_id}/logs"
            f"?stdout=1&stderr=1&tail={tail}&timestamps=1")
        if status != 200:
            return None
        return _demux_docker_logs(data)

    def restart_container(self, container_id):
        """Restart a container. Returns True on success."""
        status, _ = self._request("POST", f"/containers/{container_id}/restart")
        return status == 204


def _demux_docker_logs(data):
    """Parse Docker multiplexed log stream into plain text."""
    output = []
    offset = 0
    while offset + 8 <= len(data):
        size = int.from_bytes(data[offset + 4:offset + 8], "big")
        offset += 8
        output.append(data[offset:offset + size])
        offset += size
    return b"".join(output)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class State:  # pylint: disable=too-few-public-methods,too-many-instance-attributes
    """Server state loaded from compose.yml and generated file directories."""

    def __init__(self, compose_path, data_dir, runtime_socket):
        with open(compose_path, encoding="utf-8") as f:
            self.compose = yaml.safe_load(f)
        self.project_name = self.compose.get("name", "default")
        self.namespace = self.project_name
        self.services = self.compose.get("services", {})
        self.configmaps = self._load_file_resources(data_dir, "configmaps")
        self.secrets = self._load_file_resources(data_dir, "secrets")
        self.leases = {}  # in-memory: {name: lease_object}
        self.runtime = RuntimeClient(runtime_socket)

    @staticmethod
    def _load_file_resources(base_dir, kind):
        """Load configmaps/ or secrets/ directory -> {name: {key: value}}."""
        resources = {}
        resource_dir = Path(base_dir) / kind
        if not resource_dir.is_dir():
            return resources
        for name_dir in sorted(resource_dir.iterdir()):
            if not name_dir.is_dir():
                continue
            data = {}
            for key_file in sorted(name_dir.iterdir()):
                if key_file.is_file():
                    data[key_file.name] = key_file.read_text(encoding="utf-8")
            if data:
                resources[name_dir.name] = data
        return resources


# ---------------------------------------------------------------------------
# K8s object builders
# ---------------------------------------------------------------------------

def _extract_ports(svc):
    """Extract container port numbers from a compose service."""
    ports = []
    for p in svc.get("ports", []):
        if isinstance(p, dict):
            ports.append(p.get("target", p.get("published", 0)))
        elif isinstance(p, str):
            # "8080:80" or "80"
            parts = p.split(":")
            ports.append(int(parts[-1].split("/")[0]))
        elif isinstance(p, int):
            ports.append(p)
    return ports


def k8s_list(kind, api_version, items):
    return {
        "kind": kind,
        "apiVersion": api_version,
        "metadata": {"resourceVersion": "1"},
        "items": items,
    }


def k8s_status(code, message):
    return {"kind": "Status", "apiVersion": "v1", "status": "Failure",
            "message": message, "reason": "NotFound", "code": code}


def make_namespace(name):
    return {
        "apiVersion": "v1", "kind": "Namespace",
        "metadata": {"name": name, "labels": {"kubernetes.io/metadata.name": name}},
        "status": {"phase": "Active"},
    }


def make_pod(name, svc, namespace):
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": {"app": name}},
        "spec": {
            "containers": [{
                "name": name,
                "image": svc.get("image", "unknown"),
                "ports": [{"containerPort": p} for p in _extract_ports(svc)],
            }],
            "nodeName": "h2c-node",
        },
        "status": {
            "phase": "Running",
            "podIP": name,  # compose DNS: service name = hostname
            "hostIP": "127.0.0.1",
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def make_service(name, svc, namespace):
    ports = _extract_ports(svc)
    return {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": name, "namespace": namespace, "labels": {"app": name}},
        "spec": {
            "type": "ClusterIP",
            "clusterIP": f"10.96.{hash(name) % 256}.{hash(name + 'x') % 254 + 1}",
            "ports": [{"port": p, "targetPort": p, "protocol": "TCP"} for p in ports],
            "selector": {"app": name},
        },
    }


def make_endpoints(name, svc, namespace):
    ports = _extract_ports(svc)
    return {
        "apiVersion": "v1", "kind": "Endpoints",
        "metadata": {"name": name, "namespace": namespace},
        "subsets": [{
            "addresses": [{"ip": name, "hostname": name}],
            "ports": [{"port": p, "protocol": "TCP"} for p in ports],
        }] if ports else [],
    }


def make_configmap(name, data, namespace):
    return {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": namespace},
        "data": data,
    }


def make_secret(name, data, namespace):
    return {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": "Opaque",
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
    }


def make_lease(name, namespace, body=None):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lease = {
        "apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
        "metadata": {
            "name": name, "namespace": namespace,
            "resourceVersion": str(int(time.time())),
            "creationTimestamp": now,
        },
        "spec": {},
    }
    if body:
        if "spec" in body:
            lease["spec"] = body["spec"]
        for k in ("labels", "annotations"):
            if k in body.get("metadata", {}):
                lease["metadata"][k] = body["metadata"][k]
    return lease


def make_deployment(name, svc, namespace):
    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {
            "name": name, "namespace": namespace,
            "labels": {"app": name},
            "annotations": {},
            "resourceVersion": str(int(time.time())),
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}, "annotations": {}},
                "spec": {
                    "containers": [{
                        "name": name,
                        "image": svc.get("image", "unknown"),
                    }],
                },
            },
        },
        "status": {"replicas": 1, "readyReplicas": 1, "availableReplicas": 1},
    }


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

ROUTES = []


def route(method, pattern):
    """Decorator: register a (method, regex) -> handler."""
    def decorator(func):
        ROUTES.append((method, re.compile(pattern), func))
        return func
    return decorator


# --- Discovery (required for client-go / kubernetes-python to boot) ---

@route("GET", r"/version$")
def handle_version(state, match, body, qs):
    return 200, {
        "major": "1", "minor": "28",
        "gitVersion": "v1.28.0-h2c", "platform": "linux/amd64",
    }


@route("GET", r"/api$")
def handle_api(state, match, body, qs):
    return 200, {
        "kind": "APIVersions", "versions": ["v1"],
        "serverAddressByClientCIDRs": [
            {"clientCIDR": "0.0.0.0/0", "serverAddress": "h2c-api:6443"},
        ],
    }


@route("GET", r"/api/v1$")
def handle_api_v1(state, match, body, qs):
    resources = [
        {"name": "namespaces", "namespaced": False, "kind": "Namespace",
         "verbs": ["get", "list"]},
        {"name": "pods", "namespaced": True, "kind": "Pod",
         "verbs": ["get", "list"]},
        {"name": "pods/log", "namespaced": True, "kind": "Pod",
         "verbs": ["get"]},
        {"name": "services", "namespaced": True, "kind": "Service",
         "verbs": ["get", "list"]},
        {"name": "endpoints", "namespaced": True, "kind": "Endpoints",
         "verbs": ["get", "list"]},
        {"name": "configmaps", "namespaced": True, "kind": "ConfigMap",
         "verbs": ["get", "list"]},
        {"name": "secrets", "namespaced": True, "kind": "Secret",
         "verbs": ["get", "list"]},
    ]
    return 200, {"kind": "APIResourceList", "groupVersion": "v1", "resources": resources}


@route("GET", r"/apis$")
def handle_apis(state, match, body, qs):
    return 200, {
        "kind": "APIGroupList",
        "groups": [
            {
                "name": "apps",
                "versions": [{"groupVersion": "apps/v1", "version": "v1"}],
                "preferredVersion": {"groupVersion": "apps/v1", "version": "v1"},
            },
            {
                "name": "coordination.k8s.io",
                "versions": [{"groupVersion": "coordination.k8s.io/v1", "version": "v1"}],
                "preferredVersion": {"groupVersion": "coordination.k8s.io/v1", "version": "v1"},
            },
        ],
    }


@route("GET", r"/apis/apps/v1$")
def handle_apps_v1(state, match, body, qs):
    resources = [
        {"name": "deployments", "namespaced": True, "kind": "Deployment",
         "verbs": ["get", "list", "patch", "update"]},
    ]
    return 200, {"kind": "APIResourceList", "groupVersion": "apps/v1",
                 "resources": resources}


@route("GET", r"/apis/coordination\.k8s\.io/v1$")
def handle_coordination(state, match, body, qs):
    resources = [
        {"name": "leases", "namespaced": True, "kind": "Lease",
         "verbs": ["create", "delete", "get", "list", "update"]},
    ]
    return 200, {"kind": "APIResourceList", "groupVersion": "coordination.k8s.io/v1",
                 "resources": resources}


# --- Core: namespaces ---

@route("GET", r"/api/v1/namespaces$")
def handle_list_ns(state, match, body, qs):
    items = [make_namespace(state.namespace), make_namespace("default"),
             make_namespace("kube-system")]
    return 200, k8s_list("NamespaceList", "v1", items)


@route("GET", r"/api/v1/namespaces/(?P<ns>[^/]+)$")
def handle_get_ns(state, match, body, qs):
    ns = match.group("ns")
    if ns in (state.namespace, "default", "kube-system"):
        return 200, make_namespace(ns)
    return 404, k8s_status(404, f"namespaces \"{ns}\" not found")


# --- Core: pods ---

@route("GET", r"/api/v1/namespaces/(?P<ns>[^/]+)/pods$")
def handle_list_pods(state, match, body, qs):
    items = [make_pod(n, s, state.namespace) for n, s in state.services.items()]
    return 200, k8s_list("PodList", "v1", items)


@route("GET", r"/api/v1/namespaces/(?P<ns>[^/]+)/pods/(?P<name>[^/]+)$")
def handle_get_pod(state, match, body, qs):
    name = match.group("name")
    if name in state.services:
        return 200, make_pod(name, state.services[name], state.namespace)
    return 404, k8s_status(404, f"pods \"{name}\" not found")


# --- Core: pod logs (via runtime socket) ---

@route("GET", r"/api/v1/namespaces/(?P<ns>[^/]+)/pods/(?P<name>[^/]+)/log$")
def handle_pod_log(state, match, body, qs):
    name = match.group("name")
    if name not in state.services:
        return 404, k8s_status(404, f"pods \"{name}\" not found")
    if not state.runtime.available:
        return 501, k8s_status(501, "runtime socket not mounted")
    container_id = state.runtime.find_container(state.project_name, name)
    if not container_id:
        return 404, k8s_status(404, f"container for pod \"{name}\" not found")
    tail = qs.get("tailLines", ["100"])[0]
    log_bytes = state.runtime.get_logs(container_id, tail=tail)
    if log_bytes is None:
        return 500, k8s_status(500, "failed to retrieve logs")
    return 200, log_bytes, "text/plain"


# --- Core: services ---

@route("GET", r"/api/v1/namespaces/(?P<ns>[^/]+)/services$")
def handle_list_svc(state, match, body, qs):
    items = [make_service(n, s, state.namespace) for n, s in state.services.items()]
    return 200, k8s_list("ServiceList", "v1", items)


@route("GET", r"/api/v1/namespaces/(?P<ns>[^/]+)/services/(?P<name>[^/]+)$")
def handle_get_svc(state, match, body, qs):
    name = match.group("name")
    if name in state.services:
        return 200, make_service(name, state.services[name], state.namespace)
    return 404, k8s_status(404, f"services \"{name}\" not found")


# --- Core: endpoints ---

@route("GET", r"/api/v1/namespaces/(?P<ns>[^/]+)/endpoints$")
def handle_list_ep(state, match, body, qs):
    items = [make_endpoints(n, s, state.namespace) for n, s in state.services.items()]
    return 200, k8s_list("EndpointsList", "v1", items)


# --- Core: configmaps ---

@route("GET", r"/api/v1/namespaces/(?P<ns>[^/]+)/configmaps$")
def handle_list_cm(state, match, body, qs):
    items = [make_configmap(n, d, state.namespace) for n, d in state.configmaps.items()]
    return 200, k8s_list("ConfigMapList", "v1", items)


@route("GET", r"/api/v1/namespaces/(?P<ns>[^/]+)/configmaps/(?P<name>[^/]+)$")
def handle_get_cm(state, match, body, qs):
    name = match.group("name")
    if name in state.configmaps:
        return 200, make_configmap(name, state.configmaps[name], state.namespace)
    return 404, k8s_status(404, f"configmaps \"{name}\" not found")


# --- Core: secrets ---

@route("GET", r"/api/v1/namespaces/(?P<ns>[^/]+)/secrets$")
def handle_list_secret(state, match, body, qs):
    items = [make_secret(n, d, state.namespace) for n, d in state.secrets.items()]
    return 200, k8s_list("SecretList", "v1", items)


@route("GET", r"/api/v1/namespaces/(?P<ns>[^/]+)/secrets/(?P<name>[^/]+)$")
def handle_get_secret(state, match, body, qs):
    name = match.group("name")
    if name in state.secrets:
        return 200, make_secret(name, state.secrets[name], state.namespace)
    return 404, k8s_status(404, f"secrets \"{name}\" not found")


# --- Deployments (apps/v1) ---

@route("GET", r"/apis/apps/v1/namespaces/(?P<ns>[^/]+)/deployments$")
def handle_list_deploy(state, match, body, qs):
    items = [make_deployment(n, s, state.namespace) for n, s in state.services.items()]
    return 200, k8s_list("DeploymentList", "apps/v1", items)


@route("GET", r"/apis/apps/v1/namespaces/(?P<ns>[^/]+)/deployments/(?P<name>[^/]+)$")
def handle_get_deploy(state, match, body, qs):
    name = match.group("name")
    if name in state.services:
        return 200, make_deployment(name, state.services[name], state.namespace)
    return 404, k8s_status(404, f"deployments.apps \"{name}\" not found")


@route("PATCH", r"/apis/apps/v1/namespaces/(?P<ns>[^/]+)/deployments/(?P<name>[^/]+)$")
def handle_patch_deploy(state, match, body, qs):
    name = match.group("name")
    if name not in state.services:
        return 404, k8s_status(404, f"deployments.apps \"{name}\" not found")
    # Restart the actual container via runtime socket
    restarted = False
    if state.runtime.available:
        container_id = state.runtime.find_container(state.project_name, name)
        if container_id:
            restarted = state.runtime.restart_container(container_id)
    deploy = make_deployment(name, state.services[name], state.namespace)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    deploy["metadata"]["annotations"]["kubectl.kubernetes.io/restartedAt"] = now
    if not restarted:
        print(f"[h2c-api] WARN: could not restart container for {name}", file=sys.stderr)
    return 200, deploy


# --- Leases (leader election stub) ---

@route("GET", r"/apis/coordination\.k8s\.io/v1/namespaces/(?P<ns>[^/]+)/leases$")
def handle_list_leases(state, match, body, qs):
    return 200, k8s_list("LeaseList", "coordination.k8s.io/v1", list(state.leases.values()))


@route("GET", r"/apis/coordination\.k8s\.io/v1/namespaces/(?P<ns>[^/]+)/leases/(?P<name>[^/]+)$")
def handle_get_lease(state, match, body, qs):
    name = match.group("name")
    if name in state.leases:
        return 200, state.leases[name]
    return 404, k8s_status(404, f"leases.coordination.k8s.io \"{name}\" not found")


@route("POST", r"/apis/coordination\.k8s\.io/v1/namespaces/(?P<ns>[^/]+)/leases$")
def handle_create_lease(state, match, body, qs):
    name = body.get("metadata", {}).get("name", "")
    if not name:
        return 400, k8s_status(400, "metadata.name is required")
    if name in state.leases:
        return 409, k8s_status(409, f"leases.coordination.k8s.io \"{name}\" already exists")
    lease = make_lease(name, state.namespace, body)
    state.leases[name] = lease
    return 201, lease


@route("PUT", r"/apis/coordination\.k8s\.io/v1/namespaces/(?P<ns>[^/]+)/leases/(?P<name>[^/]+)$")
def handle_update_lease(state, match, body, qs):
    name = match.group("name")
    lease = make_lease(name, state.namespace, body)
    state.leases[name] = lease
    return 200, lease


@route("DELETE", r"/apis/coordination\.k8s\.io/v1/namespaces/(?P<ns>[^/]+)/leases/(?P<name>[^/]+)$")
def handle_delete_lease(state, match, body, qs):
    name = match.group("name")
    lease = state.leases.pop(name, None)
    if lease:
        return 200, lease
    return 404, k8s_status(404, f"leases.coordination.k8s.io \"{name}\" not found")


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    """Dispatch incoming requests to registered route handlers."""

    state = None

    # pylint: disable=invalid-name,multiple-statements
    def do_GET(self):     self._handle("GET")
    def do_POST(self):    self._handle("POST")
    def do_PUT(self):     self._handle("PUT")
    def do_PATCH(self):   self._handle("PATCH")
    def do_DELETE(self):  self._handle("DELETE")
    # pylint: enable=invalid-name,multiple-statements

    def _handle(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        # Read body for write methods
        body = {}
        if method in ("POST", "PUT", "PATCH"):
            length = int(self.headers.get("Content-Length", 0))
            if length:
                try:
                    body = json.loads(self.rfile.read(length))
                except (json.JSONDecodeError, ValueError):
                    body = {}

        # Watch = not supported
        if qs.get("watch", [""])[0].lower() == "true":
            self._respond(501, k8s_status(501, "watch not supported by h2c-api"))
            return

        # Match route
        for route_method, pattern, handler in ROUTES:
            if method != route_method:
                continue
            m = pattern.match(path)
            if m:
                result = handler(self.state, m, body, qs)
                if len(result) == 3:
                    code, resp, content_type = result
                    self._respond(code, resp, content_type)
                else:
                    code, resp = result
                    self._respond(code, resp)
                return

        self._respond(501, k8s_status(501, f"{method} {path} not implemented"))

    def _respond(self, code, body, content_type="application/json"):
        if content_type == "application/json":
            data = json.dumps(body).encode()
        else:
            data = body if isinstance(body, bytes) else str(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _fmt, *args):  # pylint: disable=arguments-differ
        msg = args[1] if len(args) > 1 else args[0]
        sys.stderr.write(f"[h2c-api] {self.requestline} -> {msg}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    compose_path = os.environ.get("H2C_COMPOSE", "/data/compose.yml")
    data_dir = os.environ.get("H2C_DATA_DIR", "/data")
    port = int(os.environ.get("H2C_PORT", "6443"))
    runtime_socket = os.environ.get("H2C_RUNTIME_SOCKET", "/var/run/docker.sock")

    if not Path(compose_path).exists():
        print(f"Error: {compose_path} not found", file=sys.stderr)
        sys.exit(1)

    state = State(compose_path, data_dir, runtime_socket)
    Handler.state = state

    print(f"h2c-api serving on :{port}", file=sys.stderr)
    print(f"  project:    {state.project_name}", file=sys.stderr)
    print(f"  services:   {len(state.services)}", file=sys.stderr)
    print(f"  configmaps: {len(state.configmaps)}", file=sys.stderr)
    print(f"  secrets:    {len(state.secrets)}", file=sys.stderr)
    rt = "connected" if state.runtime.available else "unavailable"
    print(f"  runtime:    {rt}", file=sys.stderr)

    server = HTTPServer(("0.0.0.0", port), Handler)

    # TLS — serve HTTPS if certs are available (generated by h2c_inject.py)
    sa_path = Path(os.environ.get("H2C_SA_DIR",
                                  "/var/run/secrets/kubernetes.io/serviceaccount"))
    cert_file = sa_path / "tls.crt"
    key_file = sa_path / "tls.key"
    if cert_file.exists() and key_file.exists():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_file), str(key_file))
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        print("  tls:        enabled", file=sys.stderr)
    else:
        print("  tls:        disabled (no cert found)", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
