import pytest

from pistachio.agents.analysis.data_links import data_uri, init_args


@pytest.mark.parametrize(
    "link",
    [
        "gs://lab-data/folder/",
        "<gs://lab-data/folder/|folder>",
        "https://storage.googleapis.com/lab-data/folder/",
        "https://storage.cloud.google.com/lab-data/folder/",
        "https://console.cloud.google.com/storage/browser/lab-data/folder/?project=example",
    ],
)
def test_google_storage_links(link):
    assert data_uri(link) == "gs://lab-data/folder/"
    assert init_args(f"<@BOT> init liver {link} --force") == (
        "liver",
        "gs://lab-data/folder/",
        True,
    )


@pytest.mark.parametrize(
    "link",
    [
        "https://example.com/lab-data/",
        "gs://lab-data/../outside",
        "https://storage.googleapis.com/lab-data/%2e%2e/outside",
        "gs://lab-data/a%2fb",
        "gs://lab-data/path?token=secret",
    ],
)
def test_unsupported_and_traversal_links_rejected(link):
    with pytest.raises(ValueError):
        data_uri(link)
