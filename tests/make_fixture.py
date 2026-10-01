"""Deterministic spatial fixture: three samples, five cell types, sparse expression."""

from pathlib import Path


def make_fixture(directory):
    import anndata as ad
    import numpy as np
    import pandas as pd
    from scipy.sparse import csr_matrix

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(7)
    data = ad.AnnData(
        X=csr_matrix((6000, 30), dtype=np.float32),
        obs=pd.DataFrame(
            {
                "sample_id": np.repeat(["A", "B", "C"], 2000),
                "annotated_cell_type": rng.choice(
                    ["Hepatocyte", "Kupffer", "T", "B", "Endothelial"], 6000
                ),
            },
            index=[f"cell{i}" for i in range(6000)],
        ),
    )
    data.obsm["spatial"] = rng.uniform(0, 1000, (6000, 2))
    data.write_h5ad(directory / "spatial.h5ad")
    return directory / "spatial.h5ad"


if __name__ == "__main__":
    import sys

    print(make_fixture(sys.argv[1] if len(sys.argv) > 1 else "fixture_data"))
