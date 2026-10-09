"""Package requests are data, never shell fragments or installer options."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from .runtime_source import parse_source
from .toolkit_catalog import Source, ToolkitEntry, lookup

POLICY_VERSION = 1
RuntimeKind = Literal["overlay", "isolated", "image"]


@dataclass(frozen=True)
class PackageRequest:
    specs: tuple[str, ...]
    entries: tuple[ToolkitEntry, ...] = ()
    sources: tuple[Source, ...] = ()

    @property
    def isolated(self) -> bool:
        return any(entry.isolated for entry in self.entries)


def normalized_requirement(value: str) -> str:
    if not value or len(value) > 200 or any(c in value for c in "\n\r\x00\\#;"):
        raise ValueError("Use a package name with optional extras and version constraints.")
    try:
        requirement = Requirement(value)
    except InvalidRequirement as exc:
        raise ValueError(
            "Use a package name with optional extras and version constraints."
        ) from exc
    if requirement.url or requirement.marker:
        raise ValueError("Package URLs, paths, and environment markers are not allowed.")
    name = canonicalize_name(requirement.name)
    extras = ",".join(sorted(canonicalize_name(e) for e in requirement.extras))
    return name + (f"[{extras}]" if extras else "") + str(requirement.specifier)


def normalize(packages: list[str], *, denylist: set[str] | None = None) -> PackageRequest:
    if not isinstance(packages, list) or not 1 <= len(packages) <= 10:
        raise ValueError("Request between 1 and 10 packages.")
    denied = {canonicalize_name(n) for n in denylist or ()}
    specs, entries, sources = set(), [], []
    for value in packages:
        if not isinstance(value, str):
            raise ValueError("Package specs must be strings.")
        if value.startswith("git+"):
            source = parse_source(value)
            if source not in sources:
                sources.append(source)
            continue
        entry = lookup(value.strip())
        spec = normalized_requirement(entry.spec if entry and entry.spec else value.strip())
        if canonicalize_name(Requirement(spec).name) in denied:
            raise ValueError(f"Package {Requirement(spec).name} is denied by runtime policy.")
        specs.add(spec)
        if entry and entry not in entries:
            entries.append(entry)
            if entry.source:
                sources.append(entry.source)
    if any(entry.kind == "image" for entry in entries) and (len(specs) != 1 or sources):
        raise ValueError("Request an admin-built image runtime on its own.")
    return PackageRequest(tuple(sorted(specs)), tuple(entries), tuple(sources))


def request_key(
    request: PackageRequest,
    kind: RuntimeKind,
    python_version: str,
    base_fingerprint: str,
) -> str:
    payload = {
        "kind": kind,
        "python_version": python_version,
        "base_fingerprint": base_fingerprint if kind == "overlay" else None,
        "specs": sorted(request.specs),
        "policy_version": POLICY_VERSION,
        "sources": sorted((source.url, source.commit) for source in request.sources),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def valid_runtime_id(value: str) -> bool:
    return bool(re.fullmatch(r"r_[0-9a-f]{16}", value))
