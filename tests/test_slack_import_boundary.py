"""Keep the Claude Slack runtime independent of the shared task-service agent stack."""

import ast
import subprocess
import sys
from pathlib import Path


def test_slack_modules_do_not_import_browser_memory_or_services():
    root = Path(__file__).resolve().parents[1] / "src/mica"
    files = [*(root / "agents/analysis").glob("*.py"), root / "api/slack_analysis.py"]
    forbidden = (
        "mica.services",
        "mica.agents.memory",
        "mica.agents.context",
        "mica.agents.harness",
        "mica.agents.session",
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
                if module == "mica.api.auth":
                    assert path.name == "slack_analysis.py"
                    assert [a.name for a in node.names] == ["get_principal"]


def test_worker_entrypoints_and_release_gate_import_without_fastapi(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = """
import importlib
import importlib.abc
import runpy
import sys
from pathlib import Path

assert 'fastapi' not in sys.modules

class WithoutFastAPI(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'fastapi' or fullname.startswith('fastapi.'):
            raise ModuleNotFoundError('FastAPI is not installed in the worker', name=fullname)

sys.meta_path.insert(0, WithoutFastAPI())
try:
    importlib.import_module('fastapi')
except ModuleNotFoundError:
    pass
else:
    raise AssertionError('FastAPI dependency exclusion is ineffective')

root = Path(sys.argv[1])
sys.path.insert(0, str(root / 'src'))
for module in ('batch_worker', 'cli', 'atomx_worker'):
    importlib.import_module('mica.agents.analysis.' + module)
runpy.run_path(str(root / 'scripts/check_slack_release.py'), run_name='worker_import_gate')
assert 'fastapi' not in sys.modules
print('Worker entrypoints and release gate import without FastAPI')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(root)], cwd=tmp_path,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
