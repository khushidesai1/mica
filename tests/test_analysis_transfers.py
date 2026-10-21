from mica.agents.analysis.transfers import marker_state, stamp


def test_backfilled_marker_is_not_displayed_as_verified():
    assert marker_state({"done": {"provenance": "backfill"}}) == (
        "done (backfilled; listing-only, not verified)"
    )
    assert marker_state({"done": {}}) == "done (marker; manifest not rechecked here)"


def test_active_transfer_still_takes_precedence():
    assert (
        marker_state(
            {
                "done": {"provenance": "backfill"},
                "in_progress": {"heartbeat": stamp()},
            }
        )
        == "in_progress"
    )
