from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import mica_storage_migration as migration

SOURCE = migration.SOURCE
DEST = "mica-staging-artifacts-20261021"
TASK = "task_" + "1" * 32
SNAPSHOT = f"paused/{TASK}/1790275393-503c88c4d873.json"


def record(name=SNAPSHOT, generation="10", **changes):
    properties = {
        "name": name, "size": "12", "generation": generation, "metageneration": "1",
        "crc32c": "abcd1234", "md5Hash": "content-digest", "contentType": "application/json",
        "metadata": {"sha256": "test-content-digest"}, "storageClass": "STANDARD",
        "timeCreated": "2026-09-24T00:00:00Z", **changes,
    }
    return migration.object_record(properties)


def save_inventory(path: Path, bucket: str, rows, **header):
    migration.write_evidence(path, {
        "type": "inventory", "project": migration.PROJECT, "project_number": "123",
        "bucket": bucket, **header,
    }, rows)
    return path


def verify(tmp_path, before=None, after=None, destination=None):
    before = [record()] if before is None else before
    after = before if after is None else after
    destination = [record(generation="20")] if destination is None else destination
    output = tmp_path / "mapping.jsonl"
    result = migration.verify(
        SOURCE, DEST,
        save_inventory(tmp_path / "before.jsonl", SOURCE, before),
        save_inventory(tmp_path / "after.jsonl", SOURCE, after),
        save_inventory(tmp_path / "destination.jsonl", DEST, destination),
        output,
    )
    return result, output


def test_copy_maps_new_generations_without_changing_content_identity(tmp_path):
    result, output = verify(tmp_path)
    assert result == {
        "verified": True, "matched": 1, "issues": {}, "source_objects": 1,
        "destination_objects": 1,
    }
    header, rows = migration.read_evidence(output)
    assert header["verified"] is True
    assert rows[0]["source_generation"] == "10"
    assert rows[0]["destination_generation"] == "20"
    assert output.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("changes", [
    {"generation": "11"}, {"metageneration": "2"},
    {"metadata": {"sha256": "changed"}}, {"size": "14"},
])
def test_source_must_remain_unchanged_during_final_sync(tmp_path, changes):
    result, _ = verify(tmp_path, after=[record(**changes)])
    assert result["verified"] is False
    assert result["issues"] == {"source_changed": 1}


@pytest.mark.parametrize(("destination", "issue"), [
    ([], "destination_missing"),
    ([record(size="13")], "content_mismatch"),
    ([record(crc32c="other")], "content_mismatch"),
    ([record(md5Hash="other")], "content_mismatch"),
    ([record(contentType="text/plain")], "metadata_mismatch"),
    ([record(metadata={"sha256": "modified"})], "metadata_mismatch"),
    ([record(kmsKeyName="unexpected-key")], "metadata_mismatch"),
])
def test_copy_rejects_missing_or_changed_objects(tmp_path, destination, issue):
    result, _ = verify(tmp_path, destination=destination)
    assert not result["verified"]
    assert result["issues"] == {issue: 1}


def test_source_deletions_and_destination_extras_need_explicit_reconciliation(tmp_path):
    result, _ = verify(tmp_path, after=[], destination=[record(), record(name="extra")])
    assert result["issues"] == {"source_changed": 1, "destination_extra": 1}
    assert not result["verified"]


def test_sts_generation_marker_is_not_a_destination_generation(tmp_path):
    result, output = verify(tmp_path, destination=[record(
        generation="99", metadata={
            "sha256": "test-content-digest", "x-goog-reserved-source-generation": "10",
        },
    )])
    assert result["verified"]
    assert migration.read_evidence(output)[1][0]["destination_generation"] == "99"


def test_custom_metadata_not_persisted_in_evidence(tmp_path):
    value = record(metadata={"sensitive-label": "never-expose-this"})
    assert "never-expose-this" not in json.dumps(value)
    assert "sensitive-label" not in json.dumps(value)


@pytest.mark.parametrize("kind", ["missing_footer", "partial", "tampered", "trailing"])
def test_truncated_or_incomplete_inventory_cannot_verify(tmp_path, kind):
    path = save_inventory(tmp_path / "inventory.jsonl", SOURCE, [record()])
    lines = path.read_text().splitlines()
    if kind == "missing_footer":
        lines.pop()
    elif kind == "partial":
        footer = json.loads(lines[-1])
        footer["complete"] = False
        lines[-1] = json.dumps(footer)
    elif kind == "tampered":
        row = json.loads(lines[1])
        row["size"] = 99
        lines[1] = json.dumps(row)
    else:
        lines.append(lines[1])
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError):
        migration.load_inventory(path, SOURCE)


def test_duplicate_objects_and_wrong_scope_rejected(tmp_path):
    path = save_inventory(tmp_path / "duplicate.jsonl", SOURCE, [record(), record()])
    with pytest.raises(ValueError, match="Duplicate"):
        migration.load_inventory(path, SOURCE)
    path = save_inventory(tmp_path / "other.jsonl", "scientific-data", [record()])
    with pytest.raises(ValueError, match="scope"):
        migration.load_inventory(path, SOURCE)
    with pytest.raises(ValueError):
        migration.buckets(SOURCE, "scientific-data")
    with pytest.raises(ValueError):
        migration.buckets("nut_carcinoma", DEST)


def test_inventory_bounds_and_owner_are_enforced_without_object_reads(tmp_path):
    class Client:
        def get_bucket(self, name):
            return SimpleNamespace(_properties={"projectNumber": "123"})

        def list_blobs(self, name, **kwargs):
            assert "metadata" in kwargs["fields"]
            for index in range(2):
                yield SimpleNamespace(_properties={
                    "name": str(index), "size": "1", "generation": "1",
                    "metageneration": "1", "crc32c": "checksum",
                })

    with pytest.raises(ValueError, match="owned"):
        migration.inventory(Client(), SOURCE, "999", tmp_path / "wrong.jsonl", 2)
    assert not (tmp_path / "wrong.jsonl").exists()
    partial = tmp_path / "partial.jsonl"
    with pytest.raises(ValueError, match="bound"):
        migration.inventory(Client(), SOURCE, "123", partial, 1)
    with pytest.raises(ValueError, match="Incomplete"):
        migration.read_evidence(partial)
    complete = tmp_path / "complete.jsonl"
    assert migration.inventory(Client(), SOURCE, "123", complete, 2)["complete"]
    assert len(migration.load_inventory(complete, SOURCE)) == 2


def test_evidence_never_overwrites_existing_file_or_follows_symlink(tmp_path):
    existing = tmp_path / "existing"
    existing.write_text("keep")
    symlink = tmp_path / "symlink"
    symlink.symlink_to(existing)
    for path in [existing, symlink]:
        with pytest.raises(FileExistsError):
            migration.write_evidence(path, {}, [])
    assert existing.read_text() == "keep"


def snapshot_export(path, **changes):
    path.write_text(json.dumps([{
        "id": TASK, "paused_state_uri": f"gs://{SOURCE}/{SNAPSHOT}#10",
        "update_time": "2026-10-21T00:00:00Z", "status": "NEEDS_APPROVAL", **changes,
    }]))
    return path


def test_snapshot_proposals_use_new_generation_and_concurrency_preconditions(tmp_path):
    _, mapping = verify(tmp_path)
    output = tmp_path / "proposals.jsonl"
    result = migration.snapshot_proposals(
        mapping, snapshot_export(tmp_path / "snapshots.json"), output
    )
    assert result == {"proposals": 1, "dry_run": True, "requires_write_freeze": True}
    header, rows = migration.read_evidence(output)
    assert header["dry_run"] and header["requires_write_freeze"]
    assert rows[0]["updates"] == {"paused_state_uri": f"gs://{DEST}/{SNAPSHOT}#20"}
    assert rows[0]["precondition"]["paused_state_uri"] == f"gs://{SOURCE}/{SNAPSHOT}#10"
    assert rows[0]["precondition"]["update_time"] == "2026-10-21T00:00:00Z"


@pytest.mark.parametrize("changes", [
    {"paused_state_uri": f"gs://{SOURCE}/{SNAPSHOT}#999"},
    {"paused_state_uri": f"gs://nut_carcinoma/{SNAPSHOT}#10"},
    {"paused_state_uri": f"gs://{SOURCE}/tasks/{TASK}/bundle.json#10"},
    {"update_time": ""},
    {"id": "task_" + "2" * 32},
    {"historical_attestation": "do not rewrite"},
])
def test_snapshot_proposals_reject_unsafe_or_unmapped_reference(tmp_path, changes):
    _, mapping = verify(tmp_path)
    with pytest.raises(ValueError):
        migration.snapshot_proposals(
            mapping, snapshot_export(tmp_path / "snapshots.json", **changes),
            tmp_path / "proposals.jsonl",
        )


def test_snapshot_proposals_reject_failed_verification(tmp_path):
    _, mapping = verify(tmp_path, destination=[])
    with pytest.raises(ValueError, match="verified copy"):
        migration.snapshot_proposals(
            mapping, snapshot_export(tmp_path / "snapshots.json"), tmp_path / "proposals.jsonl"
        )


def test_verification_rejects_project_number_mismatch(tmp_path):
    before = save_inventory(tmp_path / "before.jsonl", SOURCE, [record()])
    after = save_inventory(tmp_path / "after.jsonl", SOURCE, [record()])
    destination = save_inventory(
        tmp_path / "destination.jsonl", DEST, [record(generation="20")], project_number="999"
    )
    with pytest.raises(ValueError, match="same verified project"):
        migration.verify(SOURCE, DEST, before, after, destination, tmp_path / "mapping.jsonl")
    assert not (tmp_path / "mapping.jsonl").exists()


def test_snapshot_proposals_do_not_allow_other_objects_under_task_prefix(tmp_path):
    other = f"paused/{TASK}/unrelated.json"
    _, mapping = verify(
        tmp_path, before=[record(name=other)], destination=[record(name=other, generation="20")]
    )
    with pytest.raises(ValueError, match="exact managed task scope"):
        migration.snapshot_proposals(
            mapping, snapshot_export(
                tmp_path / "snapshots.json", paused_state_uri=f"gs://{SOURCE}/{other}#10"
            ), tmp_path / "proposals.jsonl",
        )
