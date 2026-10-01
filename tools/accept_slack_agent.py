"""Trusted, synthetic-data Spot acceptance; does not send any Slack messages."""

import asyncio
import dataclasses
import json
import os
import shutil

import httpx
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from slack_sdk.web.async_client import AsyncWebClient

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
        ctx = Context(settings.work_root / "acceptance", data, "slack-fixture")
        runner = Runner(settings)

        async def status(text):
            print(json.dumps({"stage": "tool", "description": text}), flush=True)

        report["stage"] = "first_turn"
        first = await runner.run_turn(
            ctx,
            "Deployment acceptance on synthetic data only. First run exactly "
            f"/opt/sci/bin/python /opt/pistachio/tools/sandbox_probe.py {data}/spatial.h5ad "
            f"{outside}/blocked. If the probe fails, stop and report the failure. "
            "If it passes, inspect spatial.h5ad, write code/plot.py and use it to plot sample B "
            "colored by annotated_cell_type to outputs/plot.png. Use backed h5ad loading. "
            "Reply starting with Data:.",
            status,
        )
        report["first_error"], report["first_cost"] = first.error, first.cost
        report["first_tools"] = first.run_log
        report["first_text"] = first.text
        assert not first.error, first.error
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
        second = await runner.run_turn(
            ctx,
            "Make the points bigger in the earlier figure. Edit and rerun the existing script.",
            status,
        )
        report["second_error"], report["second_cost"] = second.error, second.cost
        report["second_tools"] = second.run_log
        assert not second.error, second.error
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
