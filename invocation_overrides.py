#!/usr/bin/env python3
"""Apply recorded user invocation preferences (implicit or explicit-only) and harness adaptations without modifying upstream releases.

`run -- COMMAND...` exposes pristine caches to existing update/hash checks and
reapplies the preferences in a finally block. Original bytes remain recoverable.
Harness adaptations replay each plugin's adaptation repository onto the installed
cache with a three-way merge, so upstream edits merge or stop for review.
Tool notes name the other host's equivalent after a host-specific tool name.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import tempfile

import yaml

from portable import GIT, claude_home, codex_cli, codex_home, exclusive_lock, read_utf8, replace, write_utf8

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
REGISTRY = ROOT / 'invocation-overrides.json'
ADAPTATIONS = ROOT / 'harness-adaptations.json'
STATE_DIR = codex_home(HOME) / 'skill-updates/invocation-overrides'


def read_json(path, default=None):
    return json.loads(read_utf8(path)) if path.exists() else default


def read_text(path):
    return path.read_bytes().decode('utf-8') if path.is_file() else None


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest() if value is not None else None


def atomic_write(path, value):
    if value is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.override-{os.getpid()}')
    try:
        write_utf8(temporary, value)
        if path.exists():
            temporary.chmod(path.stat().st_mode & 0o777)
        replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def frontmatter(text):
    match = re.match(r'\A---\r?\n(.*?)\r?\n---(?=\r?\n|\Z)', text, re.S)
    if not match:
        raise ValueError('Missing skill frontmatter')
    metadata = yaml.safe_load(match[1])
    if not isinstance(metadata, dict):
        raise ValueError('Skill frontmatter must be a mapping')
    return match, metadata


def replace_field(header, key, value):
    # Replace only the selected top-level YAML field, including folded scalars.
    pattern = rf'(?m)^{re.escape(key)}:[^\n]*(?:\n(?:[ \t]+[^\n]*|))*(?=\n[^ \t\n]|\Z)'
    replaced, count = re.subn(pattern, lambda _: key + ': ' + json.dumps(value, ensure_ascii=False), header)
    if count != 1:
        raise ValueError(f'Expected exactly one {key} field')
    return replaced


def transform_skill(text, qualified, registry, selected=True, explicit=False):
    match, metadata = frontmatter(text)
    header = match[1]
    expected = dict(metadata)
    if explicit and metadata.get('disable-model-invocation') is not True:
        if 'disable-model-invocation' in metadata:
            header = replace_field(header, 'disable-model-invocation', True)
        else:
            header += '\ndisable-model-invocation: true'
        expected['disable-model-invocation'] = True
    elif selected and not explicit and metadata.get('disable-model-invocation') is True:
        header = replace_field(header, 'disable-model-invocation', False)
        expected['disable-model-invocation'] = False
    rule = registry.get('descriptionReplacements', {}).get(qualified)
    if rule:
        description = metadata.get('description')
        if description == rule['from']:
            header = replace_field(header, 'description', rule['to'])
            expected['description'] = rule['to']
        elif description != rule['to'] and re.search(r'explicit|by name', description or '', re.I):
            raise ValueError(f'Review changed invocation description: {qualified}')
        # A new upstream task-based description supersedes the old exact patch.
    if yaml.safe_load(header) != expected:
        raise ValueError(f'Unexpected metadata change: {qualified}')
    output = text[:match.start(1)] + header + text[match.end(1):]
    for rule in registry.get('textReplacements', {}).get(qualified, []):
        if rule['from'] in output:
            if output.count(rule['from']) != 1:
                raise ValueError(f'Ambiguous text replacement: {qualified}')
            output = output.replace(rule['from'], rule['to'], 1)
        elif rule['to'] not in output:
            raise ValueError(f'Review changed companion wording: {qualified}')
    return output


def transform_policy(text, allow=True):
    before = yaml.safe_load(text or '') or {}
    if not isinstance(before, dict):
        raise ValueError('Skill sidecar must be a mapping')
    expected = json.loads(json.dumps(before))
    expected.setdefault('policy', {})['allow_implicit_invocation'] = allow
    if before == expected:
        return text
    output = text or ''
    value = 'true' if allow else 'false'
    if 'allow_implicit_invocation' in before.get('policy', {}):
        output, count = re.subn(r'(?m)^(\s+allow_implicit_invocation:\s*)(?:true|false)\b', rf'\g<1>{value}', output)
        if count != 1:
            raise ValueError('Unexpected implicit invocation policy encoding')
    elif 'policy' in before:
        output, count = re.subn(r'(?m)^policy:\s*$', f'policy:\n  allow_implicit_invocation: {value}', output)
        if count != 1:
            raise ValueError('Unexpected policy mapping encoding')
    else:
        output = output.rstrip('\n') + ('\n' if output else '') + f'policy:\n  allow_implicit_invocation: {value}\n'
    if yaml.safe_load(output) != expected:
        raise ValueError('Unexpected sidecar change')
    return output


def git_text(repository, ref, relative):
    result = subprocess.run([*GIT, '-C', str(repository), 'show', f'{ref}:{relative}'], capture_output=True, check=True)
    return result.stdout.decode('utf-8')


def git_line(repository, *arguments):
    return subprocess.run([*GIT, '-C', str(repository), *arguments], capture_output=True, check=True).stdout.decode().strip()


def adaptation_files(adaptation):
    # Each path the adapted ref changed since it last absorbed upstream, as (path, base, adapted).
    # Diffing from the merge base keeps a newer upstream commit from reading as a revert.
    repository = Path(adaptation['repository'])
    adapted_ref = git_line(repository, 'rev-parse', '--verify', adaptation['adaptedRef'] + '^{commit}')
    base_ref = git_line(repository, 'merge-base', adaptation['baseRef'], adapted_ref)
    listing = subprocess.run([*GIT, '-C', str(repository), 'diff', '--no-renames', '--name-status', '-z', base_ref, adapted_ref],
                             capture_output=True, check=True).stdout.decode('utf-8').split('\0')
    files = []
    for status, relative in zip(listing[0::2], listing[1::2]):
        if Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise ValueError(f'Adaptation path escapes the plugin: {relative}')
        base = None if status == 'A' else git_text(repository, base_ref, relative)
        adapted = None if status == 'D' else git_text(repository, adapted_ref, relative)
        # Skill discovery reads the installed tree, so an adaptation must keep the skill set fixed.
        if Path(relative).name == 'SKILL.md' and (base is None or adapted is None
                or frontmatter(base)[1].get('name') != frontmatter(adapted)[1].get('name')):
            raise ValueError(f'Adaptations may not add, remove or rename a skill: {relative}')
        files.append((relative, base, adapted))
    return files


def adapted_here(adaptations, identity, host):
    adaptation = adaptations['plugins'].get(identity.split('@')[0])
    if adaptation and host in adaptation['hosts'] and identity in adaptation.get('identities', [identity]):
        return adaptation
    return None


def merge_adaptation(current, base, adapted, label):
    if current == adapted:
        return current
    if current == base:
        return adapted
    if current is None or base is None or adapted is None:
        raise ValueError(f'Review upstream change to an adapted file: {label}')
    with tempfile.TemporaryDirectory(prefix='harness-merge-') as folder:
        paths = [Path(folder) / name for name in ('current', 'base', 'adapted')]
        for path, value in zip(paths, (current, base, adapted)):
            write_utf8(path, value)
        result = subprocess.run([*GIT, 'merge-file', '-p', *map(str, paths)], capture_output=True)
    if result.returncode:
        raise ValueError(f'Review harness adaptation conflict after an upstream change: {label}')
    return result.stdout.decode('utf-8')


# Names only Cursor understands, as cursor/plugins' scripts/generate-pstack-harness-trees.mjs lists them.
CURSOR_NAMES = re.compile('|'.join((
    r'`Task`|\bTask (?:tool|subagent|call)', 'AskQuestion', r'(?i:\btodo ?list\b)', 'generalPurpose', 'poteto-agent',
    'comment-sicko', r'readonly: (?:true|false)|readonly/Ask', r'environment: "(?:cloud|local)"', 'cloud_base_branch',
    'run_in_background', '`resume`', 'file_attachments', r'\bclaude-[a-z]+-\d', r'\bgrok-\d', r'\bgpt-\d',
    'inherit-parent', r'pstack-models\.mdc', r'\.cursor/skills', r'\.cursor/plugins|\.cursor-plugin',
    r'\.cursor/rules|always-applied rule', r'agent-transcripts|\.cursor/projects',
    r'Bugbot|Cursor [Aa]utomation|cursor\.(?:sh|com)/automations|[Cc]loud [Aa]gent|built-in PR tool')))
MARKDOWN_LINK = re.compile(r'\]\(([^)\s]+)\)')


def harness_gaps(root, harness):
    # Skills that still use a Cursor name, or name another skill to run, without linking the harness reference
    # that says what those mean in Codex and Claude Code.
    root = Path(root)
    skills = {path.parent.name: path for path in (root / 'skills').glob('*/SKILL.md')}
    gaps = []
    for name, skill in sorted(skills.items()):
        others = '|'.join(re.escape(other) for other in skills if other != name)
        route = re.compile(rf'the \*\*({others})\*\* skill|`/({others})`')
        texts = [read_utf8(path) for path in skill.parent.rglob('*.md')]
        if not any(CURSOR_NAMES.search(text) or any(f'(../{match.group(1) or match.group(2)}/' not in line
                                                    for line in text.splitlines() for match in route.finditer(line))
                   for text in texts):
            continue
        folder = skill.parent.relative_to(root).as_posix()
        if harness not in {posixpath.normpath(posixpath.join(folder, href.split('#')[0]))
                           for href in MARKDOWN_LINK.findall(read_utf8(skill))}:
            gaps.append(name)
    return gaps


def noted_here(adaptations, identity):
    # Tool notes apply to every host's install of a listed plugin, except copies built from a noted cache.
    notes = adaptations.get('toolNotes')
    name, marketplace = identity.split('@', 1)
    if notes and name in notes['plugins'] and marketplace not in notes.get('inheritingMarketplaces', []):
        return notes['rules']
    return None


def add_tool_notes(text, rules):
    if text is None:
        return text
    match = re.match(r'\A---\r?\n.*?\r?\n---\r?\n', text, re.S)
    start = match.end() if match else 0
    body = text[start:]
    for rule in rules:
        # Atomic, so an optional suffix cannot backtrack around an existing note.
        body = re.sub('(?>' + rule['match'] + ')(?!' + re.escape(rule['note']) + ')', lambda found, note=rule['note']: found[0] + note, body)
    return text[:start] + body


def note_files(root):
    folders = {path.parent for path in skill_paths(root).values()}
    return sorted({path for folder in folders for path in folder.rglob('*.md')
                   if 'node_modules' not in path.relative_to(folder).parts})


def unnoted_tool_names(adaptations):
    # Report host-specific names in installed Codex plugins that no tool-note rule covers yet.
    notes = adaptations.get('toolNotes')
    if not notes:
        return {}
    pattern = re.compile('|'.join(rule['match'] for rule in notes['rules']))
    skipped = set(notes['plugins']) | set(notes.get('auditExclusions', {}))
    inventory = json.loads(subprocess.check_output([codex_cli(), 'plugin', 'list', '--json'], text=True))
    found = {}
    for plugin in inventory['installed']:
        if not plugin.get('enabled') or plugin['name'] in skipped:
            continue
        root = codex_home(HOME) / 'plugins/cache' / plugin['marketplaceName'] / plugin['name'] / plugin['version']
        if not root.is_dir():
            continue
        try:
            paths = note_files(root)
        except ValueError:
            continue
        count = 0
        for path in paths:
            text = read_text(path) or ''
            match = re.match(r'\A---\r?\n.*?\r?\n---\r?\n', text, re.S)
            count += len(pattern.findall(text[match.end() if match else 0:]))
        if count:
            found[plugin['pluginId']] = count
    return found


def collect_skills(root):
    if not root.is_dir():
        return
    if (root / 'SKILL.md').is_file():
        yield root / 'SKILL.md'
        return
    for child in sorted(root.iterdir()):
        if child.is_dir() and child.name not in {'.git', 'node_modules', 'references', 'examples', 'templates', 'tests', '__pycache__'}:
            yield from collect_skills(child)


def skill_paths(root):
    if (root / 'SKILL.md').is_file():
        _, metadata = frontmatter(read_text(root / 'SKILL.md'))
        return {metadata.get('name', root.name): root / 'SKILL.md'}
    paths = ['./skills/']
    for relative in ['plugin.json', '.codex-plugin/plugin.json', '.claude-plugin/plugin.json']:
        manifest = root / relative
        if manifest.is_file():
            data = read_json(manifest)
            paths = ['./skills/'] if relative == 'plugin.json' else data.get('skills') or ['./skills/']
            break
    if isinstance(paths, str):
        paths = [paths]
    found = {}
    for relative in paths:
        folder = (root / relative).resolve()
        if not folder.is_relative_to(root.resolve()):
            raise ValueError(f'Skill path escapes plugin: {folder}')
        for path in collect_skills(folder):
            _, metadata = frontmatter(read_utf8(path))
            name = metadata.get('name', path.parent.name)
            if name in found and found[name] != path:
                raise ValueError(f'Duplicate exported skill: {name}')
            found[name] = path
    return found


def targeted(registry, host):
    # Plugins with implicit (`targets`) or explicit-only (`explicitTargets`) selections on this host.
    return registry['targets'][host].keys() | registry.get('explicitTargets', {}).get(host, {}).keys()


def installations(registry, plugins=(), adaptations=None):
    adaptations = adaptations or {'plugins': {}}
    inventory = json.loads(subprocess.check_output([codex_cli(), 'plugin', 'list', '--json'], text=True))
    selected = set(plugins)
    found = []
    for plugin in inventory['installed']:
        identity = plugin['pluginId']
        wanted = (identity in targeted(registry, 'Codex') or adapted_here(adaptations, identity, 'Codex')
                  or noted_here(adaptations, identity))
        if wanted and (not selected or plugin['name'] in selected):
            root = codex_home(HOME) / 'plugins/cache' / plugin['marketplaceName'] / plugin['name'] / plugin['version']
            found.append(('Codex', identity, root))
    claude = read_json(claude_home(HOME) / 'plugins/installed_plugins.json', {'plugins': {}})
    for identity, records in claude['plugins'].items():
        wanted = (identity in targeted(registry, 'Claude') or adapted_here(adaptations, identity, 'Claude')
                  or noted_here(adaptations, identity))
        if wanted and (not selected or identity.split('@')[0] in selected):
            for record in records:
                root = Path(record['installPath'])
                if not root.resolve().is_relative_to(claude_home(HOME) / 'plugins/cache'):
                    raise ValueError(f'Unexpected Claude cache root: {root}')
                found.append(('Claude', identity, root))
    for host, roots in registry.get('standaloneRoots', {}).items():
        allowed = {'Codex': codex_home(HOME) / 'skills', 'Claude': claude_home(HOME) / 'skills'}[host].resolve()
        for identity, directory in roots.items():
            if selected and identity.split('@')[0] not in selected:
                continue
            root = Path(directory).resolve()
            if not root.is_relative_to(allowed):
                raise ValueError(f'Standalone override escapes the skill root: {root}')
            if identity not in targeted(registry, host):
                raise ValueError(f'Standalone override has no selected skills: {identity}')
            # A missing skill is an uninstall, not permission to recreate it.
            if (root / 'SKILL.md').is_file():
                found.append((host, identity, root))
    return found


def plan(registry, installs, overlay=None, adaptations=None):
    adaptations = adaptations or {'plugins': {}}
    pristine = overlay.pristine if overlay else (lambda path, text: text)
    records = overlay.state['files'] if overlay else {}
    writes = []
    counts = {'Codex': 0, 'Claude': 0}
    for host, identity, root in installs:
        plugin = identity.split('@')[0]
        staged = {}

        def stage(path, transform):
            # Transforms always start from the recorded original bytes, so replays are deterministic.
            path = path.resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError(f'Overlay path escapes the plugin: {path}')
            if path not in staged:
                old = read_text(path)
                staged[path] = [old, pristine(path, old)]
            staged[path][1] = transform(staged[path][1])

        adaptation = adapted_here(adaptations, identity, host)
        if adaptation:
            for relative, base, adapted in adaptation_files(adaptation):
                label = f'{identity}:{relative}'
                stage(root / relative, lambda text, base=base, adapted=adapted, label=label:
                      merge_adaptation(text, base, adapted, label))
        rules = noted_here(adaptations, identity)
        if rules:
            for path in note_files(root):
                stage(path, lambda text, rules=rules: add_tool_notes(text, rules))
        available = skill_paths(root)
        selected = set(registry['targets'][host].get(identity, []))
        explicit = set(registry.get('explicitTargets', {}).get(host, {}).get(identity, []))
        if selected & explicit:
            raise ValueError(f'Skills listed as both implicit and explicit-only in {identity}: {sorted(selected & explicit)}')
        companions = {key.split(':', 1)[1] for key in registry.get('textReplacements', {}) if key.startswith(plugin + ':')}
        missing = (selected | explicit | companions) - available.keys()
        if missing:
            raise ValueError(f'Review missing selected skills in {identity}: {sorted(missing)}')
        for name in sorted(selected | explicit | companions):
            path = available[name]
            qualified = plugin + ':' + name
            stage(path, lambda text, qualified=qualified, chosen=name in selected, forced=name in explicit:
                  transform_skill(text, qualified, registry, selected=chosen, explicit=forced))
            if name in selected or name in explicit:
                counts[host] += 1
                policy = path.parent / 'agents/openai.yaml'
                if host == 'Codex' or policy.exists():
                    stage(policy, lambda text, allow=name in selected: transform_policy(text, allow))
        # A recorded file that no current transform claims returns to its original bytes.
        for filename in records:
            path = Path(filename)
            if path.resolve().is_relative_to(root.resolve()) and path.resolve() not in staged:
                stage(path, lambda text: text)
        writes.extend((path, old, new, host, identity) for path, (old, new) in staged.items())
    return writes, counts


class Overlay:
    def __init__(self, state_dir=STATE_DIR):
        self.directory = state_dir
        self.path = state_dir / 'state.json'
        self.state = read_json(self.path, {'schemaVersion': 1, 'files': {}})

    def save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        if self.path.exists():
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            archive = self.directory / 'history' / (stamp + '.json')
            atomic_write(archive, read_utf8(self.path))
        atomic_write(self.path, json.dumps(self.state, indent=2, ensure_ascii=False) + '\n')
        self.path.chmod(0o600)

    def known(self, path):
        # Bytes this overlay wrote or may have left behind: the original, the target, and the prior target.
        record = self.state['files'].get(str(path))
        return (record['base'], record['effective'], record.get('previous')) if record else ()

    def pristine(self, path, current):
        record = self.state['files'].get(str(path))
        overlaid = record and current is not None and current in self.known(path)[1:]
        return record['base'] if overlaid else current

    def apply(self, writes, *, check=False, refreshed=False):
        changed = []
        for path, old, new, host, identity in writes:
            record = self.state['files'].get(str(path))
            if record and old not in self.known(path) and not refreshed:
                raise ValueError(f'Unrecognized local edit; preserve and review: {path}')
            if old == new:
                continue
            changed.append((path, old, new))
            if not check:
                # Replaying an existing overlay retains its original recovery bytes. Until every
                # file is rewritten, the prior target stays recognised so a retry or restore works.
                base = self.pristine(path, old)
                entry = {'host': host, 'pluginId': identity,
                    'base': base, 'effective': new, 'baseSha256': digest(base), 'effectiveSha256': digest(new)}
                if record and record['effective'] != new:
                    entry['previous'] = record['effective']
                self.state['files'][str(path)] = entry
        if check:
            return len(changed)
        if changed:
            # Write recovery state before touching live files; interrupted runs can replay.
            self.save()
        for path, old, new in changed:
            if read_text(path) != old:
                raise ValueError(f'Concurrent file change: {path}')
            atomic_write(path, new)
        return len(changed)

    def restore(self, installs):
        roots = [root.resolve() for _, _, root in installs]
        changes = []
        for filename, record in self.state['files'].items():
            path = Path(filename)
            if not any(path.resolve().is_relative_to(root) for root in roots):
                continue
            current = read_text(path)
            if current == record['base']:
                continue
            if current not in self.known(path)[1:]:
                raise ValueError(f'Unrecognized local edit; refusing to restore: {path}')
            changes.append((path, current, record['base']))
        for path, current, original in changes:
            if read_text(path) != current:
                raise ValueError(f'Concurrent file change: {path}')
            atomic_write(path, original)
        return len(changes)


def locked():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
    return exclusive_lock(STATE_DIR / 'lock')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['apply', 'check', 'restore', 'run'])
    parser.add_argument('--plugin', action='append', default=[], help='Limit to one plugin name, across installed hosts; repeat as needed')
    # Split explicitly so argparse never interprets arguments for the child command.
    argv = sys.argv[1:]
    command = []
    if '--' in argv:
        split = argv.index('--')
        argv, command = argv[:split], argv[split + 1:]
    args = parser.parse_args(argv)
    if (args.action == 'run') != bool(command):
        parser.error('run requires -- followed by a command; other actions take no command')
    registry = read_json(REGISTRY)
    adaptations = read_json(ADAPTATIONS, {'plugins': {}})
    with locked():
        overlay = Overlay()
        installs = installations(registry, args.plugin, adaptations)
        if args.action in ['restore', 'run']:
            restored = overlay.restore(installs)
            if args.action == 'restore':
                print(json.dumps({'restoredFiles': restored}))
                return
            try:
                status = subprocess.run(command, check=False).returncode
            finally:
                # Re-discover after replacement, rollback, or an ordinary command failure.
                old_roots = [root.resolve() for _, _, root in installs]
                overlay.state['files'] = {path: record for path, record in overlay.state['files'].items()
                    if not any(Path(path).resolve().is_relative_to(root) for root in old_roots)}
                overlay.save()
                writes, counts = plan(registry, installations(registry, args.plugin, adaptations), overlay, adaptations)
                changed = overlay.apply(writes)
                print(json.dumps({'reappliedFiles': changed, 'skills': counts}))
            if status:
                raise SystemExit(status)
            return
        writes, counts = plan(registry, installs, overlay, adaptations)
        count = overlay.apply(writes, check=args.action == 'check')
        result = {'action': args.action, 'filesNeedingChange' if args.action == 'check' else 'changedFiles': count,
                  'skills': counts, 'recoveryState': str(overlay.path)}
        if args.action == 'check' and not args.plugin:
            result['unnotedToolNames'] = unnoted_tool_names(adaptations)
        print(json.dumps(result))
        if args.action == 'check' and count:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
