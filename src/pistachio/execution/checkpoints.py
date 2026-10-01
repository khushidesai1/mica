"""Explicit checkpoint identities; dependency changes conservatively invalidate reuse."""

from __future__ import annotations

import ast
from collections.abc import Mapping

from pistachio.execution.workspace import digest
from pistachio.models import CheckpointSpec


def checkpoint_key(
    spec: CheckpointSpec,
    files: dict[str, str],
    parameters: Mapping[str, object],
    input_hash: str,
    runtime_image: str,
) -> str:
    dependencies = set(spec.source_paths)
    pending = list(dependencies)
    while pending:
        path = pending.pop()
        if path not in files:
            raise ValueError(f"checkpoint producer is missing: {path}")
        tree = ast.parse(files[path], filename=path)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                if isinstance(node, ast.ImportFrom) and node.level:
                    # Relative imports are valid; conservatively include every source module.
                    modules = [
                        p.removesuffix(".py").replace("/", ".") for p in files if p.endswith(".py")
                    ]
                for module in modules:
                    candidates = [
                        module.replace(".", "/") + ".py",
                        module.replace(".", "/") + "/__init__.py",
                    ]
                    for candidate in candidates:
                        if candidate in files and candidate not in dependencies:
                            dependencies.add(candidate)
                            pending.append(candidate)
            if isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name)
                and node.func.id in {"eval", "exec", "__import__"}
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
            ):
                # Dynamic dependencies cannot be safely inferred.
                dependencies.update(files)
    return digest(
        {
            "schema": spec.model_dump(mode="json"),
            "runtime": runtime_image,
            "inputs": input_hash,
            # Include all settings until more precise dependencies are proven.
            "parameters": parameters,
            "source": {p: files[p] for p in sorted(dependencies)},
        }
    )


# Mounted read-only in the analysis sandbox. Checkpoints are private JSON values with explicit
# output schemas, not executable objects. The runner validates and publishes them independently.
CHECKPOINT_HELPER = '''"""Task-scoped checkpoint I/O; existing inputs are read-only."""
import json
import os
from pathlib import Path

def load(checkpoint_id):
    config = json.loads(Path(os.environ['PISTACHIO_CONFIG']).read_text())
    allowed = config['checkpoint_keys']
    if checkpoint_id not in allowed:
        raise ValueError('undeclared checkpoint')
    path = Path(os.environ['PISTACHIO_CHECKPOINT_INPUTS']) / (checkpoint_id + '.json')
    return json.loads(path.read_text()) if path.exists() else None

def save(checkpoint_id, value):
    config = json.loads(Path(os.environ['PISTACHIO_CONFIG']).read_text())
    allowed = config['checkpoint_keys']
    if checkpoint_id not in allowed:
        raise ValueError('undeclared checkpoint')
    root = Path(os.environ['PISTACHIO_CHECKPOINT_OUTPUTS'])
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / (checkpoint_id + '.partial')
    with temporary.open('w') as stream:
        json.dump(value, stream, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, root / (checkpoint_id + '.json'))
'''
