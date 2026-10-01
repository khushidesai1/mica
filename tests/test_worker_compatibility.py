from __future__ import annotations

import copy
import runpy
from pathlib import Path

import pytest
from pydantic import ValidationError


@pytest.fixture
def compatibility():
    return runpy.run_path(
        str(Path(__file__).parents[1] / "scripts/check_worker_compatibility.py")
    )


def test_deployment_contract_covers_method_defaults(compatibility):
    expected = compatibility["contract"]()
    settings = expected["bundle"]["parameter_manifest"]
    assert settings[0]["decision_scope"] == "implementation"
    assert settings[1]["decision_scope"] == "method_default"
    assert "decision_scope" not in settings[2]  # Preserve legacy authority serialization.
    compatibility["check"](expected)


def test_deployment_rejects_different_worker_schema(compatibility):
    expected = copy.deepcopy(compatibility["contract"]())
    del expected["schema"]["$defs"]["ParameterSpec"]["properties"]["decision_scope"]
    with pytest.raises(ValueError, match="schemas differ"):
        compatibility["check"](expected)


def test_deployment_rejects_unrecognized_api_field(compatibility):
    expected = compatibility["contract"]()
    expected["bundle"]["parameter_manifest"][0]["future_field"] = "value"
    with pytest.raises(ValidationError, match="future_field"):
        compatibility["check"](expected)


def test_deployment_rejects_different_trusted_runtime(compatibility):
    expected = compatibility["contract"]()
    expected["runtime_contract_hash"] = "different-worker-helpers"
    with pytest.raises(ValueError, match="trusted helper and validation contracts differ"):
        compatibility["check"](expected)
