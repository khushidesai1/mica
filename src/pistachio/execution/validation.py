"""Static checks applied before generated analysis code is dispatched."""

from __future__ import annotations

import ast


class ScriptRejected(ValueError):
    pass


NETWORK_MODULES = {
    "boto3",
    "ctypes",
    "gcsfs",
    "google.cloud",
    "googleapiclient",
    "http.client",
    "httpx",
    "multiprocessing",
    "requests",
    "socket",
    "subprocess",
    "urllib",
}
FORBIDDEN_TEXT = (
    "git push",
    "git commit",
    "gh pr",
    "github.com/repos/",
    "subprocess.run(['git'",
    'subprocess.run(["git"',
)


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    values = (test.left, test.comparators[0])
    return any(isinstance(value, ast.Name) and value.id == "__name__" for value in values) and any(
        isinstance(value, ast.Constant) and value.value == "__main__" for value in values
    )


def validate_script(script: str, *, require_preflight: bool = False) -> None:
    if not script.strip():
        raise ScriptRejected("script is empty")
    try:
        tree = ast.parse(script)
    except SyntaxError as exc:
        raise ScriptRejected(f"script does not compile: {exc}") from exc
    lowered = script.lower()
    if not ast.get_docstring(tree):
        raise ScriptRejected("generated code requires a concise module docstring")
    argparse_imported = any(
        isinstance(node, ast.Import)
        and any(alias.name == "argparse" for alias in node.names)
        or isinstance(node, ast.ImportFrom)
        and node.module == "argparse"
        for node in tree.body
    )
    if not argparse_imported:
        raise ScriptRejected("generated code requires typed argparse CLI arguments")
    required_options = {"--input-dir", "--output-dir", "--seed"}
    declared_options = {
        argument.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        for argument in node.args
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
    }
    if not required_options <= declared_options:
        raise ScriptRejected("generated code is missing required typed CLI arguments")
    if require_preflight:
        if "--preflight" not in declared_options:
            raise ScriptRejected("generated code requires a --preflight real-data validation mode")
        if not any(isinstance(node, ast.If) and _is_main_guard(node) for node in tree.body):
            raise ScriptRejected("generated code requires an import-safe __main__ guard")
    if any(fragment in lowered for fragment in FORBIDDEN_TEXT):
        raise ScriptRejected("GitHub or Git write operations are forbidden")
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and node.func.attr.startswith(("system", "popen", "spawn", "exec", "fork"))
        ):
            raise ScriptRejected(f"process escape is forbidden: os.{node.func.attr}")
        if isinstance(node, ast.Import):
            names = {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names = {node.module or ""}
        else:
            continue
        if any(
            name in NETWORK_MODULES
            or any(name.startswith(prefix + ".") for prefix in NETWORK_MODULES)
            for name in names
        ):
            raise ScriptRejected(
                f"network module is not allowed in generated code: {sorted(names)}"
            )
