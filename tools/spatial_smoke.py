"""Check SAP's installed API and cached CPU model without running an analysis."""

import inspect
import json
import os
from importlib.metadata import version
from pathlib import Path

EXPECTED_PARAMETERS = {
    "ingest.cosmx": ["input_path", "output_path"],
    "ingest.codex": ["input_path", "output_path"],
    "segmentation.cellpose": [
        "store_path",
        "nuclei_channels",
        "cyto_channels",
        "diameter",
        "model_type",
        "gpu",
        "batch_size",
        "chunk_overlap",
        "temp_dir",
    ],
    "segmentation.proseg": ["store_path", "temp_dir", "um_per_px"],
}


def check_api(sap) -> None:
    for name, parameters in EXPECTED_PARAMETERS.items():
        module, function = name.split(".")
        actual = list(inspect.signature(getattr(getattr(sap, module), function)).parameters)
        if actual != parameters:
            raise RuntimeError(f"Unexpected sap.{name} signature: {actual}")


def main() -> None:
    manifest = json.loads(Path("/opt/sap/manifest.json").read_text())
    if version("sap") != manifest["version"] or version("proseg-py") != "3.1.1":
        raise RuntimeError("Unexpected SAP or Proseg package version")
    model_dir = Path("/opt/sap/models")
    for name in ("cyto3", "size_cyto3.npy"):
        if not (model_dir / name).is_file():
            raise RuntimeError(f"Cached Cellpose model missing: {name}")
    os.environ["CELLPOSE_LOCAL_MODELS_PATH"] = str(model_dir)
    import anndata
    import sap
    import torch
    from cellpose import models
    from dask.distributed import Client, LocalCluster
    from proseg import run_proseg

    check_api(sap)
    if not callable(run_proseg) or not hasattr(anndata.settings, "zarr_write_format"):
        raise RuntimeError("The Proseg/AnnData runtime is incompatible")
    torch.set_num_threads(1)
    models.Cellpose(model_type="cyto3", gpu=False)
    # The Slack sandbox forbids TCP listeners; exercise the supported in-process scheduler.
    with (
        LocalCluster(
            processes=False,
            protocol="inproc://",
            dashboard_address=None,
            n_workers=1,
            threads_per_worker=1,
        ) as cluster,
        Client(cluster) as client,
    ):
        if client.submit(sum, [1, 2]).result() != 3:
            raise RuntimeError("Dask runtime check failed")
    print(
        json.dumps(
            {
                "tool": "spatial_analysis_pipeline",
                "package": "sap",
                "version": version("sap"),
                "commit": manifest["commit"],
                "runtime_check": "passed",
                "device": "cpu",
                "analysis_run": False,
            }
        )
    )


if __name__ == "__main__":
    main()
