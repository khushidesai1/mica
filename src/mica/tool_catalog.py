"""Shared metadata for external tools; no runtime dependencies or credentials."""

ATOMX_TRANSFER_ID = "tool-khushidesai1-atomx-transfer"
ATOMX_TRANSFER_SOURCE = "https://github.com/khushidesai1/atomx-transfer"
ATOMX_TRANSFER_COMMIT = "5d864b6cd13f1873364aa70b9ac1939f4c656616"
ATOMX_TRANSFER_UNAVAILABLE = (
    "The pinned upstream source contains manual shell scripts only; Mica supplies "
    "a reviewed CLI implementation in its dedicated transfer image. "
    "Transfers are disabled until an administrator configures that image, SFTP host key, "
    "project destination, and scoped identity. A saved login is collected through a form, "
    "and every transfer needs confirmation. The analysis worker cannot perform SFTP transfers."
)

SPATIAL_ANALYSIS_ID = "tool-harjaisal-spatial-analysis-pipeline"
SPATIAL_ANALYSIS_SOURCE = "https://github.com/harjaisal/spatial_analysis_pipeline"
SPATIAL_ANALYSIS_COMMIT = "de96763933ab5952bc0ba304cb10edadfb1614a0"
SPATIAL_ANALYSIS_NOTE = (
    "Python package sap 0.1.0: CosMx/CODEX ingest, Cellpose segmentation, and Proseg refinement. "
    "Requires the dedicated spatial analysis image (cloudbuild/build-spatial-analysis.yaml), "
    "including the Proseg wheel and cached Cellpose models; upstream declares no dependencies. "
    "The Slack worker uses /opt/sap/bin/python after checking the runtime. "
    "Catalog registration alone does not install or deploy the tool."
)


def spatial_analysis_capability() -> dict[str, str]:
    """Describe a known tool without inferring what is installed in a remote worker."""
    return {
        "id": SPATIAL_ANALYSIS_ID,
        "name": "spatial_analysis_pipeline",
        "source": SPATIAL_ANALYSIS_SOURCE,
        "commit": SPATIAL_ANALYSIS_COMMIT,
        "package": "sap",
        "version": "0.1.0",
        "runtime_status": "worker_check_required",
        "python": "/opt/sap/bin/python",
        "guide": "spatial_analysis.md",
        "description": SPATIAL_ANALYSIS_NOTE,
    }


def transfer_capability() -> dict[str, str | bool]:
    """Report shipped capability, never an inferred deployment or transfer result."""
    return {
        "id": ATOMX_TRANSFER_ID,
        "name": "atomx-transfer",
        "source": ATOMX_TRANSFER_SOURCE,
        "commit": ATOMX_TRANSFER_COMMIT,
        "execution_backend": "transfer_job",
        "available": False,
        "reason": ATOMX_TRANSFER_UNAVAILABLE,
        "guide": (
            "For a requested transfer, infer the sample, destination alias and raw/additional "
            "folders from the current request, project documents, prior transfer requests or "
            "observed bucket layout. Prefer the latest correction. An affirmative reply refers "
            "to the paths just discussed; they need not be typed again. Call propose_transfer "
            "to prepare the preview, including when the user also asks to remember defaults. "
            "A guide save is not a prerequisite and analysis workers cannot start transfers. "
            "Pass parent destinations when known (raw_outputs/raw/ and "
            "raw_outputs/additional_files/ in that layout); a final folder ending in the "
            "destination sample name is normalized so the sample is appended once. "
            "Omit export_root to discover the sample on SFTP. Inventory verifies the source "
            "export/run and maps flat files, Morphology2D and the channel dictionary to raw; "
            "the other run files go to additional files. The private approval shows the exact "
            "source, final folders, file count and size. Only that approval starts copying; "
            "context and a natural-language request never approve the write. "
            "Previous requests are suggestions, not proof of completion or current source state."
        ),
    }
