"""Freeze pre-refactor policy and keep command-name lists centralized."""

import ast
from pathlib import Path
from typing import get_args

from mica.agents.analysis import command_intent, commands

BASELINE = {
    "bare": [
        "atomx login status",
        "brief",
        "cancel",
        "clean brief",
        "connect atomx",
        "forget atomx login",
        "forget context",
        "help",
        "help all",
        "help write",
        "knowledge",
        "memory",
        "memory context",
        "projects",
        "replace atomx login",
        "save brief",
        "transfers",
        "undo knowledge",
    ],
    "recipient": ["add-owner", "remove-owner", "share", "unshare"],
    "removed": [
        "clean brief",
        "forget",
        "forget context",
        "forget knowledge",
        "knowledge",
        "knowledge github",
        "knowledge show",
        "memory context",
        "remember",
        "save brief",
        "undo knowledge",
    ],
    "registry": [
        "add-owner",
        "data-root",
        "owners",
        "projects",
        "publish",
        "remove-owner",
        "rename",
        "repos",
        "repos add",
        "repos remove",
        "share",
        "unpublish",
        "unshare",
        "use",
    ],
    "email_read_only": [
        "add-owner",
        "data-root",
        "help write",
        "publish",
        "remove-owner",
        "rename",
        "repos add",
        "repos remove",
        "share",
        "unpublish",
        "unshare",
        "write grant",
        "write revoke",
        "write status",
    ],
    "sender_reply": [
        "atomx login status",
        "connect atomx",
        "forget atomx login",
        "help",
        "help all",
        "owners",
        "projects",
        "replace atomx login",
        "scripts",
        "use",
    ],
    "connection": [
        "atomx login status",
        "connect atomx",
        "forget atomx login",
        "replace atomx login",
    ],
    "transfer": [
        "atomx login status",
        "cancel transfer",
        "forget atomx login",
        "replace atomx login",
        "transfer",
        "transfers",
    ],
    "dm_only": [
        "add-owner",
        "atomx login status",
        "connect atomx",
        "data-root",
        "init",
        "owners",
        "projects",
        "publish",
        "remove-owner",
        "rename",
        "repos add",
        "repos remove",
        "share",
        "unpublish",
        "unshare",
        "use",
        "write grant",
        "write revoke",
        "write status",
    ],
    "approval": [
        "add-owner",
        "data-root",
        "publish",
        "remove-owner",
        "rename",
        "repos add",
        "repos remove",
        "share",
        "unpublish",
        "unshare",
        "write grant",
        "write revoke",
    ],
    "exact_approval": ["add-owner", "data-root", "remove-owner", "repos add", "repos remove"],
    "natural": [
        "add-owner",
        "atomx login status",
        "brief",
        "cancel",
        "connect atomx",
        "data-root",
        "help",
        "init",
        "memory",
        "owners",
        "projects",
        "publish",
        "remove-owner",
        "rename",
        "repos",
        "repos add",
        "repos remove",
        "scripts",
        "share",
        "transfers",
        "unpublish",
        "unshare",
        "use",
        "write grant",
        "write revoke",
        "write status",
    ],
    "exact_dm_only": [
        "add-owner",
        "data-root",
        "owners",
        "projects",
        "publish",
        "remove-owner",
        "rename",
        "repos",
        "repos add",
        "repos remove",
        "share",
        "unpublish",
        "unshare",
    ],
}


def test_policy_baseline():
    for field, expected in BASELINE.items():
        assert {name for name, rule in commands.COMMANDS.items() if getattr(rule, field)} == set(
            expected
        )
    assert set(get_args(command_intent.Operation)) == {
        name for name, rule in commands.COMMANDS.items() if rule.natural
    }


def test_no_duplicate_command_lists():
    root = Path(commands.__file__).parents[2]
    for path in root.rglob("*.py"):
        if path == Path(commands.__file__):
            continue
        tree = ast.parse(path.read_text())
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
                parent = parents.get(node)
                # Typed schema enums remain independent; Operation is cross-checked above.
                if isinstance(parent, ast.Subscript) and ast.unparse(parent.value) == "Literal":
                    continue
                names = {
                    v.value
                    for v in node.elts
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                }
                assert len(names & commands.COMMANDS.keys()) < 3, (path.name, node.lineno)
