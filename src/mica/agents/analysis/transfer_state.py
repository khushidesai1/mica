"""Generic transfer receipts, with the prior persisted values accepted during migration."""

from __future__ import annotations

import re
from typing import Any

HANDOFF = "transfer"
HANDOFFS = frozenset({HANDOFF, "atomx"})


def is_handoff(value: Any) -> bool:
    return isinstance(value, str) and value in HANDOFFS


def mail_stages(value: str) -> tuple[str, ...]:
    """Accept generic stages but keep new intents visible to older delivery controllers."""
    matched = re.fullmatch(r"(?:transfer|atomx)-([a-f0-9]{24})", value)
    if matched is None:
        return ()
    suffix = matched[1]
    # An older in-flight controller must see an ambiguous new send before it retries.
    # Keep one canonical persisted identity; do not dual-write competing send intents.
    return ("atomx-" + suffix, "transfer-" + suffix)
