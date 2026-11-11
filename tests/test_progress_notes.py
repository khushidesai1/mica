"""Public narration stays bounded, literal, and separate from terminal results."""

import pytest

from mica.agents.analysis.progress import TaskProgress, note_headline, public_note
from mica.agents.analysis.slack_format import activity_blocks, activity_fallback, status_blocks


def test_public_note_cleaning_and_headline_bounds():
    sentinel = "This part of the response was interrupted before it finished."
    assert public_note(" \n\t ") == public_note(sentinel) == ""
    assert public_note("Found\n the table.\t" + sentinel) == "Found the table."
    assert note_headline("Found the table. Now checking counts.") == "Found the table."
    assert len(public_note("word " * 100)) <= 300
    assert len(note_headline("word " * 100)) <= 90
    assert note_headline("word " * 100).endswith("word…")
    assert len(note_headline("x" * 400)) == 90


@pytest.mark.parametrize(
    "headline,summary,note,expected",
    [
        (
            "Mica is working",
            "Waiting for a worker",
            "Found the table. Checking counts.",
            "Waiting for a worker",
        ),
        ("Mica is working", "Waiting for a worker", "", "Waiting for a worker"),
        ("Starting the worker…", "", "", "Starting the worker…"),
        ("Mica is working", "", "", "Mica is working"),
        ("Mica is thinking", "", "", "Mica is thinking"),
    ],
)
def test_activity_title_precedence(headline, summary, note, expected):
    blocks = activity_blocks([], headline=headline, summary=summary, note=note)
    assert blocks[-1]["title"] == expected
    if note:
        assert blocks[0] == {"type": "section", "text": {"type": "plain_text", "text": note}}
    assert "Reviewing" not in repr(blocks)


@pytest.mark.parametrize("error", [None, "Interrupted"])
def test_terminal_cards_drop_notes_but_keep_tasks_and_errors(error):
    task = TaskProgress("one", "Read table", "Reading", "complete")
    blocks = activity_blocks(
        [task], note="Now checking private-data counts", finished=True, error=error
    )
    assert blocks[0]["title"] == ("Mica stopped" if error else "Mica finished")
    assert "private-data" not in repr(blocks)
    assert blocks[0]["tasks"][0]["title"] == "Read table"


def test_notes_are_literal_in_native_and_compatibility_blocks():
    note = "Found <!channel> and *bold*. Now checking counts."
    blocks = activity_blocks([], note=note)
    assert blocks[-1]["title"] == "Mica is working"
    assert blocks[0]["text"] == {"type": "plain_text", "text": note}
    fallback = activity_fallback(blocks)
    assert fallback[0] == blocks[0]
    assert fallback[1]["elements"][0]["type"] == "plain_text"
    assert "mrkdwn" not in repr(blocks + activity_fallback(blocks))
    assert status_blocks(":x:", "Mica is working", note=note) == blocks


def test_current_task_stays_glanceable_with_commentary_and_parallel_work():
    tasks = [TaskProgress(str(i), f"Checking sample {i}", "Checking") for i in range(3)]
    note = "I found three samples. I'm checking their counts."
    blocks = activity_blocks(tasks, note=note, summary="Old activity")
    assert blocks[0]["text"]["text"] == note
    assert blocks[1]["title"] == "Checking sample 2"
    assert len(blocks[1]["tasks"]) == 2
    assert "3 steps in progress" in repr(blocks[1])
    assert "Old activity" not in repr(blocks)


@pytest.mark.parametrize("state", ["waiting", "cancelled", "expired"])
def test_inactive_notices_do_not_claim_execution_or_success(state):
    blocks = status_blocks("", "Approval " + state, state=state, detail="Nothing has run.")
    assert blocks[0]["type"] == "context"
    assert "Nothing has run." in repr(blocks)
    assert "in_progress" not in repr(blocks) and "complete" not in repr(blocks)
