"""Compare two tags (or digests) of one container image in an OCI / Docker v2 registry.

Read-only: only HTTP GET requests for manifests, config blobs and anonymous pull tokens.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from .parsers import KeyChange, diff_keys

MANIFEST_TYPES = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
INDEX_TYPES = ("application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json")

# fetch(url, headers) -> (status, response headers with lower-case names, body)
Fetch = Callable[[str, dict], "tuple[int, dict, bytes]"]


class RegistryError(Exception):
    pass


def urllib_fetch(url: str, headers: dict) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()
    except urllib.error.URLError as exc:
        raise RegistryError(f"cannot reach {urllib.parse.urlsplit(url).netloc}: {exc.reason}") from exc


def parse_image_name(name: str) -> tuple[str, str]:
    """'nginx' -> ('registry-1.docker.io', 'library/nginx'); 'ghcr.io/a/b' -> ('ghcr.io', 'a/b')."""
    name = name.strip()
    if "@" in name or ":" in name.rsplit("/", 1)[-1]:
        raise RegistryError(f"give the image name without a tag or digest: {name!r}")
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        host, repo = first, rest
    else:
        host, repo = "docker.io", name
    if host in ("docker.io", "index.docker.io"):
        host = "registry-1.docker.io"
        if "/" not in repo:
            repo = f"library/{repo}"
    return host, repo


@dataclass
class ImageInfo:
    ref: str
    digest: str
    platform: str | None = None
    created: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    layers: list[str] = field(default_factory=list)
    size: int = 0  # compressed bytes of all layers


@dataclass
class ImageComparison:
    image: str
    old: ImageInfo
    new: ImageInfo
    labels: list[KeyChange]
    shared_layers: int

    @property
    def same_image(self) -> bool:
        return self.old.digest == self.new.digest


class Registry:
    def __init__(self, image: str, fetch: Fetch | None = None, scheme: str | None = None):
        self.host, self.repo = parse_image_name(image)
        self.fetch = fetch or urllib_fetch
        # Local registries (localhost:5000) normally serve plain HTTP; everything else uses HTTPS.
        local = self.host.split(":")[0] in ("localhost", "127.0.0.1")
        self.scheme = scheme or ("http" if local else "https")
        self._token: str | None = None

    def _get(self, path: str, accept: str) -> tuple[dict, bytes]:
        url = f"{self.scheme}://{self.host}/v2/{self.repo}/{path}"
        headers = {"Accept": accept}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        status, resp_headers, body = self.fetch(url, headers)
        if status == 401 and not self._token and "www-authenticate" in resp_headers:
            self._token = self._anonymous_token(resp_headers["www-authenticate"])
            return self._get(path, accept)
        if status == 404:
            raise RegistryError(f"not found in {self.host}/{self.repo}: {path.rsplit('/', 1)[-1]}")
        if status in (401, 403):
            raise RegistryError(f"{self.host} refused anonymous access to {self.repo} (HTTP {status}); "
                                "only public images are supported")
        if status != 200:
            raise RegistryError(f"{self.host} returned HTTP {status} for {path}")
        return resp_headers, body

    def _anonymous_token(self, challenge: str) -> str:
        scheme, _, params = challenge.partition(" ")
        if scheme.lower() != "bearer":
            raise RegistryError(f"{self.host} wants {scheme} authentication; only public images are supported")
        fields = dict(re.findall(r'(\w+)="([^"]*)"', params))
        realm = fields.pop("realm", None)
        if not realm:
            raise RegistryError(f"{self.host} sent an auth challenge without a token realm")
        fields.setdefault("scope", f"repository:{self.repo}:pull")
        status, _, body = self.fetch(f"{realm}?{urllib.parse.urlencode(fields)}", {})
        if status != 200:
            raise RegistryError(f"could not get an anonymous pull token from {realm} (HTTP {status})")
        doc = json.loads(body)
        token = doc.get("token") or doc.get("access_token")
        if not token:
            raise RegistryError(f"token response from {realm} had no token")
        return token

    def inspect(self, ref: str, platform: str = "linux/amd64") -> ImageInfo:
        headers, body = self._get(f"manifests/{ref}", MANIFEST_TYPES)
        manifest = json.loads(body)
        digest = headers.get("docker-content-digest") or (ref if ref.startswith("sha256:") else "")
        media = manifest.get("mediaType") or headers.get("content-type", "").split(";")[0]
        info = ImageInfo(ref, digest)
        if media in INDEX_TYPES or "manifests" in manifest:
            choice = _pick_platform(manifest.get("manifests", []), platform)
            if choice is None:
                raise RegistryError(f"{ref} has no {platform} image")
            info.platform = platform
            _, body = self._get(f"manifests/{choice['digest']}", MANIFEST_TYPES)
            manifest = json.loads(body)
        info.layers = [layer["digest"] for layer in manifest.get("layers", [])]
        info.size = sum(int(layer.get("size", 0)) for layer in manifest.get("layers", []))
        config_digest = manifest.get("config", {}).get("digest")
        if config_digest:
            _, body = self._get(f"blobs/{config_digest}", "application/json, */*")
            config = json.loads(body)
            info.created = config.get("created")
            info.labels = {k: str(v) for k, v in ((config.get("config") or {}).get("Labels") or {}).items()}
            if info.platform is None and config.get("os"):
                info.platform = "/".join(p for p in (config.get("os"), config.get("architecture")) if p)
        return info


def _pick_platform(entries: list[dict], platform: str) -> dict | None:
    want = platform.split("/")
    for entry in entries:
        p = entry.get("platform", {})
        have = [p.get("os", ""), p.get("architecture", "")] + ([p["variant"]] if p.get("variant") else [])
        if have[:len(want)] == want:
            return entry
    return None


def compare_tags(image: str, old: str, new: str, platform: str = "linux/amd64",
                 fetch: Fetch | None = None, scheme: str | None = None) -> ImageComparison:
    reg = Registry(image, fetch, scheme)
    a, b = reg.inspect(old, platform), reg.inspect(new, platform)
    shared = len(set(a.layers) & set(b.layers))
    return ImageComparison(image, a, b, diff_keys(a.labels, b.labels), shared)
