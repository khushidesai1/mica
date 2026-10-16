"""Provider request compatibility for the configured conversation model."""

import json

import httpx

from mica.agents.analysis import direct
from mica.agents.analysis.config import Settings


async def test_router_works_when_provider_rejects_forced_tool_choice(monkeypatch):
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        if body.get("tool_choice", {}).get("type") in {"tool", "any"}:
            return httpx.Response(
                400,
                json={
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": 'tool_choice: type "tool" and "any" are not supported '
                        "for this model.",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "route_request",
                        "input": {"route": "reply", "reply": "Hello!"},
                    }
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(direct.httpx, "AsyncClient", lambda **kwargs: client)
    result = await direct.decide(
        "synthetic-key",
        Settings(_env_file=None).model,
        {"request": "Hello"},
    )
    assert result.route == "reply" and result.reply == "Hello!"
    assert len(calls) == 1
    assert calls[0]["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
