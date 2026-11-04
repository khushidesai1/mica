"""Inspect wheel metadata as bytes, without importing package code."""

from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile

from packaging.requirements import Requirement
from packaging.tags import Tag
from packaging.utils import canonicalize_name, parse_wheel_filename

from .runtime_policy import RuntimePolicyError
from .runtime_sdist import ADMIN_BUILD, NATIVE_SUFFIXES


def check_metadata(data: bytes, denied: set[str]) -> tuple[str, str]:
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Wheel metadata too large")
    metadata = BytesParser().parsebytes(data)
    name, version = canonicalize_name(str(metadata["Name"])), str(metadata["Version"])
    Requirement(f"{name}=={version}")
    if name in denied:
        raise RuntimePolicyError("Package denied by runtime policy")
    for value in metadata.get_all("Requires-Dist", []):
        dependency = Requirement(str(value))
        active = dependency.marker is None or dependency.marker.evaluate({"extra": ""})
        # Optional extras are checked when the resolver selects them; rejecting every
        # advertised extra would block core scvi-tools merely for advertising its S3 hub.
        if dependency.url or (active and canonicalize_name(dependency.name) in denied):
            raise RuntimePolicyError("Wheel has a URL or denied dependency")
    return name, version


def check_wheel(path: Path, denied: set[str], max_bytes: int) -> tuple[str, str]:
    with ZipFile(path) as archive:
        members = archive.infolist()
        if sum(m.file_size for m in members) > max_bytes:
            raise ValueError("Unpacked wheel exceeds the runtime size limit")
        # Wheels such as setuptools also bundle vendored distribution metadata.
        # Only the root distribution describes what this wheel installs.
        metadata = [
            m for m in members
            if m.filename.count("/") == 1 and m.filename.endswith(".dist-info/METADATA")
        ]
        if len(metadata) != 1 or metadata[0].file_size > 2 * 1024 * 1024:
            raise ValueError("Invalid wheel metadata")
        return check_metadata(archive.read(metadata[0]), denied)


def check_pure_wheel(path: Path, denied: set[str], max_bytes: int) -> tuple[str, str]:
    name, version = check_wheel(path, denied, max_bytes)
    filename_name, filename_version, _, tags = parse_wheel_filename(path.name)
    if tags != {Tag("py3", "none", "any")}:
        raise ValueError(ADMIN_BUILD)
    if name != filename_name or version != str(filename_version):
        raise ValueError("Built wheel name/version does not match its metadata")
    with ZipFile(path) as archive:
        wheels = [
            m for m in archive.infolist()
            if m.filename.count("/") == 1 and m.filename.endswith(".dist-info/WHEEL")
        ]
        if len(wheels) != 1 or wheels[0].file_size > 64 * 1024:
            raise ValueError("Invalid built wheel tag metadata")
        metadata = BytesParser().parsebytes(archive.read(wheels[0]))
        if (
            metadata["Root-Is-Purelib"] != "true"
            or set(metadata.get_all("Tag", [])) != {"py3-none-any"}
            or any(Path(m.filename).suffix.lower() in NATIVE_SUFFIXES for m in archive.infolist())
        ):
            raise ValueError(ADMIN_BUILD)
    return name, version
