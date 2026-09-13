#!/usr/bin/env python3
"""How the checks here talk to a forge, and what they will not do on the way.

Two checks ask github.com about a project the manifest names: one for the
licence it publishes now, one for the history behind a candidate. Both run in CI
with a token, and both take the name of what to ask about from a file anybody
can send a pull request against. That is the whole reason this is one module
rather than a request in each of them:

  The host is not negotiable. `get_json` takes a path, never a URL, so no
  caller — and nothing in the manifest a caller read — can aim a request
  carrying a token at a host of its choosing.

  A redirect off that host is refused rather than followed. urllib copies
  request headers onto a redirected request, Authorization among them, so a
  redirect that leaves api.github.com would hand the token to whoever answered
  it. Renames redirect within the host and still work.

  A reply is bounded before it is read, and sanitised before any of it is
  printed. A workflow log reads `::` at the start of a line as an instruction,
  so nothing arriving from a network gets to contain one.

Nothing here decides anything about a service; the judgements live with the
checks that make them. `--self-test` proves the three properties above, offline.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
TIMEOUT_S = 20
# The licence endpoint answers with a whole licence file, base64-encoded, so a
# legitimate reply is tens of kilobytes. A reply nobody bounded is one that can
# be as large as whoever sends it likes.
MAX_REPLY = 512 * 1024

# What the forge says when it has no such repository, file or comparison. A
# different answer from a forge that could not be reached, and the callers here
# treat it as one.
NOT_FOUND = "the forge has no such thing"

# `https://github.com/<owner>/<repo>`, and nothing else: exactly two path
# segments, because a URL with more of them is not a repository root.
GITHUB_REPO = re.compile(r"^/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")
# What may be echoed out of a reply. Deliberately without `:`, which is what a
# workflow command is made of.
PRINTABLE = re.compile(r"[^A-Za-z0-9 .,;/+_()-]")


def safe(text: str, limit: int = 120) -> str:
    """One line of a network reply, fit to print into a workflow log."""
    return PRINTABLE.sub("?", str(text).strip().replace("\n", " "))[:limit]


def repo_of(upstream: str) -> tuple[str, str] | None:
    """`owner`, `repo` for a github.com project URL, or None for anything else.

    The host is parsed and compared rather than stripped off by prefix. Where a
    request goes has to be a decision made from a parsed URL, not a consequence
    of how somebody spelled a field.
    """
    parsed = urllib.parse.urlsplit(upstream)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return None
    match = GITHUB_REPO.match(parsed.path)
    return (match.group(1), match.group(2)) if match else None


def permitted_redirect(old: str, new: str) -> bool:
    """Whether a redirect may be followed while the request carries a token.

    Scheme as well as host: a redirect to the same host over plain HTTP would
    put the token on the wire in clear, which is a different way of losing it
    and not a lesser one.
    """
    here, there = urllib.parse.urlsplit(old), urllib.parse.urlsplit(new)
    return there.scheme == "https" and there.netloc.lower() == here.netloc.lower()


class SameHostRedirects(urllib.request.HTTPRedirectHandler):
    # The signature is urllib's; it calls this to decide what a redirect becomes.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not permitted_redirect(req.full_url, newurl):
            raise urllib.error.HTTPError(
                req.full_url, code, "redirected off the forge; not followed", headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def get_json(path: str) -> tuple[object | None, str]:
    """One GET against the forge's API, as a document or as why there isn't one.

    `path` is everything after the host, so the host stays this module's to
    decide. The token is read from the environment here and never returned,
    printed or logged.
    """
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "lemonfiber-media-stack"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(f"{API}{path}", headers=headers)
    try:
        with urllib.request.build_opener(SameHostRedirects).open(request, timeout=TIMEOUT_S) as reply:
            return json.loads(reply.read(MAX_REPLY)), ""
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None, NOT_FOUND
        return None, f"forge answered {error.code} {safe(error.reason)}"
    except urllib.error.URLError as error:
        return None, f"forge unreachable {safe(error.reason)}"
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, "forge answered with something that is not JSON"


def self_test() -> int:
    """The three properties above, none of which needs a forge to prove."""
    problems = []

    addressing = (
        ("https://github.com/Sonarr/Sonarr", ("Sonarr", "Sonarr")),
        ("https://github.com/Sonarr/Sonarr/", ("Sonarr", "Sonarr")),
        ("https://github.com/Sonarr/Sonarr.git", ("Sonarr", "Sonarr")),
        ("https://github.com/Sonarr/Sonarr/tree/main", None),
        ("https://evil.example/github.com/Sonarr/Sonarr", None),
        ("https://github.com.evil.example/Sonarr/Sonarr", None),
        ("https://github.com@evil.example/Sonarr/Sonarr", None),
        ("http://github.com/Sonarr/Sonarr", None),
        ("https://gitlab.com/Sonarr/Sonarr", None),
        ("not a url at all", None),
    )
    for upstream, wanted in addressing:
        if repo_of(upstream) != wanted:
            problems.append(f"{upstream} was read as {repo_of(upstream)}, wanted {wanted}")

    if not permitted_redirect(f"{API}/repos/a/b", f"{API}/repos/c/d"):
        problems.append("a redirect within the forge was refused; a renamed project would read as gone")
    for elsewhere in ("https://evil.example/collect", "http://api.github.com/x", "https://API.evil/x"):
        if permitted_redirect(f"{API}/repos/a/b", elsewhere):
            problems.append(f"a redirect to {elsewhere} was permitted to carry the token")

    for hostile, because in (
        ("::error::owned", "a workflow command"),
        ("::add-mask::secret", "a workflow command"),
        ("line one\nline two", "a second line"),
    ):
        printed = safe(hostile)
        if "::" in printed or "\n" in printed:
            problems.append(f"{because} survived being printed: {printed!r}")
    if len(safe("x" * 500)) > 120:
        problems.append("a long reply was printed in full")

    for problem in problems:
        print(f"::error::self-test: {problem}")
    if problems:
        print("\nA request that can be aimed by a manifest is not a request this repository makes.")
        return 1
    print(f"self-test: {len(addressing)} URLs read, redirects held to one host, replies made printable")
    return 0


def main() -> int:
    if "--self-test" not in sys.argv:
        print(__doc__)
        print("This is what the networked checks use to reach a forge; it checks nothing by itself.")
        return 2
    return self_test()


if __name__ == "__main__":
    sys.exit(main())
