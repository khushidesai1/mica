from mica.models import DatasetField
from mica.services.datasets import recommend_fields


def test_recommend_fields_uses_h5ad_names_and_cardinality() -> None:
    obs = [
        DatasetField(
            name="sample_id",
            kind="categorical",
            categories=["S1", "S2", "S3", "S4"],
        ),
        DatasetField(name="cell_type_L2", kind="categorical", categories=["T", "B"]),
    ]
    obsm = [DatasetField(name="spatial", kind="embedding", shape=[100, 2])]

    assert recommend_fields(obs, obsm) == ("sample_id", "cell_type_L2", "spatial")
