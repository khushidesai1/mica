"""Apply the Slack-only integration to an existing API image, failing on unexpected anchors."""

from pathlib import Path


def patch(package: Path) -> None:
    def replace(relative: str, before: str, after: str) -> None:
        path = package / relative
        text = path.read_text()
        if text.count(before) != 1:
            raise RuntimeError(f"Unexpected deployed source: {relative}")
        path.write_text(text.replace(before, after, 1))

    replace(
        "config.py",
        "from pydantic import ",
        "from pydantic import SecretStr\nfrom pydantic import ",
    )
    replace(
        "config.py",
        '    slack_bot_token: str = ""\n',
        """    slack_bot_token: str = ""
    slack_agent_backend: Literal["existing", "claude_batch"] = "existing"
    slack_agent_image: str = ""
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""), validation_alias="ANTHROPIC_API_KEY"
    )
""",
    )
    replace(
        "api/app.py",
        "from pistachio.connectors.slack import router as slack_router",
        "from pistachio.api.slack_analysis import router as slack_analysis_router\n"
        "from pistachio.connectors.slack import router as slack_router",
    )
    replace(
        "api/app.py",
        "    app.include_router(slack_router)",
        "    app.include_router(slack_router)\n    app.include_router(slack_analysis_router)",
    )
    replace(
        "connectors/slack.py",
        """    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
""",
        """    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    if services.settings.slack_agent_backend == "claude_batch":
        from pistachio.api.slack_analysis import receive
        return await receive(services, payload)
""",
    )


if __name__ == "__main__":
    from importlib.util import find_spec

    spec = find_spec("pistachio")
    assert spec and spec.origin
    patch(Path(spec.origin).parent)
