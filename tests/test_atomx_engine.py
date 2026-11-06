"""Offline transfer/repair tests with real checksum and generation comparisons."""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import google_crc32c
import jsonschema
import pytest

from tools.atomx_transfer import cli, core, worker
from tools.atomx_transfer.adapters import GCS

ROOT = "/MyExport"
RUN = "20261019_103117_S1"
RAW = "raw_outputs/raw/TMA37/"
ADDITIONAL = "raw_outputs/additional_files/TMA37/"
DONE = "transfer_status/TMA37.done.json"
LEASE = "transfer_status/TMA37.in_progress.json"
FAILED = "transfer_status/TMA37.failed.json"
SCHEMA = json.loads(Path("tools/atomx_transfer/schema/marker-v1.json").read_text())


class MemoryBucket:
    def __init__(self):
        self.data: dict[str, bytes] = {}
        self.generations: dict[str, str] = {}
        self.counter = 0
        self.fail_copy: str | None = None

    def put(self, name: str, data: bytes) -> None:
        self.counter += 1
        self.data[name] = data
        self.generations[name] = str(self.counter)

    def read(self, name: str) -> tuple[dict[str, Any], str] | None:
        if name not in self.data:
            return None
        return json.loads(self.data[name]), self.generations[name]

    def write(self, name: str, body: dict[str, Any], generation: str | None) -> str:
        if self.generations.get(name) != generation:
            raise core.Busy("Generation mismatch")
        self.put(name, json.dumps(body).encode())
        return self.generations[name]

    def delete(self, name: str, generation: str) -> None:
        if self.generations.get(name) != generation:
            raise core.Busy("Generation mismatch")
        del self.data[name]
        del self.generations[name]

    def stat(self, name: str) -> core.Object | None:
        if name not in self.data:
            return None
        data = self.data[name]
        checksum = base64.b64encode(google_crc32c.Checksum(data).digest()).decode()
        return core.Object(name, len(data), checksum)

    def list(self, prefix: str) -> list[core.Object]:
        return [self.stat(name) for name in sorted(self.data) if name.startswith(prefix)]

    def folders(self, prefix: str) -> list[str]:
        return sorted(
            {
                prefix + name[len(prefix) :].split("/")[0] + "/"
                for name in self.data
                if name.startswith(prefix) and "/" in name[len(prefix) :]
            }
        )

    def copy(self, name: str, chunks: Iterator[bytes]) -> None:
        if name == self.fail_copy:
            raise OSError("Synthetic upload failure")
        if name in self.data:
            raise core.Busy("Create-only conflict")
        self.put(name, b"".join(chunks))


class MemoryRemote:
    def __init__(self):
        decoded = f"{ROOT}/DecodedFiles/tma37/{RUN}"
        self.data = {
            f"{decoded}/CellStatsDir/Morphology2D/image.tif": b"pixels",
            f"{decoded}/RunSummary/Morphology_ChannelID_Dictionary.txt": b"map",
            f"{decoded}/metrics.csv": b"counts",
            f"{ROOT}/flatFiles/tma37/cells.csv": b"cells",
        }

    def dirs(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        under = [name[len(prefix) :] for name in self.data if name.startswith(prefix)]
        if not under and path != "/":
            raise FileNotFoundError(path)  # Like SFTP, a missing folder cannot be listed.
        return sorted({rel.split("/")[0] for rel in under if "/" in rel})

    def files(self, path: str) -> Iterator[tuple[str, int]]:
        prefix = path + "/"
        found = [
            (name[len(prefix) :], len(data))
            for name, data in sorted(self.data.items())
            if name.startswith(prefix)
        ]
        if not found:
            raise FileNotFoundError(path)
        yield from found

    def size(self, path: str) -> int:
        return len(self.data[path])

    def open(self, path: str) -> io.BytesIO:
        return io.BytesIO(self.data[path])


@pytest.fixture
def setup():
    remote, bucket = MemoryRemote(), MemoryBucket()
    run, files = core.plan(remote, ROOT, "tma37", "TMA37")
    request = {
        "sample": "tma37",
        "as_sample": "TMA37",
        "export_root": ROOT,
        "tool_version": "synthetic",
        "requested_by": "manual:test",
        "login_saved_by": "test",
        "run_id": "synthetic-run",
        "plan_fingerprint": core.fingerprint(run, files),
    }
    return remote, bucket, request


def backfill(bucket, **extra):
    return core.backfill(
        bucket,
        {
            "dry_run": False,
            "tool_version": "synthetic",
            "requested_by": "manual:test",
            "run_id": "backfill-run",
            **extra,
        },
    )


def run_cli(monkeypatch, tmp_path, bucket, command, *args, destinations=None):
    config = tmp_path / "config.yaml"
    config.write_text(
        json.dumps(
            {
                "destinations": destinations
                or {
                    "bucket": "scratch",
                    "raw_prefix": "raw_outputs/raw/",
                    "additional_prefix": "raw_outputs/additional_files/",
                    "status_prefix": "transfer_status/",
                }
            }
        )
    )  # No SFTP configuration or login needed.
    monkeypatch.setattr(worker, "GCS", lambda _: bucket)

    def no_sftp(*_):
        pytest.fail("Bucket-only operation tried to open SFTP or prompt for login")

    monkeypatch.setattr(cli, "SFTP", no_sftp)
    monkeypatch.setattr("builtins.input", no_sftp)
    monkeypatch.setattr(cli.getpass, "getpass", no_sftp)
    monkeypatch.setattr(
        "sys.argv",
        [
            "atomx-transfer",
            command,
            "--config",
            str(config),
            *args,
        ],
    )
    return cli.main()


def test_transfer_writes_matching_markers_and_verify_passes(setup, monkeypatch, tmp_path, capsys):
    remote, bucket, request = setup
    done = core.Transfer(remote, bucket, request).run()
    jsonschema.validate(done, SCHEMA)
    assert bucket.read(DONE)[0] == done
    assert LEASE not in bucket.data
    assert FAILED not in bucket.data
    for scope, prefix, count in (("raw", RAW, 3), ("additional_files", ADDITIONAL, 1)):
        text = bucket.data[prefix + "done.txt"].decode()
        fields = dict(line.split(": ", 1) for line in text.split("\n\n")[0].splitlines()[1:])
        assert fields["scope"] == scope
        assert fields["scope_files"] == str(count)
        assert fields["sample_files"] == str(done["n_files"])
        assert fields["sample_bytes"] == str(done["total_bytes"])
        assert fields["finished"] == done["finished"]
        assert fields["record"] == DONE
        assert fields["verified"] == "crc32c"
    assert run_cli(monkeypatch, tmp_path, bucket, "verify", "--sample", "TMA37") == 0
    assert "Verified: crc32c" in capsys.readouterr().out


@pytest.mark.parametrize("change", ["extra", "missing", "same_size_edit"])
def test_verify_still_detects_data_changes(setup, monkeypatch, tmp_path, change):
    remote, bucket, request = setup
    core.Transfer(remote, bucket, request).run()
    if change == "extra":
        bucket.put(RAW + "unexpected", b"extra")
    elif change == "missing":
        bucket.delete(RAW + "cells.csv", bucket.generations[RAW + "cells.csv"])
    else:
        bucket.put(RAW + "cells.csv", b"edits")
    assert run_cli(monkeypatch, tmp_path, bucket, "verify", "--sample", "TMA37") == 1


@pytest.mark.parametrize(
    "rel",
    [
        "flatFiles/tma37/done.txt",
        "flatFiles/tma37/subfolder/done.txt",
        f"DecodedFiles/tma37/{RUN}/done.txt",
    ],
)
def test_plan_rejects_marker_collisions(setup, rel):
    remote, _, _ = setup
    remote.data[f"{ROOT}/{rel}"] = b"source"
    with pytest.raises(ValueError, match="reserved done.txt"):
        core.plan(remote, ROOT, "tma37", "TMA37")


@pytest.mark.parametrize("prefix", [RAW, ADDITIONAL])
def test_backfill_repairs_text_failure_as_verified(setup, prefix):
    remote, bucket, request = setup
    bucket.fail_copy = prefix + "done.txt"
    with pytest.raises(RuntimeError, match="markers"):
        core.Transfer(remote, bucket, request).run()
    original = bucket.data[DONE], bucket.generations[DONE]
    bucket.fail_copy = None
    assert backfill(bucket)["repaired"] == ["TMA37"]
    assert (bucket.data[DONE], bucket.generations[DONE]) == original
    assert "verified: crc32c\n" in bucket.data[prefix + "done.txt"].decode()


def test_stale_completion_lease_can_be_repaired(setup):
    remote, bucket, request = setup
    done = core.Transfer(remote, bucket, request).run()
    bucket.delete(RAW + "done.txt", bucket.generations[RAW + "done.txt"])
    bucket.write(LEASE, {**done, "heartbeat": "2000-01-01T00:00:00+00:00"}, None)
    assert backfill(bucket)["repaired"] == ["TMA37"]
    assert LEASE not in bucket.data


def test_backfill_raw_only_snapshot_is_idempotent_and_stays_listing_only():
    bucket = MemoryBucket()
    bucket.put(RAW + "cells.csv", b"abc")
    result = backfill(bucket)
    assert result["marked"] == ["TMA37"]
    body = bucket.read(DONE)[0]
    jsonschema.validate(body, SCHEMA)
    assert body["provenance"] == "backfill"
    assert "manifest" not in body and "plan_fingerprint" not in body
    assert body["n_files"] == 1 and body["total_bytes"] == 3
    text = bucket.data[RAW + "done.txt"].decode()
    assert "additional_files: absent\n" in text and "source_checked: no\n" in text
    snapshot = deepcopy((bucket.data, bucket.generations, bucket.counter))
    assert backfill(bucket)["skipped"] == [{"sample": "TMA37", "reason": "complete"}]
    assert (bucket.data, bucket.generations, bucket.counter) == snapshot
    bucket.delete(RAW + "done.txt", bucket.generations[RAW + "done.txt"])
    bucket.put(RAW + "late.csv", b"late")
    bucket.put(ADDITIONAL + "later.csv", b"later")
    assert backfill(bucket)["repaired"] == ["TMA37"]
    assert bucket.data[RAW + "done.txt"].decode() == text
    assert ADDITIONAL + "done.txt" not in bucket.data


def test_backfill_three_samples_and_selected_sample():
    bucket = MemoryBucket()
    for name, scopes in (("A", core.SCOPES), ("B", core.SCOPES), ("C", ("raw",))):
        for scope in scopes:
            bucket.put(f"raw_outputs/{scope}/{name}/data.csv", b"tiny")
    assert backfill(bucket, sample="A")["marked"] == ["A"]
    assert "transfer_status/B.done.json" not in bucket.data
    result = backfill(bucket)
    assert result["marked"] == ["B", "C"]
    assert len([key for key in bucket.data if key.endswith("/done.txt")]) == 5
    assert len([key for key in bucket.data if key.endswith(".done.json")]) == 3


@pytest.mark.parametrize("args", [(), ("--dry-run",)])
def test_cli_preview_is_read_only(monkeypatch, tmp_path, capsys, args):
    bucket = MemoryBucket()
    bucket.put(RAW + "data.csv", b"abc")
    snapshot = deepcopy((bucket.data, bucket.generations, bucket.counter))
    assert run_cli(monkeypatch, tmp_path, bucket, "backfill", *args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["dry_run"] is True and result["marked"] == ["TMA37"]
    assert (bucket.data, bucket.generations, bucket.counter) == snapshot


def test_cli_backfill_and_verify_opt_in(monkeypatch, tmp_path, capsys):
    bucket = MemoryBucket()
    bucket.put(RAW + "data.csv", b"abc")
    assert run_cli(monkeypatch, tmp_path, bucket, "backfill", "--yes") == 0
    assert run_cli(monkeypatch, tmp_path, bucket, "verify", "--sample", "TMA37") == 1
    assert "Listing-only" in capsys.readouterr().out
    assert (
        run_cli(monkeypatch, tmp_path, bucket, "verify", "--sample", "TMA37", "--allow-backfilled")
        == 0
    )
    assert "no checksum verification" in capsys.readouterr().out


def test_inventory_distinguishes_backfill_changed_and_bucket_only(setup):
    remote, bucket, request = setup
    done = core.Transfer(remote, bucket, request).run()
    assert core.inventory(remote, bucket, request)["samples"][0]["status"] == "done"
    remote.data[f"{ROOT}/flatFiles/tma37/new.csv"] = b"new"
    assert core.inventory(remote, bucket, request)["samples"][0]["status"] == "changed_since_done"
    bucket.data[DONE] = json.dumps({**done, "provenance": "backfill"}).encode()
    assert core.inventory(remote, bucket, request)["samples"][0]["status"] == "done_backfilled"
    remote.data.clear()
    assert core.inventory(remote, bucket, request)["samples"][0]["status"] == "done_backfilled"


@pytest.mark.parametrize("marker", ["active", "stale", "failed", "text"])
def test_backfill_skips_unfinished_or_unowned_sample(marker):
    bucket = MemoryBucket()
    bucket.put(RAW + "partial.csv", b"partial")
    if marker in {"active", "stale"}:
        heartbeat = core.now() if marker == "active" else "2000-01-01T00:00:00+00:00"
        bucket.write(LEASE, {"heartbeat": heartbeat}, None)
    elif marker == "failed":
        bucket.write(FAILED, {"error": "transfer_failed"}, None)
    else:
        bucket.put(RAW + "done.txt", b"old marker")
    snapshot = deepcopy((bucket.data, bucket.generations, bucket.counter))
    assert backfill(bucket)["skipped"][0]["sample"] == "TMA37"
    assert (bucket.data, bucket.generations, bucket.counter) == snapshot


def test_concurrent_writer_wins_lease(monkeypatch):
    bucket = MemoryBucket()
    bucket.put(RAW + "data.csv", b"abc")
    original_write = bucket.write

    def claim_first(name, body, generation):
        if name == LEASE:
            original_write(name, {"heartbeat": core.now()}, None)
        return original_write(name, body, generation)

    monkeypatch.setattr(bucket, "write", claim_first)
    assert backfill(bucket)["skipped"] == [{"sample": "TMA37", "reason": "concurrent_update"}]
    assert DONE not in bucket.data


def test_backfill_cannot_publish_after_losing_lease(monkeypatch):
    bucket = MemoryBucket()
    bucket.put(RAW + "data.csv", b"abc")
    original_list = bucket.list

    def steal_lease(prefix):
        if prefix == RAW:
            bucket.write(LEASE, {"heartbeat": core.now()}, bucket.generations[LEASE])
        return original_list(prefix)

    monkeypatch.setattr(bucket, "list", steal_lease)
    with pytest.raises(core.Busy):
        backfill(bucket)
    assert DONE not in bucket.data
    assert RAW + "done.txt" not in bucket.data
    assert LEASE in bucket.data  # The new owner's lease must survive.


def test_transfer_failure_before_backfill_claim_is_respected(monkeypatch):
    bucket = MemoryBucket()
    bucket.put(RAW + "partial.csv", b"partial")
    original_write = bucket.write

    def fail_before_claim(name, body, generation):
        if name == LEASE and generation is None:
            original_write(FAILED, {"error": "transfer_failed"}, None)
        return original_write(name, body, generation)

    monkeypatch.setattr(bucket, "write", fail_before_claim)
    assert backfill(bucket)["skipped"] == [{"sample": "TMA37", "reason": "unfinished_transfer"}]
    assert DONE not in bucket.data
    assert LEASE not in bucket.data


def test_large_file_renews_lease_while_streaming(setup, monkeypatch):
    remote, bucket, request = setup
    remote.data[f"{ROOT}/flatFiles/tma37/large.bin"] = b"x" * (17 * 1024 * 1024)
    run, files = core.plan(remote, ROOT, "tma37", "TMA37")
    request["plan_fingerprint"] = core.fingerprint(run, files)
    ticks = iter(range(1_000, 100_000, 301))
    monkeypatch.setattr(core, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    progress = []
    done = core.Transfer(remote, bucket, request, progress.append).run()
    partial = [
        entry for entry in progress if entry["event"] == "progress" and entry["files_done"] == 4
    ]
    assert len(partial) >= 3  # Lease checks continue inside a multi-chunk file.
    assert done["total_bytes"] == sum(len(data) for data in remote.data.values())
    assert bucket.stat(RAW + "large.bin").size == 17 * 1024 * 1024


def test_existing_active_completion_is_not_repaired(setup):
    remote, bucket, request = setup
    done = core.Transfer(remote, bucket, request).run()
    bucket.delete(RAW + "done.txt", bucket.generations[RAW + "done.txt"])
    bucket.write(LEASE, {**done, "heartbeat": core.now()}, None)
    snapshot = deepcopy((bucket.data, bucket.generations, bucket.counter))
    assert backfill(bucket)["skipped"] == [{"sample": "TMA37", "reason": "unfinished_transfer"}]
    assert (bucket.data, bucket.generations, bucket.counter) == snapshot


def test_backfill_rereads_completion_after_claim(setup, monkeypatch):
    remote, bucket, request = setup
    done = core.Transfer(remote, bucket, request).run()
    bucket.delete(DONE, bucket.generations[DONE])
    for scope in core.SCOPES:
        path = core.marker_path("TMA37", scope)
        bucket.delete(path, bucket.generations[path])
    original_write = bucket.write

    def complete_before_claim(name, body, generation):
        if name == LEASE and generation is None:
            original_write(DONE, done, None)
        return original_write(name, body, generation)

    monkeypatch.setattr(bucket, "write", complete_before_claim)
    assert backfill(bucket)["repaired"] == ["TMA37"]
    assert bucket.read(DONE)[0] == done
    assert "verified: crc32c\n" in bucket.data[RAW + "done.txt"].decode()


def test_transfer_guards_and_schema(setup):
    remote, bucket, request = setup
    transfer = core.Transfer(remote, bucket, request)
    jsonschema.validate(transfer.marker, SCHEMA)
    bucket.put(RAW + "data.csv", b"legacy")
    backfill(bucket)
    with pytest.raises(core.Busy, match="done marker"):
        transfer.run()
    body = bucket.read(DONE)[0]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**body, "manifest": []}, SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**body, "provenance": "transfer"}, SCHEMA)


def test_extra_destination_object_prevents_completion(setup):
    remote, bucket, request = setup
    bucket.put(RAW + "unexpected.csv", b"extra")
    with pytest.raises(RuntimeError, match="verify"):
        core.Transfer(remote, bucket, request).run()
    assert DONE not in bucket.data
    assert RAW + "done.txt" not in bucket.data
    jsonschema.validate(bucket.read(FAILED)[0], SCHEMA)


def test_gcs_folders_consumes_all_pages():
    class Listing:
        prefixes = set()

        def __iter__(self):
            for sample in ("A", "B", "C"):
                self.prefixes.add(f"raw_outputs/raw/{sample}/")
                yield object()

    class FakeCloudBucket:
        def list_blobs(self, **kwargs):
            assert kwargs == {"prefix": "raw_outputs/raw/", "delimiter": "/"}
            return Listing()

    adapter = GCS.__new__(GCS)
    adapter.bucket = FakeCloudBucket()
    assert adapter.folders("raw_outputs/raw/") == [
        "raw_outputs/raw/A/",
        "raw_outputs/raw/B/",
        "raw_outputs/raw/C/",
    ]


def export_tree(slides: dict[str, str], root: str = ROOT) -> MemoryRemote:
    """A synthetic export whose slide folders hold runs at the given relative paths."""
    remote = MemoryRemote()
    remote.data = {}
    for slide, run in slides.items():
        decoded = f"{root}/DecodedFiles/{slide}/{run}"
        remote.data.update(
            {
                f"{decoded}/CellStatsDir/Morphology2D/image.tif": b"pixels",
                f"{decoded}/RunSummary/Morphology_ChannelID_Dictionary.txt": b"map",
                f"{root}/flatFiles/{slide}/cells.csv": b"cells",
            }
        )
    return remote


def search(remote: MemoryRemote, sample: str = "TMA37", **extra: Any) -> dict[str, Any]:
    request = {"sample": sample, "requested_by": "manual:test", "login_saved_by": "test", **extra}
    return core.inventory(remote, MemoryBucket(), request)


@pytest.mark.parametrize(
    ("remote", "wanted", "same"),
    [
        ("TMA37", "TMA37", True),
        ("tma37", "TMA37", True),
        ("TMA_037", "TMA37", True),
        ("tma-37 ", "TMA37", True),
        ("37", "TMA37", True),
        ("TMA37", "37", True),
        ("TMA3", "TMA37", False),
        ("TMA370", "TMA37", False),
        ("Slide37", "TMA37", False),
        ("", "TMA37", False),
    ],
)
def test_sample_names_match_without_spelling_details(remote, wanted, same):
    assert core.same_sample(remote, wanted) is same


def test_inventory_finds_loosely_named_slide_and_keeps_bucket_name():
    remote = export_tree({"tma_037": RUN, "TMA38": RUN})
    [item] = search(remote)["samples"]
    assert (item["sample"], item["as_sample"], item["matched"]) == ("tma_037", "TMA37", True)
    assert item["export_root"] == ROOT and item["plan_fingerprint"]


def test_inventory_finds_nested_export_named_for_the_sample():
    # AtoMx names the export folder after what was typed and appends the export time.
    root = "/Lab exports/2026/TMA37_05_11_2026_22_59_07_541"
    remote = export_tree({"Slide 1": f"run-batch/{RUN}"}, root)
    remote.data[f"/Other_19_08_2026_13_58_54_505/DecodedFiles/TMA19/{RUN}/a.csv"] = b"x"
    result = search(remote)
    [item] = result["samples"]
    assert (item["export_root"], item["sample"], item["as_sample"]) == (root, "Slide 1", "TMA37")
    assert item["run_folder"] == f"run-batch/{RUN}"
    run, files = core.plan(remote, root, "Slide 1", "TMA37")
    assert run == item["run_folder"]
    assert {f.name for f in files} == {
        "raw_outputs/raw/TMA37/Morphology2D/image.tif",
        "raw_outputs/raw/TMA37/Morphology_ChannelID_Dictionary.txt",
        "raw_outputs/raw/TMA37/cells.csv",
    }
    assert {e["export_root"] for e in result["searched"]["exports"]} == {
        root,
        "/Other_19_08_2026_13_58_54_505",
    }


def test_timestamped_slide_folder_matches_its_sample():
    [item] = search(export_tree({"TMA37_05_11_2026_22_59_07_541": RUN, "TMA38": RUN}))["samples"]
    assert (item["sample"], item["as_sample"]) == ("TMA37_05_11_2026_22_59_07_541", "TMA37")


def test_exact_folder_name_wins_and_ambiguous_names_are_all_reported():
    assert [x["sample"] for x in search(export_tree({"TMA37": RUN, "tma_37": RUN}))["samples"]] == [
        "TMA37"
    ]
    loose = search(export_tree({"tma37": RUN, "tma_37": RUN}))["samples"]
    assert sorted(x["sample"] for x in loose) == ["tma37", "tma_37"]


def test_inventory_miss_lists_what_it_searched():
    remote = export_tree({"TMA3": RUN, "TMA370": RUN})
    result = search(remote)
    assert result["samples"] == []
    assert result["searched"]["exports"] == [{"export_root": ROOT, "samples": ["TMA3", "TMA370"]}]
    assert result["searched"]["truncated"] is False


def test_matching_slide_without_flat_files_is_reported_not_hidden():
    remote = export_tree({"TMA37": RUN})
    del remote.data[f"{ROOT}/flatFiles/TMA37/cells.csv"]
    [item] = search(remote)["samples"]
    assert item["status"] == "incomplete" and "plan_fingerprint" not in item
    assert item["problem"] == "No flatFiles folder for this slide"


def test_export_search_is_bounded(monkeypatch):
    remote = MemoryRemote()
    remote.data = {f"/a{i}/b/c/d/file": b"x" for i in range(20)}
    monkeypatch.setattr(core, "SEARCH_LISTINGS", 10)
    searched = search(remote)["searched"]
    assert searched["truncated"] is True and searched["listings"] == 10
    monkeypatch.setattr(core, "SEARCH_LISTINGS", 400)
    remote.data = {f"/a/b/c/d/TMA37_05_11_2026_22_59_07_541/DecodedFiles/TMA37/{RUN}/x": b"x"}
    # Deeper than the search depth: never crawled, so a miss stays cheap.
    assert search(remote)["searched"]["exports"] == []


def test_explicit_export_folder_limits_the_search():
    remote = export_tree({"TMA37": RUN})
    remote.data.update(export_tree({"TMA37": RUN}, "/Other").data)
    [item] = search(remote, export_root="/Other")["samples"]
    assert item["export_root"] == "/Other"
