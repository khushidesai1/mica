"""Keep the layered Slack image independent of the local browser hardening changes."""

import ast
from pathlib import Path


def test_slack_modules_do_not_import_browser_memory_or_services():
    root = Path(__file__).resolve().parents[1] / "src/pistachio"
    files = [*(root / "agents/analysis").glob("*.py"), root / "api/slack_analysis.py"]
    forbidden = (
        "pistachio.services",
        "pistachio.agents.memory",
        "pistachio.agents.context",
        "pistachio.agents.harness",
        "pistachio.agents.session",
    )
    for path in files:
        for node in ast.walk(ast.parse(path.read_text())):
            modules = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            for module in modules:
                assert not module.startswith(forbidden), (path, module)
                if module == "pistachio.api.auth":
                    assert path.name == "slack_analysis.py"
                    assert [a.name for a in node.names] == ["get_principal"]
