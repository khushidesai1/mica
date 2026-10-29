"""Reviewed public tools. Catalog membership does not imply an installed runtime."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Source:
    url: str
    commit: str


@dataclass(frozen=True)
class ToolkitEntry:
    name: str
    spec: str | None
    import_names: tuple[str, ...]
    license: str
    isolated: bool = False
    source: Source | None = None
    smoke: str | None = None
    kind: Literal["pip", "image"] = "pip"
    python: str | None = None
    notes: str = ""


CATALOG = (
    ToolkitEntry(
        "Cellpose 3",
        "cellpose==3.1.1.3",
        ("cellpose",),
        "BSD-3-Clause",
        kind="image",
        python="/opt/sap/bin/python",
        notes="Versioned segmentation via cellpose_segment; existing SAP environment.",
    ),
    ToolkitEntry(
        "Cellpose-SAM",
        "cellpose==4.2.1.1",
        ("cellpose",),
        "BSD-3-Clause",
        kind="image",
        python="/opt/cellpose/4.2.1.1/bin/python",
        notes="Separate versioned runtime and pinned cpsam/cpsam_v2 weights; use cellpose_segment.",
    ),
    ToolkitEntry("Starfysh", "starfysh==1.2.0", ("starfysh",), "BSD-3-Clause"),
    ToolkitEntry("Echidna", "sc-echidna==1.0.3", ("echidna",), "MIT"),
    ToolkitEntry("Amici", "amici-st==0.1.0", ("amici",), "CC-BY-NC-ND-4.0", isolated=True),
    ToolkitEntry(
        "Decipher",
        "scdecipher==0.1.3",
        ("decipher",),
        "MIT",
        isolated=True,
        source=Source(
            "https://github.com/azizilab/decipher",
            "9610b317844c8515e077a81abafea07c7494a54c",
        ),
    ),
    ToolkitEntry(
        "sap",
        "sap==0.1.0",
        ("sap",),
        "unknown",
        kind="image",
        python="/opt/sap/bin/python",
        smoke="spatial_smoke.py",
        notes="Admin-built spatial analysis image; interpreter and smoke confirmed by operator.",
    ),
)


def lookup(alias: str) -> ToolkitEntry | None:
    return next((entry for entry in CATALOG if entry.name.casefold() == alias.casefold()), None)
