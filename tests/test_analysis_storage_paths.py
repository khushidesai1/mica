"""Existing logical run keys keep resolving after physical project-folder migration."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import NotFound

from mica.agents.analysis.storage import STORAGE_PATHS_KEY, GCSStorage, storage_paths

OLD = "projects/pista-cosmx-6k-crc/"
NEW = "projects/crc-cosmx/"


@pytest.fixture
def store():
    storage = object.__new__(GCSStorage)
    storage.bucket = MagicMock()
    index, target = MagicMock(), MagicMock()
    index.download_as_bytes.return_value = b'{"pista-cosmx-6k-crc":"crc-cosmx"}'
    target.generation = 42
    target.download_as_bytes.return_value = b"saved result"
    storage.bucket.blob.side_effect = lambda key: index if key == STORAGE_PATHS_KEY else target
    return storage, index, target


def test_existing_keys_read_and_write_the_new_folder_with_cas(store, tmp_path):
    storage, _, target = store
    logical = OLD + "runs/1/outputs/result.txt"
    path = tmp_path / "result.txt"
    assert storage.get(logical) == (b"saved result", "42")
    storage.put_if(logical, b"updated", "42")
    target.upload_from_string.assert_called_once_with(b"updated", if_generation_match=42)
    storage.delete_if(logical, "42")
    target.delete.assert_called_once_with(if_generation_match=42)
    storage.put(logical, b"updated")
    storage.upload(logical, path)
    storage.upload_new(logical, path)
    storage.open_read(logical)
    storage.open_write(logical)
    storage.download(logical, path)
    assert storage.read_prefix(logical, 5) == b"saved result"
    destinations = {call.args[0] for call in storage.bucket.blob.call_args_list}
    assert destinations == {STORAGE_PATHS_KEY, NEW + "runs/1/outputs/result.txt"}


def test_listings_preserve_logical_keys_for_checkpoint_consumers(store):
    storage, _, _ = store
    storage.bucket.list_blobs.return_value = [SimpleNamespace(name=NEW + "runs/1/meta.json")]
    assert storage.keys(OLD + "runs/") == [OLD + "runs/1/meta.json"]
    storage.bucket.list_blobs.assert_called_once_with(prefix=NEW + "runs/")


def test_cutover_is_visible_to_an_existing_storage_instance(store):
    storage, index, _ = store
    index.download_as_bytes.side_effect = NotFound("No index yet")
    assert storage.physical_key(OLD + "project.json") == OLD + "project.json"
    index.download_as_bytes.side_effect = None
    assert storage.physical_key(OLD + "project.json") == NEW + "project.json"
    assert storage.physical_key(OLD.rstrip("/")) == NEW.rstrip("/")


def test_private_internal_and_unrelated_paths_are_not_redirected(store):
    storage, index, _ = store
    for key in [
        "tasks/run/file",
        "projects/_threads/t/checkpoint.json",
        "projects/_users/u/runs/p/f",
    ]:
        assert storage.physical_key(key) == key
    index.download_as_bytes.assert_not_called()
    assert storage.physical_key("projects/other/PROJECT.md") == "projects/other/PROJECT.md"
    assert (
        storage.physical_key("projects/pista-cosmx-6k-crc-extra/a")
        == "projects/pista-cosmx-6k-crc-extra/a"
    )


@pytest.mark.parametrize(
    "raw",
    [
        b"[]",
        b'{"abc":"../escape"}',
        b'{"abc":"_registry"}',
        b'{"abc":"def","ghi":"def"}',
        b'{"abc":"def","def":"ghi"}',
        b'{"abc":2}',
    ],
)
def test_invalid_or_ambiguous_mappings_fail_closed(raw):
    with pytest.raises(ValueError):
        storage_paths(raw)
