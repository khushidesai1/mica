"""Real scientific acceptance tests; run in Dockerfile.development-test's pinned runtime."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
anndata = pytest.importorskip("anndata")
pd = pytest.importorskip("pandas")
pytest.importorskip("louvain")
pytest.importorskip("sklearn")


def module(name):
    path = Path(__file__).parent / "fixtures/neighborhood" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


composition = module("composition")
assessment = module("assessment")


def test_hand_computed_compositions_and_partial_chunks():
    coordinates = np.array([[0, 0], [1, 0], [3, 0], [10, 0], [11, 0], [14, 0]])
    samples = np.array(["a"] * 3 + ["b"] * 3)
    categories = np.array([0, 1, 1, 0, 0, 1])
    observed, neighbors = composition.compositions(coordinates, samples, categories, 2, 2, 2)
    np.testing.assert_allclose(
        observed, [[0, 1], [0.5, 0.5], [0.5, 0.5], [0.5, 0.5], [0.5, 0.5], [1, 0]]
    )
    whole, _ = composition.compositions(coordinates, samples, categories, 2, 2, 100)
    np.testing.assert_array_equal(observed, whole)
    assert not np.any(samples[neighbors] != samples[:, None])
    with pytest.raises(ValueError, match="insufficient"):
        composition.compositions(coordinates, samples, categories, 2, 3, 2)


def test_four_sample_real_anndata_louvain_and_repeatability(tmp_path):
    rng = np.random.default_rng(17)
    coordinates, samples, types = [], [], []
    for i, size in enumerate([41, 47, 53, 59]):
        labels = rng.integers(0, 2 if i == 0 else 3, size=size)
        coords = rng.normal(size=(size, 2)) + labels[:, None] * 8
        coordinates.extend(coords)
        samples.extend([f"sample-{i}"] * size)
        types.extend(["a", "b", "c"][label] for label in labels)
    data = anndata.AnnData(
        X=np.zeros((len(samples), 1)),
        obs=pd.DataFrame(
            {
                "sample_id": samples,
                "annotated_cell_type": pd.Categorical(types, categories=["a", "b", "c"]),
            },
            index=[f"cell-{i}" for i in range(len(samples))],
        ),
    )
    data.obsm["spatial"] = np.asarray(coordinates)
    path = tmp_path / "fixture.h5ad"
    data.write_h5ad(path)
    # Reopen the actual file on each repetition, including the final partial chunks.
    runs = []
    for chunk_size in [7, 23]:
        reopened = anndata.read_h5ad(path)
        matrix, neighbors = composition.compositions(
            reopened.obsm["spatial"],
            reopened.obs["sample_id"].to_numpy(),
            reopened.obs["annotated_cell_type"].cat.codes.to_numpy(),
            3,
            30,
            chunk_size,
        )
        assert matrix.shape == (200, 3)
        assert neighbors.shape == (200, 30)
        best, scores = assessment.assess(matrix, 30, 0)
        assert len(scores) == 20
        assert [item["resolution"] for item in scores] == [i / 10 for i in range(1, 21)]
        assert best["score"] == max(item["score"] for item in scores if item["score"] is not None)
        runs.append((matrix, best))
    np.testing.assert_array_equal(runs[0][0], runs[1][0])
    assert runs[0][1] == runs[1][1]
    assert assessment.score_labels(runs[0][0], np.zeros(200)) is None
    assert assessment.score_labels(runs[0][0], np.arange(200)) is None


def test_invalid_alignment_and_category_bounds():
    with pytest.raises(ValueError, match="alignment"):
        composition.compositions(
            np.zeros((4, 2)), np.array(["a"] * 3), np.array([0, 1, 0]), 2, 1, 2
        )
    with pytest.raises(ValueError, match="bounds"):
        composition.compositions(
            np.zeros((3, 2)), np.array(["a"] * 3), np.array([0, -1, 0]), 2, 1, 2
        )
