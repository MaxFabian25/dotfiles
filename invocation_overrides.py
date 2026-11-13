"""Plan the file writes that give a plugin tree its invocation preferences (implicit or explicit-only), harness
adaptations, tool notes, sibling skill links and Windows-only hook and launcher edits. Sync applies them to its
build of a release, and refuses a SKILL.md whose `../` link to a SKILL.md does not resolve.
Harness adaptations replay each plugin's adaptation repository onto the tree with a three-way merge, so
upstream edits merge or stop for review.
Tool notes name the other host's equivalent after a host-specific tool name.
Text replacements, on every platform or only on Windows, patch one exact passage of a file and stop for review when
upstream changes that passage.
"""
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import tempfile

import yaml

from portable import GIT, read_utf8, write_utf8


def read_json(path, default=None):
    return json.loads(read_utf8(path)) if path.exists() else default


def read_text(path):
    return path.read_bytes().decode('utf-8') if path.is_file() else None


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
    crlf_copy = current and '\r\n' in current and '\n' not in current.replace('\r\n', '')
    if not crlf_copy or any(part and '\r\n' in part for part in (base, adapted)):
        return _merge_lf(current, base, adapted, label)
    text, base, adapted = (None if part is None else part.replace('\r\n', '\n') for part in (current, base, adapted))
    merged = _merge_lf(text, base, adapted, label)
    return None if merged is None else merged.replace('\n', '\r\n')


def _merge_lf(current, base, adapted, label):
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


def add_windows_hook_command(text, command):
    # Codex on Windows reads a hook's commandWindows and ignores the `windows` key other hosts read. An upstream
    # manifest that sets commandWindows itself keeps its own, because that is the fix this overlay stands in for.
    if text is None:
        return text
    manifest = json.loads(text)
    for groups in manifest['hooks'].values():
        for group in groups:
            for handler in group['hooks']:
                if handler.get('type') == 'command':
                    handler.setdefault('commandWindows', command)
    return json.dumps(manifest, indent=2, ensure_ascii=False) + '\n'


def windows_overlay(hook_command, replaces):
    # The Windows-only Codex edits a caller adds to its adaptations for this plugin, or None when it declares none.
    if not hook_command and not replaces:
        return None
    return {'hookCommand': hook_command, 'replace': [(r.path, r.find, r.replace) for r in replaces]}


def replace_once(text, find, replacement, label):
    # A patch to one passage of an upstream file. An upstream that already reads as the patch needs none, and one
    # that changed, moved or removed the passage stops for review.
    if text is None:
        raise ValueError(f'Review upstream change to an adapted file: {label}')
    newline = '\r\n' if '\r\n' in text else '\n'
    find = find.replace('\r\n', '\n').replace('\n', newline)
    replacement = replacement.replace('\r\n', '\n').replace('\n', newline)
    if replacement in text:
        return text
    if text.count(find) != 1:
        raise ValueError(f'Review upstream change to an adapted file: {label}')
    return text.replace(find, replacement)


# A relative path to a skill's SKILL.md that starts at ../, so the ../ inside a URL or a longer path is not one.
SKILL_LINK = re.compile(r'(?<![\w./-])\.\./(?:[\w.-]+/)*SKILL\.md')


def relink_skills(text, path, skills):
    # Upstream links a sibling skill as ../NAME/SKILL.md, which resolves where skills are installed side by side.
    # A plugin that keeps them in category folders gets the path to that skill's SKILL.md instead.
    def relink(found):
        name = found[0].removeprefix('../').removesuffix('/SKILL.md')
        if name not in skills or (path.parent / found[0]).is_file():
            return found[0]
        return Path(os.path.relpath(skills[name], path.parent)).as_posix()
    return SKILL_LINK.sub(relink, text)


def check_skill_links(identity, root, path, text):
    if broken := sorted({link for link in SKILL_LINK.findall(text) if not (path.parent / link).is_file()}):
        relative = path.resolve().relative_to(Path(root).resolve()).as_posix()
        raise ValueError(f'{identity}: {relative} links to {", ".join(broken)}, which the built plugin does not have')


def note_files(root):
    folders = {path.parent for path in skill_paths(root).values()}
    return sorted({path for folder in folders for path in folder.rglob('*.md')
                   if 'node_modules' not in path.relative_to(folder).parts})


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
            paths = data.get('skills') or ['./skills/']
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


def exported_policies(root):
    # A policy export writes invocation flags before the adaptation merges, so the adaptation's base and
    # adapted copies get the same flags; otherwise an adapted line beside a flag reads as a conflict.
    receipt = root / '.dotfiles-policy.json'
    if not receipt.is_file():
        return {}
    exported = json.loads(receipt.read_bytes())
    paths = skill_paths(root)
    policies = {}
    for allow, names in ((True, exported.get('implicit', [])), (False, exported.get('explicit', []))):
        for name in names:
            folder = paths[name].parent.relative_to(root.resolve())
            policies[(folder / 'agents/openai.yaml').as_posix()] = lambda text, allow=allow: transform_policy(text, allow)
            if allow:
                policies[(folder / 'SKILL.md').as_posix()] = lambda text, name=name: transform_skill(text, name, {})
    return policies


def plan(registry, installs, adaptations=None):
    adaptations = adaptations or {'plugins': {}}
    writes = []
    counts = {'Codex': 0, 'Claude': 0}
    for host, identity, root in installs:
        plugin = identity.split('@')[0]
        staged = {}

        def stage(path, transform):
            path = path.resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError(f'Overlay path escapes the plugin: {path}')
            if path not in staged:
                old = read_text(path)
                staged[path] = [old, old]
            staged[path][1] = transform(staged[path][1])

        adaptation = adapted_here(adaptations, identity, host)
        if adaptation:
            exported = exported_policies(root)
            for relative, base, adapted in adaptation_files(adaptation):
                if policy := exported.get(relative):
                    base, adapted = (None if text is None else policy(text) for text in (base, adapted))
                label = f'{identity}:{relative}'
                stage(root / relative, lambda text, base=base, adapted=adapted, label=label:
                      merge_adaptation(text, base, adapted, label))
        windows = adaptations.get('windows', {}).get(plugin, {})
        if command := windows.get('hookCommand'):
            stage(root / 'hooks/hooks.json', lambda text, command=command: add_windows_hook_command(text, command))
        for relative, find, replacement in (*adaptations.get('replace', {}).get(plugin, ()), *windows.get('replace', ())):
            stage(root / relative, lambda text, find=find, replacement=replacement, label=f'{identity}:{relative}':
                  replace_once(text, find, replacement, label))
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
        for path in available.values():
            text = staged[path.resolve()][1] if path.resolve() in staged else read_text(path)
            relinked = relink_skills(text, path, available)
            if relinked != text:
                stage(path, lambda _, relinked=relinked: relinked)
            check_skill_links(identity, root, path, relinked)
        writes.extend((path, old, new, host, identity) for path, (old, new) in staged.items())
    return writes, counts

