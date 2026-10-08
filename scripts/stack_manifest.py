"""The stack manifest as one document: `stack.toml` and the service files it includes.

The manifest is a root, `stack.toml`, and a file per service in `services/`, which
the root's `include` list names in the order the services are listed (ARCH-R171).
Every script reads the manifest through here, so none of them has to know it is
written in pieces. The joined text is the root first, then each included file, the
way lemonfiber joins them, so a script that reads text and one that reads the parsed
document see the same manifest lemonfiber does.

The rules about the pieces are `layout_problems`, which `validate_manifest.py`
reports. lemonfiber refuses a stack that breaks them too, in its build.
"""

import pathlib
import tomllib
from collections.abc import Callable

ROOT_FILE = "stack.toml"
SERVICES = "services"
EXTENSION = ".toml"


def service_file(sid: str) -> str:
    """Where a service's file is, relative to the stack root."""
    return f"{SERVICES}/{sid}{EXTENSION}"


def joined(read: Callable[[str], str | None]) -> str | None:
    """The manifest as one TOML text, or None where the root cannot be read.

    `read` answers a path relative to the stack root with that file's text, or None.
    An included file it cannot answer is left out, and `layout_problems` names it.
    """
    root = read(ROOT_FILE)
    if root is None:
        return None
    parts = [root]
    for entry in included(root):
        text = read(entry)
        if text is not None:
            parts.append(f"\n# {entry}\n{text}")
    return "".join(parts)


def included(root: str) -> list[str]:
    """The entries of a root's `include` list that are paths, in order."""
    try:
        listed = tomllib.loads(root).get("include", [])
    except tomllib.TOMLDecodeError:
        return []
    return [entry for entry in listed if isinstance(entry, str)] if isinstance(listed, list) else []


def on_disk(root: pathlib.Path) -> Callable[[str], str | None]:
    """A reader of the stack at `root`, for `joined`."""

    def read(relative: str) -> str | None:
        path = root / relative
        return path.read_text(encoding="utf-8") if path.is_file() else None

    return read


def text(root: pathlib.Path) -> str:
    """The manifest of the stack at `root`, as one TOML text."""
    found = joined(on_disk(root))
    if found is None:
        raise FileNotFoundError(root / ROOT_FILE)
    return found


def load(root: pathlib.Path) -> dict:
    """The manifest of the stack at `root`, parsed."""
    return tomllib.loads(text(root))


def layout_problems(root: pathlib.Path) -> list[tuple[str, str]]:
    """Every way the files break the rules about them, as (where, what) pairs.

    The same rules, in the same words, as lemonfiber's assembly.
    """
    problems: list[tuple[str, str]] = []
    try:
        document = tomllib.loads((root / ROOT_FILE).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as why:
        return [(ROOT_FILE, f"could not be read: {why}")]

    declared = document.get("service", [])
    for service in declared if isinstance(declared, list) else [declared]:
        sid = service.get("id") if isinstance(service, dict) else None
        problems.append((ROOT_FILE, f"declares service {sid}, which belongs in {service_file(sid)}"
                         if isinstance(sid, str) else
                         "declares a [[service]], and each service is in a file of its own"))

    listed = document.get("include", [])
    if not isinstance(listed, list):
        problems.append((ROOT_FILE, "include is a list of the service files"))
        listed = []
    seen: set[str] = set()
    for entry in listed:
        if not isinstance(entry, str):
            problems.append((ROOT_FILE, f"include holds {entry}, which is not a path"))
        elif not is_entry(entry):
            problems.append((f"include entry {entry}", f"is not of the form {SERVICES}/<id>{EXTENSION}"))
        elif entry in seen:
            problems.append((f"include entry {entry}", "appears more than once"))
        elif not (root / entry).is_file():
            seen.add(entry)
            problems.append((f"include entry {entry}", "names no file"))
        else:
            seen.add(entry)
            problems.extend(service_file_problems(root, entry))

    directory = root / SERVICES
    present = sorted(f"{SERVICES}/{path.name}" for path in directory.glob(f"*{EXTENSION}") if path.is_file())
    problems.extend((stray, "is in services/ and no include entry names it")
                    for stray in present if stray not in seen)
    return problems


def is_entry(entry: str) -> bool:
    """Whether an `include` entry is of the form `services/<id>.toml`."""
    prefix = f"{SERVICES}/"
    if not entry.startswith(prefix) or not entry.endswith(EXTENSION):
        return False
    sid = entry[len(prefix):-len(EXTENSION)]
    return bool(sid) and not sid.startswith(".") and "/" not in sid and "\\" not in sid


def service_file_problems(root: pathlib.Path, entry: str) -> list[tuple[str, str]]:
    """What one service file holds that it should not."""
    try:
        document = tomllib.loads((root / entry).read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as why:
        return [(entry, str(why))]
    problems = [(entry, f"holds {key}, and a service file holds only its [[service]]")
                for key in document if key != "service"]
    services = document.get("service", [])
    sid = entry[len(SERVICES) + 1:-len(EXTENSION)]
    if not services:
        problems.append((entry, "holds no [[service]]"))
    elif len(services) > 1:
        problems.append((entry, f"holds {len(services)} [[service]], and a service file holds one"))
    elif services[0].get("id") != sid:
        problems.append((entry, f"holds service {services[0].get('id', 'with no id')}, and its name says {sid}"))
    return problems


def self_test() -> list[str]:
    """Every rule judged against a stack written for it; the failures, if any."""
    import tempfile

    failures = []
    service = '[[service]]\nid = "{id}"\nname = "{id}"\n'
    root_text = 'schema_version = 1\ninclude = ["services/a.toml", "services/b.toml"]\n\n[[profile]]\nid = "p"\n'
    cases = {
        "a whole stack": (root_text, {"a": service.format(id="a"), "b": service.format(id="b")}, []),
        "a service in the root": (root_text + "\n" + service.format(id="c"),
                                  {"a": service.format(id="a"), "b": service.format(id="b")},
                                  [("stack.toml", "declares service c, which belongs in services/c.toml")]),
        "an entry of the wrong form": (root_text.replace("services/b.toml", "other/b.toml"),
                                       {"a": service.format(id="a")},
                                       [("include entry other/b.toml", "is not of the form services/<id>.toml")]),
        "an entry twice": (root_text.replace('"services/b.toml"', '"services/a.toml"'),
                           {"a": service.format(id="a")},
                           [("include entry services/a.toml", "appears more than once")]),
        "a missing file": (root_text, {"a": service.format(id="a")},
                           [("include entry services/b.toml", "names no file")]),
        "a stray file": (root_text, {"a": service.format(id="a"), "b": service.format(id="b"),
                                     "c": service.format(id="c")},
                         [("services/c.toml", "is in services/ and no include entry names it")]),
        "a mismatched id": (root_text, {"a": service.format(id="a"), "b": service.format(id="x")},
                            [("services/b.toml", "holds service x, and its name says b")]),
        "two services": (root_text, {"a": service.format(id="a"),
                                     "b": service.format(id="b") + service.format(id="b2")},
                         [("services/b.toml", "holds 2 [[service]], and a service file holds one")]),
        "something else": (root_text, {"a": service.format(id="a"),
                                       "b": service.format(id="b") + '[[profile]]\nid = "q"\n'},
                           [("services/b.toml", "holds profile, and a service file holds only its [[service]]")]),
        "no service": (root_text, {"a": service.format(id="a"), "b": ""},
                       [("services/b.toml", "holds no [[service]]")]),
    }
    for said, (root_written, files, want) in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / ROOT_FILE).write_text(root_written, encoding="utf-8")
            (root / SERVICES).mkdir()
            for sid, body in files.items():
                (root / service_file(sid)).write_text(body, encoding="utf-8")
            got = layout_problems(root)
            if got != want:
                failures.append(f"{said}: {got} != {want}")
            if not want:
                ids = [entry["id"] for entry in load(root).get("service", [])]
                if ids != ["a", "b"]:
                    failures.append(f"{said}: read back as {ids}, not a then b")
    return failures


if __name__ == "__main__":
    import sys

    found = self_test()
    for failure in found:
        print(f"::error::stack manifest self-test: {failure}")
    if not found:
        print("stack manifest self-test: every layout rule refuses what it should, and a whole stack reads back in order")
    sys.exit(1 if found else 0)
