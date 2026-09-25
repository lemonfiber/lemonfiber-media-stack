#!/usr/bin/env python3
"""How the scripts here ask a container registry what a tag names, and what it has.

Two questions, both anonymous and both read-only: which tags a repository
publishes, and which multi-architecture index a reference resolves to. Every
image in the stack is public, so nothing here holds a credential of anybody's —
the bearer a registry hands out is its own anonymous pull token, asked for on
the registry's own challenge.

  The digest is computed, not taken. A registry names what it served in a
  header, and the header is the registry's claim; the SHA-256 of the bytes
  that arrived is the fact. Only the second is returned.

  A redirect off the host a request was sent to is refused rather than
  followed, because urllib copies request headers onto the redirected request
  and one of them is that pull token.

  A reply is bounded before it is read, and nothing arriving from a registry is
  printed unsanitised: `forge.safe` is the one place that rule is written.

`--self-test` proves the addressing, the digest arithmetic and the reading of an
index against documents nobody fetched.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from forge import safe

SCHEME = "https"
TIMEOUT_S = 20
# A tag listing for a busy repository runs to hundreds of kilobytes a page; an
# index is a few kilobytes. One bound covers both with room.
MAX_REPLY = 4 * 1024 * 1024
# The pages of a tag listing followed before this gives up, which at a
# thousand tags a page is far more tags than any image here publishes.
MAX_PAGES = 50

DOCKER_HUB = "registry-1.docker.io"
INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
IMAGE_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
ACCEPT = ", ".join(INDEX_TYPES + IMAGE_TYPES)

DIGEST = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
# A repository path as a registry spells one, and a tag as one is spelled.
REPOSITORY = re.compile(r"\A[a-z0-9]+(?:[._/-][a-z0-9]+)*\Z")
TAG = re.compile(r"\A\w[\w.-]{0,127}\Z", re.ASCII)
HOST = re.compile(r"\A[a-z0-9.-]+(?::\d+)?\Z", re.ASCII)


def address(image: str) -> tuple[str, str] | None:
    """The registry host and repository an image name refers to, or None.

    Docker's own reading: a first segment with a dot or a colon in it is a
    host, and anything else is on Docker Hub, where a single-segment name is an
    official image under `library/`.
    """
    first, _, rest = image.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        host, repository = first.lower(), rest
    else:
        host, repository = DOCKER_HUB, image if rest else f"library/{image}"
    if not HOST.match(host) or not REPOSITORY.match(repository):
        return None
    return host, repository


def pinned(service: dict) -> str:
    """The reference a manifest's service runs: the tag to read, the digest that decides.

    A service with no digest is named by its tag alone. The manifest check
    refuses that service for the missing field, once, rather than again for a
    reference ending in nothing.
    """
    reference = f"{service.get('image')}:{service.get('tag')}"
    return f"{reference}@{service['digest']}" if service.get("digest") else reference


def digest_of(body: bytes) -> str:
    """The content digest of a manifest, as a registry addresses it."""
    return f"sha256:{hashlib.sha256(body).hexdigest()}"


def platforms_of(document: dict) -> set[tuple[str, str]]:
    """The runnable platforms an index lists, leaving out attestations."""
    found = set()
    for entry in document.get("manifests", []):
        platform = entry.get("platform", {})
        if platform.get("os") == "unknown" or platform.get("architecture") == "unknown":
            continue
        found.add((str(platform.get("os")), str(platform.get("architecture"))))
    return found


def is_index(document: dict, media_type: str) -> bool:
    """Whether a manifest is an index of images rather than one image."""
    declared = document.get("mediaType") or media_type
    return declared in INDEX_TYPES or ("manifests" in document and "config" not in document)


class SameHostRedirects(urllib.request.HTTPRedirectHandler):
    # The signature is urllib's; it calls this to decide what a redirect becomes.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        here, there = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if there.scheme != SCHEME or there.netloc.lower() != here.netloc.lower():
            raise urllib.error.HTTPError(
                req.full_url, code, "redirected off the registry; not followed", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url: str, headers: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": "lemonfiber-media-stack", **headers})
    with urllib.request.build_opener(SameHostRedirects).open(request, timeout=TIMEOUT_S) as reply:
        body = reply.read(MAX_REPLY + 1)
        if len(body) > MAX_REPLY:
            raise ValueError(f"the registry answered with more than {MAX_REPLY // 1024} KiB")
        return body, {key.lower(): value for key, value in reply.headers.items()}


def challenge_fields(challenge: str) -> dict[str, str]:
    """The `key="value"` pairs of a `WWW-Authenticate` challenge, after its scheme."""
    fields = {}
    for part in challenge.partition(" ")[2].split(","):
        key, _, value = part.strip().partition("=")
        if key and value.startswith('"') and value.endswith('"') and len(value) >= 2:
            fields[key] = value[1:-1]
    return fields


def next_page(link: str) -> str:
    """The target of a `Link` header's `rel="next"`, or empty where it has none."""
    for part in link.split(","):
        target, _, parameters = part.partition(";")
        target = target.strip()
        relation = parameters.replace('"', "").replace(" ", "")
        if relation == "rel=next" and target.startswith("<") and target.endswith(">"):
            return target[1:-1]
    return ""


def _anonymous_token(challenge: str) -> str:
    """The pull token a registry's `WWW-Authenticate: Bearer` challenge offers."""
    if not challenge.lower().startswith("bearer "):
        raise ValueError("the registry asked for credentials this does not hold")
    fields = challenge_fields(challenge)
    realm = urllib.parse.urlsplit(fields.get("realm", ""))
    if realm.scheme != SCHEME or not realm.netloc:
        raise ValueError("the registry's token service is not an https address")
    query = urllib.parse.urlencode({k: v for k, v in fields.items() if k in ("service", "scope")})
    body, _ = _fetch(urllib.parse.urlunsplit((SCHEME, realm.netloc, realm.path, query, "")), {})
    document = json.loads(body)
    token = document.get("token") or document.get("access_token")
    if not token:
        raise ValueError("the registry's token service answered without a token")
    return str(token)


def _get(host: str, path: str, accept: str = "") -> tuple[bytes, dict[str, str]]:
    """One GET against a registry, answering its anonymous challenge if it makes one."""
    url = f"{SCHEME}://{host}{path}"
    headers = {"Accept": accept} if accept else {}
    try:
        return _fetch(url, headers)
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
        challenge = error.headers.get("WWW-Authenticate", "")
    return _fetch(url, {**headers, "Authorization": f"Bearer {_anonymous_token(challenge)}"})


def _problem(error: Exception) -> str:
    """Why a registry gave no answer: its status, no connection, or a reply that is not JSON.

    JSON and text decoding errors are `ValueError`s, which is how they arrive
    here alongside the bound `_fetch` raises, so they are told apart first.
    """
    if isinstance(error, urllib.error.HTTPError):
        return f"registry answered {error.code} {safe(error.reason)}"
    if isinstance(error, urllib.error.URLError):
        return f"registry unreachable {safe(error.reason)}"
    if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        return "registry answered with something that is not JSON"
    return safe(str(error))


def tags(image: str) -> tuple[list[str], str]:
    """Every tag a repository publishes, or why they could not be listed."""
    where = address(image)
    if where is None:
        return [], f"{safe(image)} is not an image name this will address"
    host, repository = where
    path = f"/v2/{repository}/tags/list?n=1000"
    found: list[str] = []
    try:
        for _ in range(MAX_PAGES):
            body, headers = _get(host, path)
            found.extend(str(tag) for tag in json.loads(body).get("tags") or [])
            following = next_page(headers.get("link", ""))
            if not following:
                return found, ""
            nxt = urllib.parse.urlsplit(following)
            if nxt.netloc and nxt.netloc.lower() != host:
                return found, "the registry paged its tag list onto another host; not followed"
            path = urllib.parse.urlunsplit(("", "", nxt.path, nxt.query, ""))
        return found, f"the tag list ran past {MAX_PAGES} pages"
    except (urllib.error.URLError, ValueError) as error:
        return [], _problem(error)


def resolve(image: str, reference: str) -> tuple[str, set[tuple[str, str]], str]:
    """The index a tag or digest resolves to: its digest, its platforms, or why not.

    A reference that resolves to a single image rather than an index answers
    with the reason and no digest, because pinning one platform's image is the
    thing `E1-R1` forbids.
    """
    where = address(image)
    if where is None:
        return "", set(), f"{safe(image)} is not an image name this will address"
    if not (TAG.match(reference) or DIGEST.match(reference)):
        return "", set(), f"{safe(reference)} is neither a tag nor a sha256 digest"
    host, repository = where
    try:
        body, headers = _get(host, f"/v2/{repository}/manifests/{reference}", ACCEPT)
        document = json.loads(body)
    except (urllib.error.URLError, ValueError) as error:
        return "", set(), _problem(error)
    digest = digest_of(body)
    if DIGEST.match(reference) and digest != reference:
        return "", set(), f"asked for {reference} and was served content whose digest is {digest}"
    if not is_index(document, headers.get("content-type", "")):
        return "", set(), "resolves to a single-platform image, not a multi-architecture index"
    return digest, platforms_of(document), ""


def addressing_problems() -> list[str]:
    """Where an image name is read as living, including names that are not names."""
    problems = []
    for image, wanted in (
        ("caddy", (DOCKER_HUB, "library/caddy")),
        ("jellyfin/jellyfin", (DOCKER_HUB, "jellyfin/jellyfin")),
        ("lscr.io/linuxserver/sonarr", ("lscr.io", "linuxserver/sonarr")),
        ("ghcr.io/hotio/unpackerr", ("ghcr.io", "hotio/unpackerr")),
        ("localhost:5000/x", ("localhost:5000", "x")),
        ("ghcr.io/../../evil", None),
        ("Caddy", None),
        ("ghcr.io/a?b", None),
    ):
        if address(image) != wanted:
            problems.append(f"{image} was addressed as {address(image)}, wanted {wanted}")
    return problems


def reading_problems() -> list[str]:
    """The digest arithmetic, and an index told apart from one image."""
    problems = []
    if digest_of(b"") != "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855":
        problems.append("the digest of nothing is not SHA-256's")

    index = {
        "mediaType": INDEX_TYPES[0],
        "manifests": [
            {"platform": {"os": "linux", "architecture": "amd64"}},
            {"platform": {"os": "linux", "architecture": "arm64"}},
            {"platform": {"os": "unknown", "architecture": "unknown"}},
        ],
    }
    if platforms_of(index) != {("linux", "amd64"), ("linux", "arm64")}:
        problems.append(f"an index with an attestation read as {sorted(platforms_of(index))}")
    if not is_index(index, ""):
        problems.append("an OCI index was not read as an index")
    if not is_index({"manifests": []}, INDEX_TYPES[1]):
        problems.append("a Docker manifest list was not read as an index")
    if is_index({"mediaType": IMAGE_TYPES[0], "config": {}, "layers": []}, ""):
        problems.append("a single image was read as an index")
    return problems


def header_problems() -> list[str]:
    """What is read out of a challenge and a `Link`, and which challenges are refused."""
    problems = []
    challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:a/b:pull"'
    if challenge_fields(challenge) != {"realm": "https://ghcr.io/token", "service": "ghcr.io",
                                       "scope": "repository:a/b:pull"}:
        problems.append(f"a challenge read as {challenge_fields(challenge)}")
    for link, wanted in (
        ('</v2/library/caddy/tags/list?last=2.8&n=1000>; rel="next"', "/v2/library/caddy/tags/list?last=2.8&n=1000"),
        ("</v2/x/tags/list?last=1>; rel=next", "/v2/x/tags/list?last=1"),
        ('</v2/x/tags/list?last=1>; rel="prev"', ""),
        ("", ""),
    ):
        if next_page(link) != wanted:
            problems.append(f"{link!r} read as next page {next_page(link)!r}")

    try:
        _anonymous_token('Basic realm="https://example.test"')
        problems.append("a Basic challenge was answered")
    except ValueError:
        pass
    try:
        _anonymous_token('Bearer realm="http://example.test/token",service="x"')
        problems.append("a token service over plain HTTP was asked")
    except ValueError:
        pass
    return problems


def self_test() -> int:
    """The addressing, the digest and the reading of an index, without a registry."""
    problems = addressing_problems() + reading_problems() + header_problems()
    for problem in problems:
        print(f"::error::self-test: {problem}")
    if problems:
        return 1
    print("self-test: names addressed, digests computed, indexes told apart from images")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    if len(sys.argv) == 3:
        digest, found, problem = resolve(sys.argv[1], sys.argv[2])
        if problem:
            print(problem)
            return 1
        print(digest, " ".join(sorted(f"{o}/{a}" for o, a in found)))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
