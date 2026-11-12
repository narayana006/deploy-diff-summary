import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from deploy_diff_summary import cli
from deploy_diff_summary.registry import (
    Registry,
    RegistryError,
    compare_tags,
    parse_image_name,
)
from deploy_diff_summary.render import render_image_json, render_image_markdown

INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class FakeRegistry:
    """An in-memory OCI registry: tags -> manifests -> config blobs."""

    def __init__(self, token: str | None = None):
        self.objects: dict[str, tuple[str, bytes]] = {}  # digest -> (media type, body)
        self.tags: dict[str, str] = {}
        self.token = token
        self.requests: list[str] = []

    def put(self, media: str, doc: dict) -> str:
        body = json.dumps(doc).encode()
        self.objects[digest(body)] = (media, body)
        return digest(body)

    def add_image(self, tag: str, labels: dict, layers: list[str], created: str, multi: bool = False) -> str:
        cfg = self.put("application/json", {"created": created, "os": "linux", "architecture": "amd64",
                                            "config": {"Labels": labels}})
        man = self.put(MANIFEST, {"mediaType": MANIFEST, "config": {"digest": cfg},
                                  "layers": [{"digest": f"sha256:{name * 64}"[:71], "size": 1048576}
                                             for name in layers]})
        if multi:
            arm = self.put(MANIFEST, {"mediaType": MANIFEST, "layers": []})
            man = self.put(INDEX, {"mediaType": INDEX, "manifests": [
                {"digest": arm, "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
                {"digest": man, "platform": {"os": "linux", "architecture": "amd64"}}]})
        self.tags[tag] = man
        return man

    def __call__(self, url: str, headers: dict):
        self.requests.append(url)
        parts = urlsplit(url)
        if parts.path == "/token":
            q = parse_qs(parts.query)
            assert q["scope"] == ["repository:acme/orders:pull"] and q["service"] == ["reg.example"]
            return 200, {}, json.dumps({"token": self.token}).encode()
        if self.token and headers.get("Authorization") != f"Bearer {self.token}":
            return 401, {"www-authenticate": 'Bearer realm="https://reg.example/token",service="reg.example"'}, b""
        prefix = "/v2/acme/orders/"
        assert parts.path.startswith(prefix), parts.path
        _kind, ref = parts.path[len(prefix):].split("/", 1)
        d = self.tags.get(ref, ref)
        if d not in self.objects:
            return 404, {}, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}'
        media, body = self.objects[d]
        return 200, {"content-type": media, "docker-content-digest": d}, body


@pytest.fixture
def registry():
    reg = FakeRegistry()
    reg.add_image("1.41.0", {"org.opencontainers.image.revision": "8e24acd", "team": "orders"},
                  ["a", "b", "c"], "2026-10-01T10:00:00Z")
    reg.add_image("1.42.0", {"org.opencontainers.image.revision": "aa6199d", "release": "1.42.0"},
                  ["a", "b", "d", "e"], "2026-11-01T10:00:00Z", multi=True)
    return reg


def test_parse_image_name():
    assert parse_image_name("nginx") == ("registry-1.docker.io", "library/nginx")
    assert parse_image_name("grafana/grafana") == ("registry-1.docker.io", "grafana/grafana")
    assert parse_image_name("docker.io/library/redis") == ("registry-1.docker.io", "library/redis")
    assert parse_image_name("ghcr.io/acme/orders") == ("ghcr.io", "acme/orders")
    assert parse_image_name("localhost:5000/app") == ("localhost:5000", "app")
    with pytest.raises(RegistryError):
        parse_image_name("ghcr.io/acme/orders:1.0")


def test_local_registries_use_plain_http():
    assert Registry("localhost:5000/app").scheme == "http"
    assert Registry("ghcr.io/acme/orders").scheme == "https"


def test_compare_tags_reports_digests_labels_layers_and_platform(registry):
    c = compare_tags("reg.example/acme/orders", "1.41.0", "1.42.0", fetch=registry)
    assert not c.same_image
    assert c.old.digest == registry.tags["1.41.0"] and c.new.digest == registry.tags["1.42.0"]
    assert c.new.platform == "linux/amd64" and c.old.platform == "linux/amd64"
    assert len(c.new.layers) == 4 and c.new.size == 4 * 1048576
    assert c.shared_layers == 2
    assert {(k.key, k.kind, k.old, k.new) for k in c.labels} == {
        ("org.opencontainers.image.revision", "changed", "8e24acd", "aa6199d"),
        ("team", "removed", "orders", None),
        ("release", "added", None, "1.42.0"),
    }
    md = render_image_markdown(c)
    assert "# Image diff: reg.example/acme/orders 1.41.0 -> 1.42.0" in md
    assert "| Layers | 3 (3.0 MB) | 4 (4.0 MB) |" in md
    assert "2 of 4 layers in `1.42.0` are shared with `1.41.0`." in md
    assert "- changed `org.opencontainers.image.revision`: `8e24acd` -> `aa6199d`" in md
    assert json.loads(render_image_json(c))["shared_layers"] == 2


def test_same_digest_and_lookup_by_digest(registry):
    d = registry.tags["1.41.0"]
    c = compare_tags("reg.example/acme/orders", "1.41.0", d, fetch=registry)
    assert c.same_image and c.labels == []
    assert "nothing changed" in render_image_markdown(c)


def test_platform_selection(registry):
    c = compare_tags("reg.example/acme/orders", "1.42.0", "1.42.0", platform="linux/arm64", fetch=registry)
    assert c.new.platform == "linux/arm64" and c.new.layers == []
    with pytest.raises(RegistryError, match="no windows/amd64 image"):
        compare_tags("reg.example/acme/orders", "1.41.0", "1.42.0", platform="windows/amd64", fetch=registry)


def test_anonymous_token_flow():
    reg = FakeRegistry(token="pull-only")
    reg.add_image("v1", {}, ["a"], "2026-01-01T00:00:00Z")
    c = compare_tags("reg.example/acme/orders", "v1", "v1", fetch=reg)
    assert c.same_image
    assert sum(u.startswith("https://reg.example/token?") for u in reg.requests) == 1


def test_errors_for_unknown_tag_and_refused_access(registry):
    with pytest.raises(RegistryError, match="not found"):
        compare_tags("reg.example/acme/orders", "1.41.0", "9.9.9", fetch=registry)
    def refuse(url, headers):
        return (403, {}, b"")
    with pytest.raises(RegistryError, match="only public images"):
        compare_tags("reg.example/acme/orders", "a", "b", fetch=refuse)
    def basic(url, headers):
        return (401, {"www-authenticate": 'Basic realm="x"'}, b"")
    with pytest.raises(RegistryError, match="Basic"):
        compare_tags("reg.example/acme/orders", "a", "b", fetch=basic)


def test_cli_against_local_http_registry(registry, tmp_path, capsys):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status, headers, body = registry(f"http://reg.example{self.path}", dict(self.headers))
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        # The fake only serves acme/orders, so serve it under 127.0.0.1:<port>/acme/orders.
        image = f"127.0.0.1:{server.server_port}/acme/orders"
        out = tmp_path / "image.md"
        assert cli.main(["--image", image, "--from", "1.41.0", "--to", "1.42.0", "-o", str(out)]) == 0
        assert "2 of 4 layers" in out.read_text()
        assert cli.main(["--image", image, "--from", "1.41.0", "--to", "nope"]) == 2
        assert "not found" in capsys.readouterr().err
    finally:
        server.shutdown()
