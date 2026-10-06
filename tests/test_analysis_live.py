"""Explicitly opt-in paid integration test. Offline tests never contact Claude."""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.runner import Context, Runner


@pytest.mark.live
async def test_real_plot_and_resume(tmp_path):
    from make_fixture import make_fixture

    if os.environ.get("PISTACHIO_RUN_LIVE_TESTS") != "1" or not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("Requires PISTACHIO_RUN_LIVE_TESTS=1 and ANTHROPIC_API_KEY; uses API credits")
    make_fixture(tmp_path / "data")
    settings = Settings(
        _env_file=None,
        sci_python=Path(sys.executable),
        tools_dir=Path(__file__).resolve().parents[1] / "tools",
        work_root=tmp_path / "work",
    )
    ctx = Context(tmp_path / "work/t1", tmp_path / "data")
    runner = Runner(settings)
    first = await runner.run_turn(
        ctx, "Plot sample B colored by annotated_cell_type. Use spatial.h5ad.", AsyncMock()
    )
    assert not first.error and first.text.startswith("Data:")
    assert any(p.suffix == ".png" and p.stat().st_size > 100 for p in first.files)
    scripts = set((ctx.workdir / "code").glob("*.py"))
    contents = {p: p.read_bytes() for p in scripts}
    ctx.session_id = first.session_id
    second = await runner.run_turn(
        ctx, "Make the points bigger; edit and rerun the existing script.", AsyncMock()
    )
    assert not second.error and second.session_id == first.session_id
    assert any(p.exists() and p.read_bytes() != contents[p] for p in scripts)
    assert any(p.suffix == ".png" for p in second.files)


@pytest.mark.live
async def test_agent_looks_at_its_figure(tmp_path):
    if os.environ.get("PISTACHIO_RUN_LIVE_TESTS") != "1" or not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("Requires explicit approval and live API credits")
    settings = Settings(
        _env_file=None,
        sci_python=Path(sys.executable),
        tools_dir=Path(__file__).resolve().parents[1] / "tools",
        work_root=tmp_path / "work",
    )
    ctx = Context(tmp_path / "work/t1", tmp_path / "data")
    ctx.data_path.mkdir()
    result = await Runner(settings).run_turn(
        ctx,
        "Create one large red square with matplotlib at 200 dpi, then a 6000x4000 PNG "
        "of the same square. Save both in outputs. Preview and Read both, then name the "
        "color you saw in each preview.",
        AsyncMock(),
    )
    assert result.error is None
    assert "red" in result.text.lower()
    reads = [
        x
        for x in result.run_log
        if x["tool"] == "Read" and ".previews/" in x["input"].get("file_path", "")
    ]
    assert len(reads) >= 2
    assert "JSON message exceeded maximum buffer size" not in result.text
