"""Helper the sync builder uses for Codex agent roles copied from a repository.

Codex loads custom agents from ~/.codex/agents/, not from a plugin's Claude-format `agents/` folder. Roles copied
from a repository get their project-local paths pointed at the installed skill. Each role's header names this file.
"""
from pathlib import Path
import re


def rewrite(text, rules, skill, local=None):
    # Paths point at the installed skill. `local` is a copy of it to check them in before it is installed.
    for old, new in rules:
        text = text.replace(old, new.format(skill=skill))
    for old, _ in rules:
        if old in text:
            raise ValueError(f'Unrewritten install path: {old}')
    for path in re.findall(re.escape(str(skill)) + r'/[^\s`)\'"]+', text):
        if not (Path(local) / path[len(str(skill)) + 1:] if local else Path(path)).exists():
            raise ValueError(f'Rewritten path does not exist: {path}')
    return text
