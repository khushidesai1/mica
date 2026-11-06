"""Reviewed public tools. Catalog membership does not imply an installed runtime."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .config import Settings
    from .runtime_install import RuntimeRecord
    from .storage import Storage

BUNDLED_SKILLS = (
    "mica:spatial-analysis", "mica:cellpose", "mica:project-knowledge", "mica:segmentation-report"
)


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
        notes="Existing SAP environment; use the Cellpose skill.",
    ),
    ToolkitEntry(
        "Cellpose-SAM",
        "cellpose==4.2.1.1",
        ("cellpose",),
        "BSD-3-Clause",
        kind="image",
        python="/opt/cellpose/4.2.1.1/bin/python",
        notes="Separate runtime and pinned cpsam/cpsam_v2 weights; use the Cellpose skill.",
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


@dataclass(frozen=True)
class Environment:
    """One named interpreter; user input can select an id, never an executable path."""

    id: str
    python: str
    cuda: bool
    packages: tuple[str, ...]
    assets: tuple[str, ...] = ()


def runtime_cuda(packages: dict[str, str]) -> bool:
    return any(
        name.startswith(("nvidia-cuda-runtime-", "jax-cuda", "cupy-cuda")) for name in packages
    ) and not packages.get("torch", "").endswith("+cpu")


# Match the GPU image lockfiles; these versions describe the installed stack, not a driver test.
CUDA_IMAGE_PACKAGES = ("torch 2.7.1+cu126", "nvidia-cuda-runtime-cu12 12.6.77")


def environment_descriptor(record: RuntimeRecord) -> dict[str, Any]:
    """Bounded discovery metadata; it never replaces immutable manifest validation."""
    return {
        "version": 1,
        "policy_version": record.policy_version,
        "python": record.python,
        "python_version": record.python_version,
        "base_fingerprint": record.base_fingerprint,
        "validated": record.validated.get("ok") is True,
        "top_level": dict(sorted(record.top_level.items())[:10]),
        "cuda": runtime_cuda(record.packages),
    }


def descriptor_environment(name: str, raw: Any, settings: Settings) -> Environment | None:
    from .runtime_policy import POLICY_VERSION, valid_runtime_id

    if (
        not valid_runtime_id(name)
        or not isinstance(raw, dict)
        or raw.get("version") != 1
        or raw.get("policy_version") != POLICY_VERSION
        or raw.get("validated") is not True
        or type(raw.get("cuda")) is not bool
        or raw.get("python") != str(settings.runtime_root / name / "bin/python")
    ):
        return None
    packages = raw.get("top_level")
    if (
        not isinstance(packages, dict)
        or len(packages) > 10
        or any(
            not isinstance(value, str) or not 1 <= len(value) <= 200
            for item in packages.items()
            for value in item
        )
    ):
        return None
    return Environment(
        name,
        raw["python"],
        raw["cuda"],
        tuple(f"{package} {version}" for package, version in sorted(packages.items())),
    )


def environments(
    settings: Settings, records: Iterable[RuntimeRecord | Environment] = ()
) -> list[Environment]:
    """The same image/runtime inventory is used by API guidance and worker tools."""
    gpu = settings.gpu_worker
    rows = [
        Environment(
            "science",
            str(settings.sci_python),
            False,
            ("NumPy", "SciPy", "scanpy", "scvi-tools", "squidpy"),
        ),
        Environment("sap", "/opt/sap/bin/python", False, ("sap", "cellpose 3.1.1.3")),
        Environment(
            "cellpose-3.1.1.3",
            "/opt/cellpose/3.1.1.3/bin/python" if gpu else "/opt/sap/bin/python",
            gpu,
            ("cellpose 3.1.1.3", *(CUDA_IMAGE_PACKAGES if gpu else ("torch",))),
            ("/opt/sap/models",),
        ),
        Environment(
            "cellpose-4.2.1.1",
            "/opt/cellpose/4.2.1.1/bin/python",
            gpu,
            ("cellpose 4.2.1.1", *(CUDA_IMAGE_PACKAGES if gpu else ("torch",))),
            ("/opt/cellpose/models", "/opt/cellpose/manifest.json"),
        ),
    ]
    if settings.runtimes_enabled:
        from .runtime_policy import valid_runtime_id

        for record in records:
            if (
                not valid_runtime_id(record.id)
                or Path(record.python) != settings.runtime_root / record.id / "bin/python"
            ):
                continue
            if isinstance(record, Environment):
                rows.append(
                    Environment(record.id, record.python, gpu and record.cuda, record.packages)
                )
                continue
            if record.kind == "image" or not record.validated.get("ok"):
                continue
            rows.append(
                Environment(
                    record.id,
                    record.python,
                    gpu and runtime_cuda(record.packages),
                    tuple(
                        f"{name} {version}" for name, version in sorted(record.top_level.items())
                    ),
                )
            )
    return rows


def environment_view(
    settings: Settings, records: Iterable[RuntimeRecord | Environment] = ()
) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in asdict(row).items() if key != "assets"}
        for row in environments(settings, records)
    ]


def environment_prompt(settings: Settings, records: Iterable[RuntimeRecord] = ()) -> str:
    return "\n".join(
        [
            "## Python environments",
            "Use an environment id with run_gpu; Bash uses the listed interpreter for CPU work.",
            *(
                f"- {row.id}: {row.python}; CUDA {'yes' if row.cuda else 'no'}; "
                + ", ".join(row.packages)
                for row in environments(settings, records)
            ),
        ]
    )


def saved_runtimes(storage: Storage, settings: Settings) -> list[RuntimeRecord | Environment]:
    """Advertise validated saved environments; workers restore them with ensure_packages."""
    if not settings.runtimes_enabled:
        return []
    import logging

    from .runtime_store import RuntimeStore

    store = RuntimeStore(storage, settings)
    try:
        rows: list[RuntimeRecord | Environment] = []
        index = store.index()
        names = sorted(
            (name for name, value in index.items() if isinstance(value, dict)),
            key=lambda name: str(index[name].get("created_at", "")),
            reverse=True,
        )[:100]
        legacy = 0
        for name in names:
            if "environment" in index[name]:
                if descriptor := descriptor_environment(name, index[name]["environment"], settings):
                    rows.append(descriptor)
                continue
            if legacy >= 3:
                continue  # Older entries remain restorable through ensure_packages.
            legacy += 1
            try:
                rows.append(store.load(name))
            except (FileNotFoundError, ValueError):
                continue
        return rows
    except Exception:
        logging.getLogger(__name__).warning("Saved environment catalog unavailable")
        return []
