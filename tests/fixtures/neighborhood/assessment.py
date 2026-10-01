"""Louvain/scoring acceptance path with an explicit fixture tie policy."""

from __future__ import annotations

import igraph
import louvain
import numpy as np
from sklearn.metrics import silhouette_score
from sklearn.neighbors import NearestNeighbors


def score_labels(values, labels):
    count = len(np.unique(labels))
    if not 2 <= count <= len(values) - 1:
        return None
    return float(silhouette_score(values, labels, metric="euclidean"))


def assess(values, k, seed):
    if len(values) <= k:
        raise ValueError("insufficient composition graph observations")
    raw = (
        NearestNeighbors(n_neighbors=k + 1, algorithm="brute")
        .fit(values)
        .kneighbors(values, return_distance=False)
    )
    edges = sorted(
        {tuple(sorted((i, int(j)))) for i, row in enumerate(raw) for j in row[row != i][:k]}
    )
    graph = igraph.Graph(n=len(values), edges=edges, directed=False)
    results = []
    for step in range(1, 21):
        resolution = step / 10
        partition = louvain.find_partition(
            graph,
            louvain.RBConfigurationVertexPartition,
            resolution_parameter=resolution,
            seed=seed,
        )
        labels = np.asarray(partition.membership)
        results.append(
            {
                "resolution": resolution,
                "score": score_labels(values, labels),
                "labels": labels.tolist(),
            }
        )
    valid = [item for item in results if item["score"] is not None]
    if not valid:
        raise ValueError("no resolution has a defined Silhouette score")
    # The fixture explicitly authorizes the lowest resolution for exact score ties.
    best = max(valid, key=lambda item: (item["score"], -item["resolution"]))
    return best, results
