"""The `[[wiring]]` table of stack.toml: each link between two services, and
whether it asks for a capability or names the service at the far end.
"""

from __future__ import annotations

from manifest_rules import CORE_CAPABILITY
from report import UNNAMED, Report


def wiring_ends(entry: dict, where: str, service_ids: set[str], report: Report) -> None:
    """The three fields that name a service, each held to being one of ours.

    Separated from the shape rules below only because the list of them is what
    will grow: a link between two services is the one thing here that cannot be
    checked against a vocabulary, because the other end is a service rather than
    a name somebody published.
    """
    by = entry.get("by")
    report.check(
        isinstance(by, str) and by in service_ids,
        where,
        f"by names {by!r}, which this stack does not declare",
        "F4-R1",
    )
    for field in ("to", "filled_by"):
        named = entry.get(field)
        if named is None:
            continue
        report.check(
            isinstance(named, str) and named in service_ids,
            where,
            f"{field} names {named!r}, which this stack does not declare",
            "F4-R1",
        )
        report.check(
            named != by,
            where,
            f"{field} names the service the wiring runs from; a service does not wire to itself",
        )


def validate_wiring(entry: dict, service_ids: set[str], provides_of: dict[str, list],
                    seen: set[tuple], report: Report) -> None:
    """One link between two services, and which of the two it names.

    An ask says what the far end has to *do*; a by-name link says which service
    it is and why. Exactly one of the two, because a link that said both would
    be an ask whose answer was already decided, which is a name with extra steps.

    `why` is required on a by-name link and on a chosen filler for the same
    reason: an exception with no reason beside it is indistinguishable from an
    oversight, and a choice with no reason beside it is indistinguishable from a
    rule somebody encoded — which is the thing being chosen instead of.
    """
    by = entry.get("by", UNNAMED)
    asks, to = entry.get("asks"), entry.get("to")
    where = f"wiring by {by}"
    wiring_ends(entry, where, service_ids, report)

    if not report.check(
        (asks is None) != (to is None),
        where,
        "a wiring asks for a capability or names a service, and carries exactly one of "
        f"`asks` and `to`; this has {'both' if asks and to else 'neither'}",
        "F9-R4",
    ):
        return

    for field in ("each", "filled_by"):
        report.check(
            field not in entry or asks is not None,
            where,
            f"{field} says something about an ask, and this names a service",
        )
    report.check(
        not (entry.get("each") and "filled_by" in entry),
        where,
        "filled_by chooses between claimants and each reaches all of them; a wiring "
        "does both only by meaning neither",
    )

    if to is not None:
        report.check(
            isinstance(entry.get("why"), str) and bool(str(entry.get("why", "")).strip()),
            where,
            f"names {to!r} and does not say why; a by-name wiring is the exception and an "
            "exception with no reason reads as an oversight",
            "F4-R12",
        )
        report.check((by, "to", to) not in seen, where, f"names {to!r} twice")
        seen.add((by, "to", to))
        return

    report.check(
        isinstance(asks, str) and CORE_CAPABILITY.match(asks) is not None,
        where,
        f"asks for {asks!r}, which is not a core capability name — those are `area.verb`, "
        "lowercase, with exactly one dot",
        "F4-R1",
    )
    report.check((by, "asks", asks) not in seen, where, f"asks for {asks!r} twice")
    seen.add((by, "asks", asks))

    filler = entry.get("filled_by")
    if filler is None:
        return
    report.check(
        isinstance(entry.get("why"), str) and bool(str(entry.get("why", "")).strip()),
        where,
        f"chooses {filler!r} and does not say why; a choice with no reason beside it is a "
        "rule somebody encoded",
        "F4-R8",
    )
    report.check(
        asks in provides_of.get(str(filler), []),
        where,
        f"chooses {filler!r} to fill {asks!r}, which that service does not declare",
        "F4-R8",
    )


def validate_wirings(wirings: list, services: list, service_ids: set[str],
                     report: Report) -> None:
    """Every link between two of the stack's services."""
    provides_of = {
        str(service.get("id", UNNAMED)): service.get("provides", []) for service in services
    }
    seen: set[tuple] = set()
    for entry in wirings:
        validate_wiring(entry, service_ids, provides_of, seen, report)
    validate_each_addressed(wirings, services, report)


def validate_each_addressed(wirings: list, services: list, report: Report) -> None:
    """A variable holds one address, so none names a capability its service asks
    `each` of: the rule `crates/lemonfiber-plugin/src/refusing/asking.rs` holds a
    plugin's `[[ask]]` to, held here to the stack's `[[wiring]]`."""
    each = {(entry.get("by"), entry.get("asks")) for entry in wirings if entry.get("each")}
    for service in services:
        sid = service.get("id", UNNAMED)
        env = service.get("env")
        for name, value in (env.items() if isinstance(env, dict) else ()):
            capability = value.get("url_of") if isinstance(value, dict) else None
            report.check(
                (sid, capability) not in each,
                f"service {sid}.env.{name}",
                f"asks for the address of {capability}, which {sid} asks each of; a variable holds "
                "one address",
                "ARCH-R232",
            )
