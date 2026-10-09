"""Inspect wheel metadata as bytes, without importing package code."""

from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def check_metadata(data: bytes, denied: set[str]) -> tuple[str, str]:
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Wheel metadata too large")
    metadata = BytesParser().parsebytes(data)
    name, version = canonicalize_name(str(metadata["Name"])), str(metadata["Version"])
    Requirement(f"{name}=={version}")
    if name in denied:
        raise ValueError("Package denied by runtime policy")
    for value in metadata.get_all("Requires-Dist", []):
        dependency = Requirement(str(value))
        if dependency.url or canonicalize_name(dependency.name) in denied:
            raise ValueError("Wheel has a URL or denied dependency")
    return name, version


def check_wheel(path: Path, denied: set[str], max_bytes: int) -> tuple[str, str]:
    with ZipFile(path) as archive:
        members = archive.infolist()
        if sum(m.file_size for m in members) > max_bytes:
            raise ValueError("Unpacked wheel exceeds the runtime size limit")
        metadata = [m for m in members if m.filename.endswith(".dist-info/METADATA")]
        if len(metadata) != 1 or metadata[0].file_size > 2 * 1024 * 1024:
            raise ValueError("Invalid wheel metadata")
        return check_metadata(archive.read(metadata[0]), denied)
