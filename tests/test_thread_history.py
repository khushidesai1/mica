from mica.agents.analysis.storage import LocalStorage
from mica.agents.analysis.thread_history import History


def test_legacy_whole_words_and_newer_ties(tmp_path):
    history = History(LocalStorage(tmp_path), "thread")
    for seq, text in [(9, "Excellent"), *[(i, "CELL") for i in range(8, 0, -1)]]:
        history.record({"id": f"event{seq}", "seq": seq, "assistant": text})
    # The bounded legacy page selects newer ties and presents them chronologically.
    assert [r["seq"] for r in history.recall("cell")["matches"]] == [5, 6, 7, 8]


def test_legacy_outline_keeps_storage_paging_and_never_copies_transcripts(tmp_path):
    history = History(LocalStorage(tmp_path), "thread")
    for seq in range(105):
        history.record({"id": f"event{seq:03}", "seq": seq, "success": seq % 2 == 0,
                        "user": "PRIVATE transcript", "assistant": "Excellent"})
    page = history.recall("cell")
    assert page["matches"] == [] and page["next_offset"] == 100
    assert [r["event_id"] for r in page["outline"]] == [f"event{i:03}" for i in range(99, 79, -1)]
    assert all(row["date"] is None for row in page["outline"])
    assert "PRIVATE" not in str(page["outline"])
    last = history.recall("cell", offset=page["next_offset"])
    assert len(last["outline"]) == 5 and last["next_offset"] is None
    assert "PRIVATE transcript" in history.recall(event_id="event104")["text"]


def test_legacy_recall_casefolds_unicode_words(tmp_path):
    history = History(LocalStorage(tmp_path), "thread")
    history.record({"id": "unicode", "seq": 1, "assistant": "Straße café"})
    assert history.recall("STRASSE")["matches"][0]["id"] == "unicode"
    assert history.recall("CAFÉ")["matches"][0]["id"] == "unicode"
