"""What a validator found: every violation, each naming where it is."""

from __future__ import annotations

# What an entry is called in a report when it has not said what it is called.
# Every rule here names a location, and an entry missing the field that would
# name it still has to be findable in the file.
UNNAMED = "<unnamed>"


class Report:
    """Collects every violation so one run tells the whole story."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def fail(self, where: str, message: str, requirement: str = "") -> None:
        suffix = f" ({requirement})" if requirement else ""
        self.errors.append(f"{where}: {message}{suffix}")

    def check(self, ok: bool, where: str, message: str, requirement: str = "") -> bool:
        if not ok:
            self.fail(where, message, requirement)
        return ok
