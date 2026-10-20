import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from mica.agents.analysis.registry import Binding, Registry, RegistryDoc
from mica.agents.analysis.storage import Conflict, LocalStorage
from tools.migrate_slack_registry import apply, report

A, B = "alice@example.com", "bob@example.com"


def test_first_invitation_selects_normalized_recipient_and_later_invites_preserve_choice():
    doc = RegistryDoc()
    first = doc.create(A, "first", "gs://data/first/")
    second = doc.create(A, "second", "gs://data/second/")
    doc.share(first, A, B.upper())
    assert doc.defaults == {B: first}
    assert doc.active_for(B, "EMAIL", "E1", private=True) == first
    doc.share(second, A, B)
    assert doc.defaults[B] == first
    doc.bind("EMAIL", "E1", B, second, private=True)
    doc.share(first, A, B)
    assert doc.defaults[B] == second
    assert not doc.can_use(first, "different@example.com")
    assert not doc.projects[first].published_channels


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("revoked", [False, True])
def test_invitation_preserves_explicit_and_legacy_choices_even_if_revoked(legacy, revoked):
    doc = RegistryDoc()
    old = doc.create(A, "old-project", "gs://data/old/")
    new = doc.create(A, "new-project", "gs://data/new/")
    doc.projects[old].members.add(B)
    if legacy:
        doc.bindings["T1:D1"] = Binding(project_id=old, bound_by=B)
    else:
        doc.defaults[B] = old
    if revoked:
        doc.share(old, A, B, remove=True)
    doc.share(new, A, B)
    assert doc.active_for(B, "EMAIL", "E1", private=True) == (None if revoked else old)
    assert doc.defaults == ({} if legacy else {B: old})


def test_concurrent_first_invites_choose_once_and_replay_cannot_restore_access(tmp_path):
    registry = Registry(LocalStorage(tmp_path))
    projects = [
        registry.update(name, A, name, lambda d, name=name: d.create(A, name, "gs://data/"))
        for name in ("first", "second")
    ]

    def share(pid):
        return registry.update("share-" + pid, A, "share", lambda d: d.share(pid, A, B))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(share, projects))
    selected = registry.load().defaults[B]
    assert selected in projects
    assert all(registry.load().can_use(pid, B) for pid in projects)
    registry.update("revoke", A, "unshare", lambda d: d.share(selected, A, B, remove=True))
    share(selected)
    doc = registry.load()
    assert doc.defaults[B] == selected
    assert not doc.can_use(selected, B)
    assert doc.active_for(B, "EMAIL", "E1", private=True) is None


def test_compare_and_swap_and_concurrent_updates(tmp_path):
    store = LocalStorage(tmp_path)
    gen = store.put_if("value", b"one", None)
    with pytest.raises(Conflict):
        store.put_if("value", b"two", None)
    store.put_if("value", b"two", gen)
    registry = Registry(store)

    def create(i):
        return registry.update(str(i), A, str(i), lambda d: d.create(A, f"proj-{i}", "gs://data/"))

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(create, range(3)))
    assert len(registry.load().projects) == 3


def test_names_are_private_and_replayed_share_cannot_regrant(tmp_path):
    registry = Registry(LocalStorage(tmp_path))
    a = registry.update("a", A, "create", lambda d: d.create(A, "liver", "gs://data/a/"))
    b = registry.update("b", B, "create", lambda d: d.create(B, "liver", "gs://data/b/"))
    assert a != b
    assert registry.load().find("liver", B) == b
    with pytest.raises(ValueError):
        registry.load().find(a, B)

    def share(d):
        return d.share(a, A, B)

    registry.update("share", A, "share", share)
    registry.update("unshare", A, "unshare", lambda d: d.share(a, A, B, remove=True))
    registry.update("share", A, "share", share)
    assert not registry.load().can_use(a, B)
    with pytest.raises(ValueError):
        registry.update("share", B, "share", share)


def test_binding_requires_explicit_audience_publication_and_owner():
    doc = RegistryDoc()
    a, b = doc.create(A, "liver", "gs://data/"), doc.create(B, "brain", "gs://data/")
    doc.share(a, A, B)
    doc.bind("T1", "D2", B, a, private=True)
    with pytest.raises(ValueError, match="publish"):
        doc.bind("T1", "C1", A, a, private=False)
    doc.publish(a, A, "T1", "C1")
    doc.bind("T1", "C1", A, a, private=False)
    doc.publish(b, B, "T1", "C1")
    with pytest.raises(ValueError, match="current project's owner"):
        doc.bind("T1", "C1", B, b, private=False, force=True)
    with pytest.raises(ValueError, match="owner"):
        doc.bind("T1", "C2", B, a, private=False)
    doc.publish(a, A, "T1", "C1", remove=True)
    assert "T1:C1" not in doc.projects[a].published_channels


def test_concurrent_renames_cannot_create_duplicate_owned_names(tmp_path):
    registry = Registry(LocalStorage(tmp_path))
    projects = [
        registry.update(name, A, "create", lambda d, name=name: d.create(A, name, "gs://data/"))
        for name in ("liver", "brain")
    ]

    def rename(pid):
        try:
            registry.update(f"rename-{pid}", A, "rename", lambda d: d.rename(pid, A, "atlas"))
            return True
        except ValueError as exc:
            assert "already have" in str(exc)
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(rename, projects)) == [False, True]
    assert sum(p.name == "atlas" for p in registry.load().projects.values()) == 1


def test_rename_preserves_legacy_id_and_allows_other_owners_names(tmp_path):
    registry = Registry(LocalStorage(tmp_path))

    def seed(doc):
        pid = doc.create(A, "liver", "gs://data/")
        doc.projects["liver"] = doc.projects.pop(pid)
        doc.create(B, "atlas", "gs://data/b/")
        return "liver"

    registry.update("seed", A, "seed", seed)
    registry.update("rename", A, "rename", lambda d: d.rename("liver", A, "atlas"))
    doc = registry.load()
    assert doc.find("atlas", A) == doc.find("liver", A) == "liver"
    assert doc.find("atlas", B) != "liver"
    registry.update("rename-again", A, "rename", lambda d: d.rename("liver", A, "atlas-final"))
    registry.update("rename", A, "rename", lambda d: d.rename("liver", A, "atlas"))
    assert registry.load().projects["liver"].name == "atlas-final"


def test_migration_requires_drain_preserves_legacy_and_detects_drift(tmp_path):
    store = LocalStorage(tmp_path)
    raw = json.dumps(
        {"created_by": "U1", "slack_channel_id": "C1", "data_uri": "gs://data/"}
    ).encode()
    store.put("projects/liver/project.json", raw)
    store.put("projects/liver/PROJECT.md", b"private brief")
    rows = report(store, {"T1:U1": A}, "T1")
    assert rows[0]["owner"] == A and rows[0]["brief_bytes"] == 13
    with pytest.raises(ValueError, match="drain"):
        apply(store, rows, {"liver": A}, "T1", quiesced=False)
    assert apply(store, rows, {}, "T1", quiesced=True) == ["liver"]
    assert apply(store, rows, {"liver": A}, "T1", quiesced=True) == []
    doc = Registry(store).load()
    assert doc.bound("T1", "C1") == "liver"
    assert not doc.projects["liver"].published_channels
    before = store.keys("projects/")
    apply(store, rows, {"liver": A}, "T1", quiesced=True)
    assert store.keys("projects/") == before
    assert store.get("projects/liver/project.json")[0] == raw
    with pytest.raises(ValueError, match="drift"):
        apply(store, rows, {"liver": B}, "T1", quiesced=True)
