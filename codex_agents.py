"""Helpers the sync builder uses to make Codex agent roles for plugins whose agents Codex does not load.

Codex loads custom agents from ~/.codex/agents/, not from a plugin's Claude-format `agents/` folder. Roles copied
from a repository get their project-local paths pointed at the installed skill; roles made from Claude-format agent
files are read-only unless the agent may edit files or fetch pages. Each role's header names this file.
"""
import json
from pathlib import Path
import re
import tomllib

EDITING_TOOLS = {'Edit', 'Write', 'MultiEdit', 'NotebookEdit'}
# Codex's read-only sandbox also blocks network access from shell commands, which page fetching needs.
NETWORK_TOOLS = {'WebFetch'}


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


def frontmatter(text):
    match = re.match(r'---\n(.*?)\n---\n', text, re.S)
    if not match:
        raise ValueError('Agent file has no frontmatter')
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(':')
        value = value.strip()
        fields[key.strip()] = json.loads(value) if value.startswith('"') else value
    return fields, text[match.end():].strip() + '\n'


def toml_role(fields, body, sandbox):
    lines = [f'name = {json.dumps(fields["name"])}', f'description = {json.dumps(fields["description"])}']
    if sandbox:
        lines.append(f'sandbox_mode = {json.dumps(sandbox)}')
    if "'''" in body:
        raise ValueError(f'{fields["name"]} body cannot be a TOML literal string')
    text = '\n'.join(lines) + "\ndeveloper_instructions = '''\n" + body + "'''\n"
    role = tomllib.loads(text)
    if role['developer_instructions'] != body or role['name'] != fields['name']:
        raise ValueError(f'{fields["name"]} did not round-trip through TOML')
    return text
