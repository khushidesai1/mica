"""Read-only, metadata-only inspection of explicitly supplied AnnData objects."""

from __future__ import annotations

import importlib
import io
import re
from typing import Any

from google.cloud import storage

from pistachio.models import DatasetField, DatasetInspection

GCS_H5AD = re.compile(r"gs://([^/]+)/([^\s?#]+\.h5ad)(?:[?#][^\s]*)?", re.IGNORECASE)


class GCSRangeReader(io.RawIOBase):
    """Seekable, generation-pinned range reader with a small read-through cache."""

    def __init__(self, blob: Any, size: int, generation: int, chunk_size: int = 4 << 20) -> None:
        self.blob = blob
        self.size = size
        self.generation = generation
        self.chunk_size = chunk_size
        self.position = 0
        self.cache_start = -1
        self.cache = b""

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            position = offset
        elif whence == io.SEEK_CUR:
            position = self.position + offset
        elif whence == io.SEEK_END:
            position = self.size + offset
        else:
            raise ValueError(f"unsupported whence: {whence}")
        if position < 0:
            raise ValueError("negative seek position")
        self.position = min(position, self.size)
        return self.position

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def read(self, size: int = -1) -> bytes:
        if self.position >= self.size:
            return b""
        if size < 0:
            size = self.size - self.position
        remaining = min(size, self.size - self.position)
        pieces: list[bytes] = []
        while remaining:
            if not (self.cache_start <= self.position < self.cache_start + len(self.cache)):
                start = (self.position // self.chunk_size) * self.chunk_size
                end = min(self.size, start + self.chunk_size) - 1
                self.cache = self.blob.download_as_bytes(
                    start=start,
                    end=end,
                    if_generation_match=self.generation,
                )
                self.cache_start = start
            offset = self.position - self.cache_start
            take = min(remaining, len(self.cache) - offset)
            if take <= 0:
                break
            pieces.append(self.cache[offset : offset + take])
            self.position += take
            remaining -= take
        return b"".join(pieces)


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _shape(node: Any) -> list[int]:
    h5py = importlib.import_module("h5py")
    if isinstance(node, h5py.Dataset):
        return list(node.shape)
    if isinstance(node, h5py.Group) and "data" in node:
        return list(node["data"].shape)
    return []


def _categories(node: Any, limit: int = 30) -> list[str]:
    h5py = importlib.import_module("h5py")
    if not isinstance(node, h5py.Group) or "categories" not in node:
        return []
    categories = node["categories"]
    if not isinstance(categories, h5py.Dataset) or len(categories.shape) != 1:
        return []
    count = min(categories.shape[0], limit)
    return [_decode(item) for item in categories[:count]]


def recommend_fields(
    obs_fields: list[DatasetField], obsm_fields: list[DatasetField]
) -> tuple[str, str, str]:
    def best(names: tuple[str, ...], fields: list[DatasetField]) -> str:
        ranked = sorted(
            fields,
            key=lambda field: (
                not any(token in field.name.lower() for token in names),
                abs(len(field.categories) - 4) if "sample" in names else 0,
                len(field.name),
            ),
        )
        if not ranked or not any(token in ranked[0].name.lower() for token in names):
            return ""
        return ranked[0].name

    sample = best(("sample", "library", "batch", "slide"), obs_fields)
    if not sample:
        four_level = [field.name for field in obs_fields if len(field.categories) == 4]
        sample = four_level[0] if len(four_level) == 1 else ""
    annotation = best(("l2", "level_2", "level2"), obs_fields)
    spatial = best(("spatial",), obsm_fields)
    return sample, annotation, spatial


class DatasetInspector:
    """Inspect only H5AD schema metadata from GCS; never materialize assay matrices."""

    def __init__(self, project: str | None = None) -> None:
        self.project = project

    def inspect_prompt(self, prompt: str) -> list[DatasetInspection]:
        return [
            self.inspect_uri(f"gs://{bucket}/{name}")
            for bucket, name in dict.fromkeys(GCS_H5AD.findall(prompt))
        ]

    def inspect_uri(self, uri: str) -> DatasetInspection:
        match = GCS_H5AD.fullmatch(uri)
        if match is None:
            return DatasetInspection(uri=uri, inspection_error="Unsupported dataset URI")
        bucket_name, object_name = match.groups()
        client = storage.Client(project=self.project or None)
        blob = client.bucket(bucket_name).blob(object_name)
        try:
            h5py = importlib.import_module("h5py")
            blob.reload()
            generation = int(blob.generation or 0)
            size = int(blob.size or 0)
            reader = GCSRangeReader(blob, size, generation)
            with h5py.File(reader, "r") as handle:
                obs = handle.get("obs")
                obsm = handle.get("obsm")
                obs_fields = []
                if isinstance(obs, h5py.Group):
                    obs_fields = [
                        DatasetField(
                            name=name,
                            kind="categorical" if _categories(node) else "column",
                            shape=_shape(node),
                            categories=_categories(node),
                        )
                        for name, node in obs.items()
                        if not name.startswith("_")
                    ][:250]
                obsm_fields = []
                if isinstance(obsm, h5py.Group):
                    obsm_fields = [
                        DatasetField(name=name, kind="embedding", shape=_shape(node))
                        for name, node in obsm.items()
                    ][:100]
                sample, annotation, spatial = recommend_fields(obs_fields, obsm_fields)
                n_obs = None
                if isinstance(obs, h5py.Group) and "_index" in obs:
                    n_obs = int(obs["_index"].shape[0])
            return DatasetInspection(
                uri=uri,
                generation=str(generation),
                size_bytes=size,
                checksum=blob.md5_hash or blob.crc32c or "",
                n_obs=n_obs,
                obs_fields=obs_fields,
                obsm_fields=obsm_fields,
                suggested_sample_key=sample,
                suggested_annotation_key=annotation,
                suggested_spatial_key=spatial,
            )
        except Exception as exc:
            return DatasetInspection(
                uri=uri,
                generation=str(blob.generation or ""),
                size_bytes=int(blob.size or 0),
                checksum=blob.md5_hash or blob.crc32c or "",
                inspection_error=f"{type(exc).__name__}: {exc}",
            )
