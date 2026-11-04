"""Hash-locked wheel provisioning; all package execution is offline."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from . import runtime_source
from .config import Settings
from .runtime_network import WheelMirror
from .runtime_policy import (
    POLICY_VERSION,
    PackageRequest,
    RuntimeKind,
    RuntimePolicyError,
    normalized_requirement,
    request_key,
)
from .runtime_sandbox import PROBE, sandbox_command
from .runtime_sdist import ADMIN_BUILD, SourceDistribution, extract_source
from .runtime_wheels import check_pure_wheel, check_wheel


class RuntimeFailure(ValueError):
    """An actionable, bounded package setup error, safe to show to the agent."""


class ResolutionFailure(RuntimeFailure):
    pass


@dataclass
class LockedPackage:
    version: str
    line: str


def parse_lock(text: str) -> dict[str, LockedPackage]:
    packages: dict[str, LockedPackage] = {}
    for line in text.replace("\\\n", " ").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(
            r"([A-Za-z0-9._-]+)==([^\s;]+)((?:\s+--hash=sha256:[0-9a-f]{64})+)", line
        )
        if not match:
            raise RuntimeFailure("Resolution must contain only exact pins and SHA256 hashes.")
        name, version = canonicalize_name(match[1]), match[2]
        if name in packages:
            raise RuntimeFailure("Duplicate package in runtime lock")
        Requirement(f"{name}=={version}")
        packages[name] = LockedPackage(version, line)
    return packages


def directory_size(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue  # uv atomically moves cache files while size checks run.
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass
class RuntimeRecord:
    id: str
    kind: RuntimeKind
    python: str
    request: list[str]
    python_version: str
    base_fingerprint: str | None
    top_level: dict[str, str]
    packages: dict[str, str]
    validated: dict[str, Any]
    size_bytes: int = 0
    archive_sha256: str = ""
    lock_sha256: str = ""
    lock: str = "lock.txt"
    catalog: str | None = None
    policy_version: int = POLICY_VERSION
    created_by: str = ""
    created_at: str = ""
    reason: str = ""
    note: str = ""
    sources: list[dict[str, str]] = field(default_factory=list)
    source_built: list[dict[str, str]] = field(default_factory=list)


class Installer:
    def __init__(self, settings: Settings, *, wheelhouse: Path | None = None) -> None:
        self.settings = settings
        # Constructor-only test dependency, never configurable by the agent or environment.
        self.wheelhouse = wheelhouse
        self.uv = shutil.which("uv") or str(Path(sys.executable).parent / "uv")
        self._base: tuple[str, str, dict[str, str]] | None = None
        self.deadline = 0.0
        self.scratch = Path("/tmp")
        self.network: dict[str, str] = {}
        self.mirror: WheelMirror | None = None
        self.built: Path | None = None
        self.source_built: list[dict[str, str]] = []

    def environment(self) -> dict[str, str]:
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.scratch),
            "UV_CACHE_DIR": str(self.scratch / "uv-cache"),
            "UV_PYTHON_DOWNLOADS": "never",
            "UV_NO_CONFIG": "1",
            "UV_HTTP_RETRIES": "1",
            # Bound mirror/NAT pressure and leave room for its upstream 30-second timeout.
            # The overall installer deadline still bounds all attempts.
            "UV_CONCURRENT_DOWNLOADS": "4",
            "UV_HTTP_TIMEOUT": "45",
            "UV_NO_PROGRESS": "1",
            "UV_KEYRING_PROVIDER": "disabled",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_TERMINAL_PROMPT": "0",
            **self.network,
        }

    def run(self, args: list[str], *, resolution: bool = False, git: bool = False) -> str:
        log = self.scratch / "command.log"
        env = self.environment()
        if git:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
                env.pop(key, None)
            env.update(GIT_ALLOW_PROTOCOL="https", GIT_LFS_SKIP_SMUDGE="1")
        with log.open("wb") as output:
            process = subprocess.Popen(
                args,
                cwd=self.scratch,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                while process.poll() is None:
                    if time.monotonic() >= self.deadline:
                        raise RuntimeFailure("Package setup exceeded its time limit.")
                    self.check_size()
                    if log.stat().st_size > 8 * 1024 * 1024:
                        raise RuntimeFailure("Package command produced too much output.")
                    time.sleep(0.05)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        if log.stat().st_size > 8 * 1024 * 1024:
            raise RuntimeFailure("Package command produced too much output.")
        with log.open("rb") as stream:
            # Successful freeze/list output defines the entire base constraint set.
            # Truncate only diagnostic output from a failed subprocess.
            if process.returncode:
                stream.seek(max(0, log.stat().st_size - 32_000))
            text = stream.read().decode(errors="replace")
        if process.returncode:
            error = "\n".join(text.splitlines()[-30:])
            cls = ResolutionFailure if resolution else RuntimeFailure
            raise cls(error)
        return text

    def check_size(self) -> None:
        if directory_size(self.scratch) > self.settings.runtime_max_gb * 1024**3:
            raise RuntimeFailure("Runtime scratch exceeds the package size limit.")
        if (
            directory_size(self.settings.runtime_root)
            > self.settings.runtime_max_local_gb * 1024**3
        ):
            raise RuntimeFailure("Local runtime storage limit exceeded.")
        for path in self.settings.runtime_root.glob("r_*"):
            if directory_size(path) > self.settings.runtime_max_gb * 1024**3:
                raise RuntimeFailure("Runtime exceeds the package size limit.")

    def identity(self) -> tuple[str, str, dict[str, str]]:
        if self._base is None:
            self.settings.runtime_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=".base-", dir=self.settings.runtime_root
            ) as temp:
                self.scratch = Path(temp)
                self.deadline = time.monotonic() + self.settings.runtime_timeout_seconds
                self.base()
        assert self._base is not None
        return self._base

    def base(self) -> tuple[str, str, dict[str, str]]:
        if self._base is None:
            # Only the reviewed base interpreter is executed here, never installed package code.
            version = self.run(
                [
                    str(self.settings.sci_python),
                    "-I",
                    "-S",
                    "-c",
                    "import platform; print(platform.python_version())",
                ]
            ).strip()
            if not version.startswith("3.12."):
                raise RuntimeFailure("Package runtimes require Python 3.12.")
            frozen = self.run(
                [
                    self.uv,
                    "pip",
                    "freeze",
                    "--quiet",
                    "--no-config",
                    "--offline",
                    "--python",
                    str(self.settings.sci_python),
                ]
            )
            listed = self.run(
                [
                    self.uv,
                    "pip",
                    "list",
                    "--format",
                    "freeze",
                    "--quiet",
                    "--no-config",
                    "--offline",
                    "--python",
                    str(self.settings.sci_python),
                ]
            )
            pins: dict[str, str] = {}
            for line in listed.splitlines():
                if re.fullmatch(r"[A-Za-z0-9._-]+==[^\s]+", line):
                    name, pin = line.split("==", 1)
                    pins[canonicalize_name(name)] = pin
                elif line and not line.startswith("#"):
                    raise RuntimeFailure("Base environment must contain exact public package pins.")
            canonical = "\n".join(f"{name}=={pin}" for name, pin in sorted(pins.items()))
            digest = hashlib.sha256(
                (version + "\n" + canonical + "\n" + frozen).encode()
            ).hexdigest()
            self._base = version, digest, pins
        return self._base

    def index_args(self) -> list[str]:
        if self.wheelhouse:
            return [
                "--offline",
                "--no-index",
                "--find-links",
                str(self.wheelhouse),
                *(["--find-links", str(self.built)] if self.built else []),
            ]
        return ["--default-index", self.network["HTTP_PROXY"] + "/simple"]

    def skeleton(self, path: Path, kind: RuntimeKind) -> None:
        self.run(
            [
                self.uv,
                "venv",
                "--no-config",
                "--offline",
                "--no-python-downloads",
                "--python",
                str(self.settings.sci_python),
                *(["--system-site-packages"] if kind == "overlay" else []),
                str(path),
            ]
        )

    def resolve(self, specs: list[str], kind: RuntimeKind, base: dict[str, str]) -> str:
        requirements = self.scratch / "requirements.in"
        requirements.write_text("\n".join(specs) + "\n")
        constraints = self.scratch / "base-constraints.txt"
        constraints.write_text("\n".join(f"{k}=={v}" for k, v in sorted(base.items())) + "\n")
        lock = self.scratch / "lock.txt"
        lock.unlink(missing_ok=True)
        age = (datetime.now(UTC) - timedelta(days=self.settings.runtime_min_age_days)).isoformat()
        self.run(
            [
                self.uv,
                "pip",
                "compile",
                str(requirements),
                "--python",
                str(self.settings.sci_python),
                "--only-binary",
                ":all:",
                "--generate-hashes",
                "--no-config",
                "--no-sources",
                "--no-header",
                "--no-annotate",
                "--no-python-downloads",
                "--exclude-newer",
                age,
                *self.index_args(),
                # uv caches empty simple-index responses from the initial wheel-only solve.
                # Refresh only our newly published local wheels before the one retry.
                *[
                    arg
                    for name in (self.mirror.local_wheels if self.mirror else ())
                    for arg in ("--refresh-package", name)
                ],
                *(["-c", str(constraints)] if kind == "overlay" else []),
                "-o",
                str(lock),
            ],
            resolution=True,
        )
        return lock.read_text()

    def validate(
        self,
        path: Path,
        imports: tuple[str, ...],
        smoke: tuple[Path, ...] = (),
    ) -> dict[str, str]:
        if not shutil.which("bwrap"):
            raise RuntimeFailure("Offline validator unavailable: bubblewrap is required.")
        script = (
            PROBE
            + "\nimport importlib, json\nversions = {}\n"
            + f"for name in {imports!r}:\n"
            + "    module = importlib.import_module(name)\n"
            + "    versions[name] = str(getattr(module, '__version__', 'unknown'))\n"
            + "print(json.dumps(versions))\n"
        )
        output = self.run(
            sandbox_command(
                path,
                [str(path / "bin/python"), "-I", "-c", script],
                read_only=(
                    self.settings.sci_python.parent.parent,
                    self.settings.sci_python.resolve().parent.parent,
                ),
            )
        )
        versions: dict[str, str] = json.loads(output.splitlines()[-1])
        for script_path in smoke:
            self.run(
                sandbox_command(
                    path, [str(path / "bin/python"), str(script_path)], read_only=(script_path,)
                )
            )
        return versions

    def create(
        self,
        request: PackageRequest,
        *,
        reason: str,
        isolated: bool = False,
        principal: str = "",
    ) -> RuntimeRecord:
        request = self.pin_request(request)
        self.settings.runtime_root.mkdir(parents=True, exist_ok=True)
        self.deadline = time.monotonic() + self.settings.runtime_timeout_seconds
        self.source_built = []
        with tempfile.TemporaryDirectory(
            prefix=".setup-", dir=self.settings.runtime_root
        ) as scratch:
            self.scratch = Path(scratch)
            with (
                nullcontext(None)
                if self.wheelhouse
                else WheelMirror(
                    self.scratch / "downloads",
                    self.settings.csv("runtime_denylist"),
                    int(self.settings.runtime_max_gb * 1024**3),
                    min_age_days=self.settings.runtime_min_age_days,
                )
            ) as mirror:
                self.network = (
                    {
                        "HTTP_PROXY": mirror.url,
                        "HTTPS_PROXY": mirror.url,
                        "ALL_PROXY": mirror.url,
                        "NO_PROXY": "",
                    }
                    if mirror
                    else {}
                )
                self.mirror = mirror
                try:
                    return self._create(request, reason, isolated, principal)
                finally:
                    self.network = {}
                    self.mirror = None
                    self.built = None

    def _create(
        self,
        request: PackageRequest,
        reason: str,
        isolated: bool,
        principal: str,
    ) -> RuntimeRecord:
        version, fingerprint, pins = self.base()
        if request.sources:
            request = self.build_sources(request)
        if any(e.kind == "image" for e in request.entries):
            return self.image_record(request, reason, principal)
        kind: RuntimeKind = "isolated" if isolated or request.isolated else "overlay"
        specs = list(request.specs)
        note = ""
        if kind == "isolated":
            specs += ["matplotlib", "pandas", "anndata"]
        try:
            lock = self.resolve_runtime(specs, kind, pins, version)
        except ResolutionFailure as exc:
            if kind != "overlay" or "No solution found" not in str(exc):
                raise
            note = "Base constraints conflict; using isolated runtime. " + str(exc)[-1500:]
            kind = "isolated"
            lock = self.resolve_runtime(
                specs + ["matplotlib", "pandas", "anndata"], kind, pins, version
            )
        packages = parse_lock(lock)
        denied: set[str] = {canonicalize_name(n) for n in self.settings.csv("runtime_denylist")}
        if denied & packages.keys():
            raise RuntimePolicyError("Resolution includes a package denied by runtime policy.")
        key = request_key(request, kind, version, fingerprint)
        path = self.settings.runtime_root / ("r_" + key[:16])
        if path.exists():
            raise RuntimeFailure("Runtime already exists; use the validated runtime store.")
        try:
            self.skeleton(path, kind)
            (path / "lock.txt").write_text(lock)
            install = self.scratch / "install.txt"
            install.write_text(
                "\n".join(
                    p.line
                    for name, p in packages.items()
                    if kind != "overlay" or pins.get(name) != p.version
                )
                + "\n"
            )
            # Exactly one install into an empty venv. No subsequent online Python discovery
            # may execute .pth files from the newly installed packages.
            self.run(
                [
                    self.uv,
                    "pip",
                    "install",
                    "--python",
                    str(path / "bin/python"),
                    "--no-deps",
                    "--require-hashes",
                    "--only-binary",
                    ":all:",
                    "--no-config",
                    "--no-sources",
                    "--no-python-downloads",
                    *self.index_args(),
                    "-r",
                    str(install),
                ]
            )
            imports = self.import_names(request, path)
            smoke = tuple(self.settings.tools_dir / e.smoke for e in request.entries if e.smoke)
            validated = self.validate(path, imports, smoke)
            self.check_size()
            now = datetime.now(UTC).isoformat()
            top: dict[str, str] = {
                canonicalize_name(Requirement(s).name): packages[
                    canonicalize_name(Requirement(s).name)
                ].version
                for s in request.specs
            }
            return RuntimeRecord(
                id=path.name,
                kind=kind,
                python=str(path / "bin/python"),
                request=list(request.specs),
                python_version=version,
                base_fingerprint=fingerprint if kind == "overlay" else None,
                top_level=top,
                packages={k: p.version for k, p in packages.items()},
                validated={"imports": list(imports), "versions": validated, "ok": True, "at": now},
                size_bytes=directory_size(path),
                lock_sha256=file_sha256(path / "lock.txt"),
                catalog=", ".join(e.name for e in request.entries) or None,
                created_by=hashlib.sha256(principal.encode()).hexdigest(),
                created_at=now,
                reason=reason,
                note=note,
                source_built=list(self.source_built),
                sources=[
                    {"url": source.url, "commit": source.commit} for source in request.sources
                ],
            )
        except BaseException:
            shutil.rmtree(path, ignore_errors=True)
            raise

    def image_record(self, request: PackageRequest, reason: str, principal: str) -> RuntimeRecord:
        entry = request.entries[0]
        if not entry.python or not Path(entry.python).is_file():
            raise RuntimeFailure(f"{entry.name} needs an admin build; its interpreter is absent.")
        path = Path(entry.python).parent.parent
        packages: dict[str, str] = {
            canonicalize_name(d.metadata["Name"]): d.version
            for d in importlib.metadata.distributions(
                path=[str(path / "lib/python3.12/site-packages")]
            )
        }
        top: dict[str, str] = {}
        for spec in request.specs:
            requirement = Requirement(spec)
            name = canonicalize_name(requirement.name)
            if name not in packages or not requirement.specifier.contains(packages[name]):
                raise RuntimeFailure("Admin runtime does not match the catalog version")
            top[name] = packages[name]
        smoke = (self.settings.tools_dir / entry.smoke,) if entry.smoke else ()
        versions = self.validate(path, entry.import_names, smoke)
        version, fingerprint, _ = self.base()
        now = datetime.now(UTC).isoformat()
        lock = "\n".join(f"{n}=={v}" for n, v in sorted(packages.items()))
        return RuntimeRecord(
            id="r_" + request_key(request, "image", version, fingerprint)[:16],
            kind="image",
            python=entry.python,
            request=list(request.specs),
            python_version=version,
            base_fingerprint=None,
            top_level=top,
            packages=dict(packages),
            validated={
                "imports": list(entry.import_names),
                "versions": versions,
                "ok": True,
                "at": now,
            },
            lock_sha256=hashlib.sha256(lock.encode()).hexdigest(),
            catalog=entry.name,
            created_by=hashlib.sha256(principal.encode()).hexdigest(),
            created_at=now,
            reason=reason,
            note="Validated the admin-built image runtime.",
        )

    def pin_request(self, request: PackageRequest) -> PackageRequest:
        if not request.sources or all(
            re.fullmatch(r"[0-9a-f]{40}", s.commit) for s in request.sources
        ):
            return request
        self.settings.runtime_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".refs-", dir=self.settings.runtime_root) as temp:
            self.scratch = Path(temp)
            self.deadline = time.monotonic() + self.settings.runtime_timeout_seconds
            sources = tuple(
                runtime_source.pin_source(s, lambda args: self.run(args, git=True))
                for s in request.sources
            )
        return replace(request, sources=sources)

    def resolve_runtime(
        self,
        specs: list[str],
        kind: RuntimeKind,
        base: dict[str, str],
        python_version: str,
    ) -> str:
        try:
            return self.resolve(specs, kind, base)
        except ResolutionFailure as exc:
            if not self.mirror:
                raise
            if self.mirror.policy_errors:
                raise RuntimePolicyError(self.mirror.policy_errors[-1]) from exc
            candidates = self.mirror.source_candidates(str(exc), specs, python_version)
            if not candidates:
                raise
            if self.source_built:
                raise RuntimeFailure(
                    "Source-build retry did not resolve the dependencies. " + ADMIN_BUILD
                ) from exc
            if len(candidates) > self.settings.runtime_max_source_builds:
                raise RuntimePolicyError(
                    "Runtime source-build limit exceeded; request an admin-built runtime."
                ) from exc
            for source in candidates:
                self.build_sdist(source)
            # One retry; the pinned wheel-only solver remains authoritative for every dependency.
            return self.resolve(specs, kind, base)

    def build_sdist(self, source: SourceDistribution) -> None:
        assert self.mirror is not None
        archive = self.scratch / source.filename
        self.mirror.fetch_source(source, archive)
        source_path = extract_source(
            source,
            archive,
            self.scratch / ("source-pypi-" + source.name),
            int(self.settings.runtime_max_gb * 1024**3),
        )
        try:
            wheel, name, version = self.build_wheel(source_path, "pypi-" + source.name, pure=True)
        except RuntimePolicyError:
            raise
        except (RuntimeFailure, ValueError) as exc:
            if self.mirror.policy_errors:
                raise RuntimePolicyError(self.mirror.policy_errors[-1]) from exc
            raise RuntimeFailure(ADMIN_BUILD + " " + str(exc)[-1500:]) from exc
        if (name, version) != (source.name, source.version):
            raise RuntimeFailure("Built wheel does not match the pinned PyPI source")
        self.publish_built(wheel)
        self.source_built.append(
            {
                "name": name,
                "version": version,
                "url": source.url,
                "source_sha256": source.sha256,
                "wheel_sha256": file_sha256(wheel),
            }
        )

    def publish_built(self, wheel: Path) -> None:
        assert self.built is not None
        installed = self.built / wheel.name
        shutil.copyfile(wheel, installed)
        if self.mirror:
            self.mirror.add_wheel(installed)

    def build_sources(self, request: PackageRequest) -> PackageRequest:
        specs = set(request.specs)
        for i, source in enumerate(request.sources):
            source_path = self.scratch / f"source-{i}"
            runtime_source.checkout(source, source_path, lambda args: self.run(args, git=True))
            wheel, name, version = self.build_wheel(source_path, f"catalog-{i}")
            expected = [e for e in request.entries if e.source == source and e.spec]
            if expected and not all(
                Requirement(e.spec or "").specifier.contains(version)
                and canonicalize_name(Requirement(e.spec or "").name) == name
                for e in expected
            ):
                raise RuntimeFailure("Built wheel does not match the reviewed catalog version")
            self.publish_built(wheel)
            specs.add(f"{name}=={version}")
        return replace(request, specs=tuple(sorted(specs)))

    def build_wheel(
        self,
        source_path: Path,
        label: str,
        *,
        pure: bool = False,
    ) -> tuple[Path, str, str]:
        if not shutil.which("bwrap"):
            raise RuntimeFailure("Offline source builder unavailable: bubblewrap is required.")
        self.built = self.scratch / "built"
        self.built.mkdir(exist_ok=True)
        denied: set[str] = {canonicalize_name(n) for n in self.settings.csv("runtime_denylist")}
        pyproject = source_path / "pyproject.toml"
        if pyproject.exists() and pyproject.stat().st_size > 64 * 1024:
            raise RuntimeFailure("Source build configuration is too large")
        config = tomllib.loads(pyproject.read_text()) if pyproject.exists() else {}
        build_system = config.get("build-system", {})
        if not isinstance(build_system, dict):
            raise RuntimeFailure("Invalid source build configuration")
        raw = build_system.get("requires", ["setuptools", "wheel"])
        if not isinstance(raw, list) or len(raw) > 20 or not all(isinstance(v, str) for v in raw):
            raise RuntimeFailure("Invalid source build requirements")
        requirements = [normalized_requirement(value) for value in raw] + ["pip"]
        if any(canonicalize_name(Requirement(r).name) in denied for r in requirements):
            raise RuntimePolicyError("Build requirement denied by runtime policy")
        lock = self.resolve(requirements, "isolated", {})
        build_packages = parse_lock(lock)
        if denied & build_packages.keys():
            raise RuntimePolicyError("Build dependency denied by runtime policy")
        build_env = self.scratch / f"build-env-{label}"
        self.skeleton(build_env, "isolated")
        build_lock = self.scratch / f"build-{label}.txt"
        build_lock.write_text(lock)
        self.run(
            [
                self.uv,
                "pip",
                "install",
                "--python",
                str(build_env / "bin/python"),
                "--no-deps",
                "--require-hashes",
                "--only-binary",
                ":all:",
                "--no-config",
                "--no-sources",
                "--no-python-downloads",
                *self.index_args(),
                "-r",
                str(build_lock),
            ]
        )
        destination = self.scratch / f"wheel-output-{label}"
        destination.mkdir()
        args = [
            "pip",
            "wheel",
            "--no-deps",
            "--no-index",
            "--no-build-isolation",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--find-links",
            str(self.built),
            "--wheel-dir",
            str(destination),
            str(source_path),
        ]
        script = (
            PROBE
            + f"\nimport sys, runpy\nsys.argv = {args!r}\n"
            + ("runpy.run_module('pip', run_name='__main__')\n")
        )
        self.run(
            sandbox_command(
                build_env,
                [str(build_env / "bin/python"), "-I", "-c", script],
                read_only=(self.settings.sci_python.resolve().parent.parent,),
                writable=(source_path, destination),
            )
        )
        wheels = list(destination.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeFailure("Source build must produce exactly one wheel")
        wheel = wheels[0]
        if any(path.is_symlink() for path in destination.rglob("*")):
            raise RuntimeFailure("Build output must not contain symlinks")
        validator = check_pure_wheel if pure else check_wheel
        name, version = validator(wheel, denied, int(self.settings.runtime_max_gb * 1024**3))
        return wheel, name, version

    def import_names(self, request: PackageRequest, path: Path) -> tuple[str, ...]:
        known = {
            canonicalize_name(Requirement(e.spec).name): e.import_names
            for e in request.entries
            if e.spec
        }
        paths = [path / "lib/python3.12/site-packages"]
        if self._base:
            paths.append(self.settings.sci_python.parent.parent / "lib/python3.12/site-packages")
            paths.append(
                self.settings.sci_python.resolve().parent.parent / "lib/python3.12/site-packages"
            )
        distributions = {
            canonicalize_name(d.metadata["Name"]): d
            for d in importlib.metadata.distributions(path=[str(p) for p in reversed(paths)])
        }
        imports: set[str] = set()
        for spec in request.specs:
            name = canonicalize_name(Requirement(spec).name)
            if name in known:
                imports.update(known[name])
                continue
            dist = distributions.get(name)
            top = (dist.read_text("top_level.txt") or "") if dist else ""
            candidates = top.splitlines() or (
                [str(f).split("/")[0].removesuffix(".py") for f in dist.files or ()] if dist else []
            )
            modules = {n for n in candidates if n.isidentifier() and n != "__pycache__"}
            imports.update(modules or {name.replace("-", "_")})
        return tuple(sorted(imports))
