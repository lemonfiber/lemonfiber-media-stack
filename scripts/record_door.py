#!/usr/bin/env python3
"""Record what Jellyfin itself answers behind the door, case by case.

`check_door.py` asks the door every case in its CASES with a stand-in for
Jellyfin behind it. This asks the same cases with Jellyfin behind it: the image
the manifest pins, run fresh, holding two one-film libraries made here with
Jellyfin's own ffmpeg, each film with a subtitle file and trickplay pictures
beside it, a music library holding one artist's folder with its picture, and a
member given only the first film's library, who may not play from outside the
house. Jellyfin trusts the door's fixed address, and nothing else,
to name the client, as lemonfiber sets it. Each case is asked through the door
and, where it comes from the household, of Jellyfin alone beside it, and
recordings/door/<case>.json keeps the request as the case writes it and the
status and content type of each answer. `check_door.py` holds each recording
to its case's verdict.

Every password and token is made for the run and thrown away with it, never on
a command line and never printed, and a recording is written only once none of
them is in it. The containers, networks and volume are removed whatever
happened.

    python3 scripts/record_door.py    # needs Docker and the network

Exit 0 = every recording written and each agrees with its case, 1 = not.
"""

from __future__ import annotations

import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import TypeVar

import check_door
import stack_manifest
from check_door import (
    CASES,
    DOOR,
    DOOR_UPSTREAM,
    FROM_HOUSEHOLD,
    KEPT_ARTIST,
    PLAIN,
    RECORDINGS,
    SUBTITLE,
    TLS,
    Rig,
    docker,
    must,
    request_of,
    verdict,
)
from record import still_carries
from registry import by_digest, pinned

Found = TypeVar("Found")

JELLYFIN = "jellyfin"
READY_S = 240
POLL_S = 2.0
CLIENT = 'MediaBrowser Client="lemonfiber-record", Device="record", DeviceId="record-{who}", Version="1"'
FILMS = (("Films", "films", "Open Film (2001)"), ("Kept", "kept", "Closed Film (2002)"))
MUSIC = ("Kept music", "keptmusic")
# Trickplay pictures every second, so a three-second film has some to list.
TRICKPLAY_INTERVAL_MS = 1000
TRICKPLAY_WIDTH = 320
FFMPEG = "/usr/lib/jellyfin-ffmpeg/ffmpeg"


def service(sid: str) -> dict:
    return next(s for s in stack_manifest.load(check_door.ROOT)["service"] if s["id"] == sid)


class Jellyfin:
    """Jellyfin's own API on the loopback port this run published, as its administrator."""

    def __init__(self, port: int) -> None:
        self.base = f"{check_door.SCHEME}://{check_door.HOST}:{port}"
        self.token = ""

    def ask(self, method: str, path: str, document: object = None, *, token: str | None = None,
            who: str = "operator") -> tuple[int, bytes]:
        """As the administrator, or presenting `token`, or nothing where it is empty."""
        presented = self.token if token is None else token
        authorization = CLIENT.format(who=who) + (f', Token="{presented}"' if presented else "")
        request = urllib.request.Request(
            self.base + path, method=method,
            data=None if document is None else json.dumps(document).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "Authorization": authorization},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as reply:
                return reply.status, reply.read()
        except urllib.error.HTTPError as refused:
            return refused.code, refused.read()

    def wait(self, path: str) -> None:
        """Until `path` is answered 200. Jellyfin answers its public information
        and its health check while it is still starting, and 503 to the rest."""
        deadline = time.monotonic() + READY_S
        while time.monotonic() < deadline:
            try:
                if self.ask("GET", path)[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(POLL_S)
        raise RuntimeError(f"Jellyfin did not answer {path} within {READY_S}s")

    def until(self, what: str, ask: Callable[[], Found | None]) -> Found:
        deadline = time.monotonic() + READY_S
        while time.monotonic() < deadline:
            found = ask()
            if found:
                return found
            time.sleep(POLL_S)
        raise RuntimeError(f"Jellyfin never {what}")


def first_run(jellyfin: Jellyfin, made: set[str]) -> tuple[str, str, str]:
    """The administrator, two libraries, and a member given one; the member's
    token, and the ids of the film they may see and the one they may not."""
    secret, kept = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
    made.update({secret, kept})
    jellyfin.ask("POST", "/Startup/Configuration",
                 {"UICulture": "en-US", "MetadataCountryCode": "US", "PreferredMetadataLanguage": "en"})
    jellyfin.ask("GET", "/Startup/User")
    jellyfin.ask("POST", "/Startup/User", {"Name": "operator", "Password": secret})
    if jellyfin.ask("POST", "/Startup/Complete")[0] >= 300:
        raise RuntimeError("Jellyfin did not complete its first run")
    status, body = jellyfin.ask("POST", "/Users/AuthenticateByName", {"Username": "operator", "Pw": secret})
    if status != 200:
        raise RuntimeError(f"Jellyfin refused the administrator it made: {status}")
    jellyfin.token = json.loads(body)["AccessToken"]
    made.add(jellyfin.token)
    _, body = jellyfin.ask("GET", "/System/Configuration")
    configuration = json.loads(body)
    configuration["TrickplayOptions"]["Interval"] = TRICKPLAY_INTERVAL_MS
    if jellyfin.ask("POST", "/System/Configuration", configuration)[0] >= 300:
        raise RuntimeError("Jellyfin refused its trickplay interval")
    trickplay = {"EnableTrickplayImageExtraction": True, "ExtractTrickplayImagesDuringLibraryScan": True}
    for name, collection, folder, options in (*((name, "movies", folder, trickplay) for name, folder, _ in FILMS),
                                              (MUSIC[0], "music", MUSIC[1], {})):
        query = urllib.parse.urlencode({"name": name, "collectionType": collection, "paths": f"/media/{folder}",
                                        "refreshLibrary": "true"})
        jellyfin.ask("POST", f"/Library/VirtualFolders?{query}", {"LibraryOptions": options})
    _, body = jellyfin.ask("POST", "/Users/New", {"Name": "member", "Password": kept})
    member = json.loads(body)["Id"]
    folders = {f["Name"]: f["Id"] for f in json.loads(jellyfin.ask("GET", "/Library/MediaFolders")[1])["Items"]}
    policy = json.loads(jellyfin.ask("GET", f"/Users/{member}")[1])["Policy"]
    policy.update({"EnableAllFolders": False, "EnabledFolders": [folders[FILMS[0][0]]], "EnableRemoteAccess": False})
    if jellyfin.ask("POST", f"/Users/{member}/Policy", policy)[0] >= 300:
        raise RuntimeError("Jellyfin refused the member's policy")

    def both_films() -> dict[str, str] | None:
        listed = json.loads(jellyfin.ask("GET", "/Items?Recursive=true&IncludeItemTypes=Movie")[1])["Items"]
        found = {item["Name"]: item["Id"] for item in listed}
        return found if all(title.split(" (")[0] in found for _, _, title in FILMS) else None

    films = jellyfin.until("listed both films", both_films)
    open_item, closed_item = (films[title.split(" (")[0]] for _, _, title in FILMS)
    jellyfin.until("extracted the open film's picture",
                   lambda: jellyfin.ask("GET", f"/Items/{open_item}/Images/Primary")[0] == 200)
    for item in (open_item, closed_item):
        jellyfin.until("extracted a film's trickplay pictures",
                       lambda item=item: jellyfin.ask("GET", f"/Videos/{item}/Trickplay/{TRICKPLAY_WIDTH}/tiles.m3u8")[0] == 200)
        streams = json.loads(jellyfin.ask("GET", f"/Items/{item}/PlaybackInfo")[1])["MediaSources"][0]["MediaStreams"]
        subtitles = [stream["Index"] for stream in streams if stream["Type"] == "Subtitle"]
        if subtitles != [SUBTITLE]:
            raise RuntimeError(f"Jellyfin indexed the film's subtitles as {subtitles}, and the cases ask for {SUBTITLE}")

    def kept_artist_pictured() -> bool:
        listed = json.loads(jellyfin.ask("GET", "/Items?Recursive=true&IncludeItemTypes=MusicArtist")[1])["Items"]
        return any(item["Name"] == KEPT_ARTIST and "Primary" in item.get("ImageTags", {}) for item in listed)

    jellyfin.until("pictured the kept artist", kept_artist_pictured)
    status, body = jellyfin.ask("POST", "/Users/AuthenticateByName", {"Username": "member", "Pw": kept},
                                token="", who="member")
    if status != 200:
        raise RuntimeError(f"Jellyfin refused the member it made: {status}")
    token = json.loads(body)["AccessToken"]
    made.add(token)
    return token, open_item, closed_item


def trust_the_door(jellyfin: Jellyfin, rig: Rig) -> None:
    """Jellyfin's one known proxy, the door's fixed address on `door-upstream`,
    as lemonfiber sets it; Jellyfin reads it when it starts."""
    _, body = jellyfin.ask("GET", "/System/Configuration/network")
    network = json.loads(body)
    network["KnownProxies"] = [rig.place.door[DOOR_UPSTREAM]]
    if jellyfin.ask("POST", "/System/Configuration/network", network)[0] >= 300:
        raise RuntimeError("Jellyfin refused its network configuration")
    must(docker("restart", rig.upstream_name), "Jellyfin did not restart")
    jellyfin.wait("/System/Info")


def answer_of(answer: check_door.Answer) -> dict:
    kept: dict = {"status": answer.status}
    if answer.content_type:
        kept["headers"] = {"content-type": answer.content_type}
    return kept


def record() -> int:
    door, jellyfin_service = service(DOOR), service(JELLYFIN)
    made: set[str] = set()
    problems: list[str] = []
    with Rig(check_door.layout()) as rig:
        media = f"{rig.run}-media"
        try:
            must(docker("volume", "create", media), "a volume for the films")
            films = " && ".join(
                f'mkdir -p /media/{folder} && {FFMPEG} -loglevel error -f lavfi -i testsrc=duration=3:size=320x240:rate=10 '
                f'-f lavfi -i sine=duration=3 -c:v libx264 -c:a aac -shortest "/media/{folder}/{title}.mp4" && '
                f'printf "1\\n00:00:00,000 --> 00:00:02,000\\n{title}\\n" > "/media/{folder}/{title}.srt"'
                for _, folder, title in FILMS)
            artist = f"/media/{MUSIC[1]}/{KEPT_ARTIST}"
            films += (f' && mkdir -p "{artist}/Kept Album" && '
                      f'printf "<artist><name>{KEPT_ARTIST}</name></artist>" > "{artist}/artist.nfo" && '
                      f'{FFMPEG} -loglevel error -f lavfi -i color=c=orange:s=64x64 -frames:v 1 "{artist}/folder.jpg" && '
                      f'{FFMPEG} -loglevel error -f lavfi -i sine=duration=3 -c:a aac "{artist}/Kept Album/01 Kept.m4a"')
            must(docker("run", "--rm", "-v", f"{media}:/media", "--entrypoint", "sh", pinned(jellyfin_service),
                        "-c", films), "the films could not be made")
            port = check_door.free_port()
            rig.start_upstream(pinned(jellyfin_service), ["-v", f"{media}:/media:ro"], published=port)
            jellyfin = Jellyfin(port)
            jellyfin.wait("/Startup/Configuration")
            token, open_item, closed_item = first_run(jellyfin, made)
            trust_the_door(jellyfin, rig)
            rig.start_door(check_door.CADDYFILE.read_text(encoding="utf-8"))
            RECORDINGS.mkdir(parents=True, exist_ok=True)
            for case in CASES:
                request = request_of(case, open_item, closed_item, token)
                at_door = rig.ask(case, PLAIN, request)
                document: dict = {
                    "recorded_from": by_digest(door),
                    "behind": by_digest(jellyfin_service),
                    "note": case.note,
                    "request": {
                        "method": case.method,
                        "path": case.path,
                        "presents": check_door.PRESENTS[case.presents],
                        "from": case.origin,
                        **({"forwarded_for": case.forwarded_for} if case.forwarded_for else {}),
                    },
                    "door": answer_of(at_door),
                }
                if case.origin == FROM_HOUSEHOLD:
                    over_tls = rig.ask(case, TLS, request)
                    if over_tls.status != at_door.status:
                        problems.append(f"{case.name}: {PLAIN} answered {at_door.status} and {TLS} {over_tls.status}")
                    document["jellyfin_alone"] = answer_of(check_door.ask_at(port, case.method, *request))
                text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
                if still_carries(text, made):
                    problems.append(f"{case.name}: the recording carries a credential this run made; not written")
                    continue
                (RECORDINGS / f"{case.name}.json").write_text(text, encoding="utf-8")
                if verdict(at_door, stand_in_behind=False) != case.expect:
                    problems.append(f"{case.name}: Jellyfin behind the door answered {at_door.status}, and the case "
                                    f"expects the door to {case.expect}")
        finally:
            for name in rig.containers:
                docker("rm", "-fv", name)
            rig.containers.clear()
            docker("volume", "rm", media)
    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        return 1
    print(f"{len(CASES)} recordings written to recordings/door/, each agreeing with its case")
    return 0


def main() -> int:
    try:
        return record()
    except RuntimeError as failed:
        print(f"::error::{failed}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
