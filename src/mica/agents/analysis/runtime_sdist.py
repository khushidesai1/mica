"""Validate pinned PyPI source archives before an offline, credential-free build."""

from __future__ import annotations

import hashlib
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

ADMIN_BUILD = (
    "This package needs an admin-built runtime; only pure-Python source builds are supported."
)
NATIVE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".cu",
    ".f",
    ".f90",
    ".for",
    ".rs",
    ".so",
    ".dylib",
    ".dll",
    ".pyd",
    ".a",
}


@dataclass(frozen=True)
class SourceDistribution:
    name: str
    version: str
    filename: str
    url: str
    sha256: str


def extract_source(
    source: SourceDistribution, archive: Path, destination: Path, max_bytes: int
) -> Path:
    with archive.open("rb") as hash_stream:
        if hashlib.file_digest(hash_stream, "sha256").hexdigest() != source.sha256:
            raise ValueError("Source archive SHA256 does not match the PyPI index")
    names: set[str] = set()
    roots: set[str] = set()
    total = 0

    def target(name: str, size: int, regular: bool, directory: bool) -> Path:
        nonlocal total
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or "\\" in name
            or name in names
            or not (regular or directory)
            or len(names) >= 100_000
        ):
            raise ValueError("Unsafe source archive member")
        if path.suffix.lower() in NATIVE_SUFFIXES:
            raise ValueError(ADMIN_BUILD)
        names.add(name)
        roots.add(path.parts[0])
        total += size
        if total > max_bytes:
            raise ValueError("Unpacked source exceeds the runtime size limit")
        output = destination.joinpath(*path.parts)
        output.parent.mkdir(parents=True, exist_ok=True)
        return output

    destination.mkdir()
    if source.filename.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                output = target(member.name, member.size, member.isfile(), member.isdir())
                if member.isdir():
                    output.mkdir(exist_ok=True)
                else:
                    stream = tar.extractfile(member)
                    if stream is None:
                        raise ValueError("Missing source archive member")
                    with stream, output.open("xb") as out:
                        shutil.copyfileobj(stream, out)
    elif source.filename.endswith(".zip"):
        with ZipFile(archive) as zipfile:
            for zip_member in zipfile.infolist():
                mode = zip_member.external_attr >> 16
                regular = (mode & 0o170000) in {0, 0o100000}
                directory = zip_member.is_dir()
                output = target(zip_member.filename, zip_member.file_size, regular, directory)
                if directory:
                    output.mkdir(exist_ok=True)
                else:
                    # ZipFile.open verifies CRC while reading the pinned bytes.
                    with zipfile.open(zip_member) as zip_stream, output.open("xb") as out:
                        shutil.copyfileobj(zip_stream, out)
    else:
        raise ValueError("Unsupported source archive format")
    if len(roots) != 1:
        raise ValueError("Source archive must have one project directory")
    root = destination / next(iter(roots))
    if not root.is_dir() or not any((root / p).is_file() for p in ("pyproject.toml", "setup.py")):
        raise ValueError("Source archive has no build configuration")
    return root
