"""Freeze pre-refactor policy and keep command-name lists centralized."""

import ast
from pathlib import Path
from typing import get_args

from mica.agents.analysis import command_intent, commands

BASELINE = {'recipient': ['add-owner', 'remove-owner', 'share', 'unshare'],
 'registry': ['add-owner',
              'data-root',
              'owners',
              'projects',
              'publish',
              'remove-owner',
              'rename',
              'repos',
              'repos add',
              'repos remove',
              'share',
              'unpublish',
              'unshare',
              'use',
              'bind channel'],
 'email_read_only': ['add-owner',
                     'data-root',
                     'publish',
                     'remove-owner',
                     'rename',
                     'repos add',
                     'repos remove',
                     'share',
                     'unpublish',
                     'unshare',
                     'write grant',
                     'write revoke',
                     'write status',
                     'bind channel'],
 'sender_reply': ['atomx login status',
                  'connect atomx',
                  'forget atomx login',
                  'help',
                  'owners',
                  'projects',
                  'replace atomx login',
                  'scripts',
                  'use'],
 'connection': ['atomx login status',
                'connect atomx',
                'forget atomx login',
                'replace atomx login'],
 'transfer': ['atomx login status',
              'cancel transfer',
              'forget atomx login',
              'replace atomx login',
              'transfer',
              'transfers'],
 'dm_only': ['import session',
             'add-owner',
             'atomx login status',
             'connect atomx',
             'data-root',
             'init',
             'owners',
             'projects',
             'publish',
             'remove-owner',
             'rename',
             'repos add',
             'repos remove',
             'share',
             'unpublish',
             'unshare',
             'use',
             'write grant',
             'write revoke',
             'write status',
             'bind channel',
             'forget atomx login',
             'replace atomx login'],
 'approval': ['add-owner',
              'data-root',
              'publish',
              'remove-owner',
              'rename',
              'repos add',
              'repos remove',
              'share',
              'unpublish',
              'unshare',
              'write grant',
              'write revoke',
              'bind channel'],
 'exact_approval': ['add-owner', 'data-root', 'remove-owner', 'repos add', 'repos remove'],
 'natural': ['import session',
             'add-owner',
             'atomx login status',
             'brief',
             'cancel',
             'connect atomx',
             'data-root',
             'help',
             'init',
             'memory',
             'owners',
             'projects',
             'publish',
             'remove-owner',
             'rename',
             'repos',
             'repos add',
             'repos remove',
             'scripts',
             'share',
             'transfers',
             'unpublish',
             'unshare',
             'use',
             'write grant',
             'write revoke',
             'write status',
             'bind channel',
             'forget atomx login',
             'replace atomx login',
             'cancel transfer'],
 'exact_dm_only': ['add-owner',
                   'data-root',
                   'owners',
                   'projects',
                   'publish',
                   'remove-owner',
                   'rename',
                   'repos',
                   'repos add',
                   'repos remove',
                   'share',
                   'unpublish',
                   'unshare',
                   'bind channel'],
 'handoff': ['init',
             'import session',
             'cancel',
             'cancel transfer',
             'connect atomx',
             'forget atomx login',
             'replace atomx login'],
 'mutating': ['use', 'scripts'],
 'channel': ['bind channel', 'publish', 'unpublish']}

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
