"""Trusted, synthetic-data Spot acceptance; does not send any Slack messages."""

import asyncio
import dataclasses
import json
import os
import shlex
import shutil
from unittest.mock import patch

import httpx
from claude_agent_sdk import ToolResultBlock, UserMessage
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from slack_sdk.web.async_client import AsyncWebClient

from pistachio.agents.analysis import runner as runner_module
from pistachio.agents.analysis.batch_worker import restore, snapshot
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.preflight import check
from pistachio.agents.analysis.runner import Context, Runner
from pistachio.agents.analysis.storage import GCSStorage


async def main():
    settings = Settings()
    storage = GCSStorage(settings.bucket)
    report = {"passed": False, "stage": "bootstrap"}
    try:
        check(settings)
        audience = os.environ["PISTACHIO_CONTROL_PLANE_URL"]
        url = os.environ["CANARY_CREDENTIALS_URL"]
        token = id_token.fetch_id_token(Request(), audience)
        async with httpx.AsyncClient(timeout=30) as http:
            response = await http.get(url, headers={"Authorization": f"Bearer {token}"})
            response.raise_for_status()
            settings = Settings(**response.json())
            models = await http.get(
                "https://api.anthropic.com/v1/models",
                headers={
                    "x-api-key": settings.anthropic_api_key.get_secret_value(),
                    "anthropic-version": "2023-06-01",
                },
            )
            models.raise_for_status()
            available = [x["id"] for x in models.json()["data"]]
        report["available_models"] = available
        if settings.model not in available:
            settings.model = next(x for x in available if "opus" in x)
        settings.fallback_model = ""
        settings.max_budget_usd_per_turn = 3
        report["model"] = settings.model
        auth = await AsyncWebClient(token=settings.slack_bot_token.get_secret_value()).auth_test()
        report["slack_auth"] = bool(auth["ok"])
        scopes = set(auth.headers.get("x-oauth-scopes", "").split(","))
        report["missing_slack_scopes"] = sorted(
            {"chat:write", "im:history", "files:read", "files:write"} - scopes
        )
        report["display_names_available"] = "users:read" in scopes
        assert not report["missing_slack_scopes"], "Required Slack scope missing"
        print(
            json.dumps(
                {
                    "stage": "credentials",
                    "model": settings.model,
                    "missing_slack_scopes": report["missing_slack_scopes"],
                }
            ),
            flush=True,
        )
        home = settings.work_root.parent
        outside = home / "outside-probe"
        outside.mkdir(exist_ok=True)
        (outside / "control").write_text("trusted write works")
        data = settings.data_mount_root / settings.bucket / "inputs/slack-fixture"
        # Exercise the same long private-workspace layout used by real DM workers.
        workdir = settings.work_root / ("u" * 32) / ("p" * 18) / ("t" * 32) / ("a" * 24)
        assert len(str(workdir / ".tmp").encode()) > 108
        ctx = Context(workdir, data, "slack-fixture")
        report["private_workspace_path_bytes"] = len(str(workdir).encode())
        runner = Runner(settings)

        async def status(text):
            print(json.dumps({"stage": "tool", "description": str(text)}), flush=True)

        # Reproduce the old failure on synthetic, read-only data, then resume that
        # same SDK session with the production fix. Inspect actual tool errors so a
        # successful model response cannot hide a broken shell.
        sdk_query, make_options = runner_module.query, runner_module.build_options

        async def checked_turn(prompt, *, legacy=False):
            failures = []
            tool_results = []

            def options(*args, **kwargs):
                value = make_options(*args, **kwargs)
                if legacy:
                    value.env["CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR"] = "0"
                return value

            async def recorded_query(**kwargs):
                async for message in sdk_query(**kwargs):
                    if isinstance(message, UserMessage) and isinstance(message.content, list):
                        for block in message.content:
                            if isinstance(block, ToolResultBlock):
                                tool_results.append(
                                    {"is_error": block.is_error, "content": str(block.content)}
                                )
                                if block.is_error:
                                    failures.append(str(block.content))
                    yield message

            with (
                patch.object(runner_module, "build_options", options),
                patch.object(runner_module, "query", recorded_query),
            ):
                result = await runner.run_turn(ctx, prompt, status)
            report[report["stage"] + "_results"] = tool_results
            return result, failures

        # The reported failure is below the data root, not at the root itself.
        storage.put("inputs/slack-fixture/cwd-probe/marker.txt", b"synthetic cwd fixture")
        python_pwd = "/opt/sci/bin/python -c 'import os; print(os.getcwd())'"
        # Initialize the sandbox in the workspace before a compound directory change,
        # matching the failed real thread (list workspace, cd + inspect, then Python).
        change_dir = "cd " + shlex.quote(str(data / "cwd-probe")) + " && " + python_pwd
        report["stage"] = "reproduce_legacy_cwd"
        legacy, failures = await checked_turn(
            "Synthetic deployment regression test. Ignore normal analysis workflow. "
            "Make exactly three sequential Bash calls: first run exactly "
            + python_pwd
            + "; next run exactly "
            + change_dir
            + "; then in a SEPARATE Bash call run exactly "
            + python_pwd
            + ". Do not combine commands, "
            "use a subshell, change back, or attempt repairs. Report the tool error and stop.",
            legacy=True,
        )
        commands = [x["input"]["command"].strip() for x in legacy.run_log if x["tool"] == "Bash"]
        report["legacy_text"] = legacy.text
        report["legacy_tools"] = legacy.run_log
        assert commands[:3] == [python_pwd, change_dir, python_pwd], commands
        # Depending on SDK sandbox initialization, the next command either errors
        # immediately or executes in the wrong cwd. Both demonstrate the regression.
        metadata_failure = any("Read-only file system" in x and ".claude" in x for x in failures)
        assert metadata_failure or (
            report["reproduce_legacy_cwd_results"][-1]["content"].strip() == str(data / "cwd-probe")
        ), report["reproduce_legacy_cwd_results"]
        assert legacy.session_id
        report["legacy_cwd_failure_reproduced"] = metadata_failure
        report["legacy_cwd_drift_reproduced"] = True
        report["legacy_cost"] = legacy.cost
        ctx.session_id = legacy.session_id
        check_cwd = "/opt/sci/bin/python -c " + shlex.quote(
            "from pathlib import Path; assert Path.cwd() == Path("
            + repr(str(workdir))
            + "); Path('outputs/cwd-reset.txt').write_text('ok')"
        )
        report["stage"] = "first_turn"
        first, failures = await checked_turn(
            "The runtime has been repaired; this is the SAME session. First make two "
            "separate sequential Bash calls, exactly: "
            + change_dir
            + " and then "
            + check_cwd
            + ". Do not combine them, use subshells, or change back. "
            "Both must succeed. Then continue acceptance on synthetic data: run exactly "
            f"/opt/sci/bin/python /opt/pistachio/tools/sandbox_probe.py {data}/spatial.h5ad "
            f"{outside}/blocked. If the probe fails, stop and report the failure. "
            "If it passes, inspect spatial.h5ad, write code/plot.py and use it to plot sample B "
            "colored by annotated_cell_type to outputs/plot.png. Use backed h5ad loading. "
            "Reply starting with Data:."
        )
        report["first_error"], report["first_cost"] = first.error, first.cost
        report["first_tools"] = first.run_log
        report["first_text"] = first.text
        assert not first.error, first.error
        assert not failures, failures
        commands = [x["input"]["command"].strip() for x in first.run_log if x["tool"] == "Bash"]
        assert commands[:2] == [change_dir, check_cwd], commands
        assert (ctx.workdir / "outputs/cwd-reset.txt").read_text() == "ok"
        assert first.session_id == legacy.session_id
        report["poisoned_session_recovered"] = True
        report["cwd_reset_between_commands"] = True
        probe = json.loads((ctx.workdir / "outputs/sandbox-probe.json").read_text())
        assert all(probe.values()) and not (outside / "blocked").exists()
        assert (ctx.workdir / "outputs/plot.png").stat().st_size > 100
        report["probe"] = probe
        scripts = {p.name: p.read_bytes() for p in (ctx.workdir / "code").glob("*.py")}
        assert scripts
        ctx.session_id = first.session_id
        archive = home / "acceptance.tar.gz"
        snapshot(archive, home)
        artifact_prefix = os.environ["CANARY_REPORT_KEY"].removesuffix(".json")
        storage.upload(f"{artifact_prefix}/checkpoint.tar.gz", archive)
        archive.unlink()
        # These directories belong exclusively to this disposable acceptance worker.
        shutil.rmtree(settings.work_root)
        shutil.rmtree(home / ".claude/projects")
        storage.bucket.blob(f"{artifact_prefix}/checkpoint.tar.gz").download_to_filename(
            str(archive)
        )
        restore(archive, home)
        report["stage"] = "restored_turn"
        second, failures = await checked_turn(
            "After checkpoint restore, again make two separate sequential Bash calls exactly: "
            + change_dir
            + " and then "
            + check_cwd
            + ". Do not combine commands or change back. Then make the points bigger "
            "in the earlier figure. Edit and rerun the existing script."
        )
        report["second_error"], report["second_cost"] = second.error, second.cost
        report["second_tools"] = second.run_log
        assert not second.error, second.error
        assert not failures, failures
        commands = [x["input"]["command"].strip() for x in second.run_log if x["tool"] == "Bash"]
        assert commands[:2] == [change_dir, check_cwd], commands
        report["restored_cwd_reset"] = True
        assert second.session_id == first.session_id
        assert any(
            (ctx.workdir / "code" / name).read_bytes() != old for name, old in scripts.items()
        )
        assert any(p.suffix == ".png" for p in second.files)
        storage.upload(f"{artifact_prefix}/plot.png", ctx.workdir / "outputs/plot.png")
        report["plot_uri"] = f"gs://{settings.bucket}/{artifact_prefix}/plot.png"
        report["checkpoint_gcs_roundtrip"] = True
        report["context"] = dataclasses.asdict(ctx)
        report["passed"] = True
        report["stage"] = "complete"
    except Exception as exc:
        report["failure"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        storage.put(os.environ["CANARY_REPORT_KEY"], json.dumps(report, default=str).encode())
        print(
            json.dumps({k: report[k] for k in ("stage", "passed", "failure") if k in report}),
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
