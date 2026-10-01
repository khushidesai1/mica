"""Reference candidate for the four-sample development acceptance fixture."""

from __future__ import annotations

import numpy as np
from sklearn.neighbors import NearestNeighbors


def compositions(coordinates, samples, categories, n_categories, k, chunk_size):
    coordinates = np.asarray(coordinates)
    samples = np.asarray(samples)
    categories = np.asarray(categories)
    if coordinates.shape != (len(samples), 2) or categories.shape != (len(samples),):
        raise ValueError("coordinate, category, and sample alignment mismatch")
    if (
        not np.isfinite(coordinates).all()
        or np.any(categories < 0)
        or np.any(categories >= n_categories)
    ):
        raise ValueError("invalid coordinates or category bounds")
    result = np.zeros((len(samples), n_categories), dtype=float)
    neighbors = np.full((len(samples), k), -1, dtype=int)
    for sample in np.unique(samples):
        indices = np.flatnonzero(samples == sample)
        if len(indices) <= k:
            raise ValueError("sample has insufficient neighbors; k is not automatically reduced")
        model = NearestNeighbors(n_neighbors=k + 1, algorithm="brute").fit(coordinates[indices])
        for start in range(0, len(indices), chunk_size):
            local_rows = np.arange(start, min(start + chunk_size, len(indices)))
            raw = model.kneighbors(coordinates[indices[local_rows]], return_distance=False)
            selected = np.array(
                [row[row != focal][:k] for focal, row in zip(local_rows, raw, strict=True)]
            )
            global_rows = indices[local_rows]
            neighbors[global_rows] = indices[selected]
            for global_row, neighbor_ids in zip(global_rows, neighbors[global_rows], strict=True):
                result[global_row] = (
                    np.bincount(categories[neighbor_ids], minlength=n_categories) / k
                )
    if not np.allclose(result.sum(axis=1), 1):
        raise ValueError("composition normalization invariant failed")
    if np.any(samples[neighbors] != samples[:, None]):
        raise ValueError("cross-sample spatial edge")
    if np.any(neighbors == np.arange(len(samples))[:, None]):
        raise ValueError("focal cell included")
    return result, neighbors
