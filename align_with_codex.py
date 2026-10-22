#!/usr/bin/env python3
"""Mirror portable Codex plugins into a validated local Claude marketplace.

Moved from ~/.claude/bin/align-with-codex.py on 2026-10-14; that path forwards here.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib

import yaml

from portable import GIT, claude_cli, claude_home, codex_home, posix, read_utf8, replace, write_utf8

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
CATALOG = claude_home(HOME) / 'marketplaces/codex-aligned'
SOURCES = {}
# Agents to keep in a Claude copy, by plugin; others are removed. Plugins not listed keep all agents.
# cursor-team-kit is packaged for three skills pstack calls by name, not for its agents.
AGENT_SELECTION = {}
# Skills a Claude copy keeps although Codex has them off; their Claude flag still comes from the Codex sidecar.
# Like AGENT_SELECTION, this moves into the description in migration unit 7.
CLAUDE_SKILLS = {}
# Bump when the Claude adaptation changes, so unchanged Codex sources still rebuild.
ADAPTATION = 'claude-host-names-1'
_LEGACY = object()


def read_json(path):
    return json.loads(read_utf8(path))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write((json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode())
    temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
    replace(temporary, path)


def command(*args, cwd=None):
    result = subprocess.run([claude_cli(), *args], capture_output=True, text=True, timeout=60, cwd=cwd)
    if result.returncode:
        raise RuntimeError(f'claude {args}: {result.stdout}\n{result.stderr}')
    return result.stdout


def manifest(root):
    for relative in ('.codex-plugin/plugin.json', '.claude-plugin/plugin.json', 'plugin.json'):
        path = root / relative
        if path.exists():
            return read_json(path)
    raise RuntimeError(f'Missing plugin manifest: {root}')


def selected_skills(root, data, disabled, plugin, kept_skills=_LEGACY):
    kept = CLAUDE_SKILLS.get(plugin, ()) if kept_skills is _LEGACY else kept_skills
    paths = data.get('skills', ['./skills'])
    if isinstance(paths, str):
        paths = [paths]
    result = []
    for relative in paths:
        directory = (root / relative).resolve()
        if not directory.is_relative_to(root.resolve()):
            raise RuntimeError(f'Skill escapes plugin: {directory}')
        candidates = [directory / 'SKILL.md'] if (directory / 'SKILL.md').exists() else directory.rglob('SKILL.md')
        for path in candidates:
            front = yaml.safe_load(read_utf8(path).split('---', 2)[1])
            name = str(front.get('name', path.parent.name))
            if f'{plugin}:{name}' not in disabled or name in kept:
                result.append(path.relative_to(root))
    return sorted(set(result), key=lambda path: path.parts)


def excluded_skill_names(disabled, plugin, kept_skills=_LEGACY):
    kept = CLAUDE_SKILLS.get(plugin, ()) if kept_skills is _LEGACY else kept_skills
    return sorted(key for key in disabled if key.startswith(plugin + ':') and key.split(':', 1)[1] not in kept)


def drop_unselected_agents(stage, plugin, selection=_LEGACY):
    keep = AGENT_SELECTION.get(plugin) if selection is _LEGACY else selection
    if keep is None:
        return []
    removed = []
    for path in sorted((stage / 'agents').glob('*.md'), key=lambda path: path.parts):
        front = yaml.safe_load(read_utf8(path).split('---', 2)[1]) or {}
        if str(front.get('name', path.stem)) not in keep:
            path.unlink()
            removed.append(path.name)
    return removed


def package_agents(root):
    agents = {}
    for path in sorted((root / 'agents').glob('*.md'), key=lambda path: path.parts):
        front = yaml.safe_load(read_utf8(path).split('---', 2)[1]) or {}
        agents[str(front.get('name', path.stem))] = path
    return agents


def claude_host_names(text, plugin, agents):
    # Upstream names Cursor's generic subagent and its own agents unqualified;
    # Claude calls the generic one general-purpose and namespaces plugin agents.
    text = text.replace('generalPurpose', 'general-purpose')
    for agent in agents:
        text = text.replace(f'subagent_type: "{agent}"', f'subagent_type: "{plugin}:{agent}"')
    return text


def tree_files(root):
    # Runtime markers and script-installed dependencies are not package content.
    return {posix(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob('*'), key=lambda path: path.parts) if path.is_file() and path.name != '.orphaned_at'
            and not {'.in_use', 'node_modules'} & set(path.relative_to(root).parts)}


def verify(entries):
    # Rewritten on every install, so the receipt always describes what is installed now.
    records = read_json(claude_home(HOME) / 'plugins/installed_plugins.json').get('plugins', {})
    adaptations = ROOT / 'harness-adaptations.json'
    adapted = read_json(adaptations).get('plugins', {}) if adaptations.exists() else {}
    receipt = []
    for entry in entries:
        identity = entry['name'] + '@codex-aligned'
        build_dir = CATALOG / entry['source']
        alignment = read_json(build_dir / '.claude-plugin/codex-alignment.json')
        native = read_json(build_dir / '.claude-plugin/plugin.json')
        installs = records.get(identity, [])
        unavailable = [record for record in installs
                       if record.get('scope') in ('project', 'local')
                       and not Path(record.get('projectPath', '')).is_dir()]
        installs = [record for record in installs if record not in unavailable]
        expected = tree_files(build_dir)
        item = {
            'id': identity, 'version': entry['version'],
            'skills': sorted(Path(path).parent.name for path in alignment['selectedSkills']),
            'skillCount': len(alignment['selectedSkills']),
            'instructionBodiesPreserved': alignment['instructionBodiesPreserved'],
            'installedPackageMatches': bool(installs) and all(
                tree_files(Path(record['installPath'])) == expected for record in installs),
            'installScopes': sorted(record.get('scope', '') for record in installs),
            'unavailableProjectScopes': [record.get('projectPath') for record in unavailable],
            'noAddedAgentsHooksOrMCP': 'hooks' not in native and 'mcpServers' not in native
                and len(native.get('agents', [])) == len(alignment['agents']),
        }
        if entry['name'] in adapted:
            repository = adapted[entry['name']]['repository']
            item['codexHarnessAdaptation'] = repository + '@' + subprocess.run(
                [*GIT, '-C', repository, 'rev-parse', '--short', adapted[entry['name']]['adaptedRef']],
                capture_output=True, text=True, check=True).stdout.strip()
        receipt.append(item)
    write_json(CATALOG / 'verification.json', receipt)
    return receipt


def build(name, marketplace, config):
    parent = codex_home(HOME) / 'plugins/cache' / marketplace / name
    versions = [path for path in parent.iterdir() if path.is_dir()]
    if len(versions) != 1:
        raise RuntimeError(f'Expected one active Codex version for {name}: {versions}')
    source = versions[0]
    disabled = {entry['name'] for entry in config.get('skills', {}).get('config', [])
                if entry.get('enabled') is False and 'name' in entry}
    return build_from(name, source, disabled, CATALOG, validate=lambda path: command('plugin', 'validate', str(path)))


def build_from(name, source, disabled, catalog_root, *, kept_skills=_LEGACY, agents=_LEGACY, invocable=frozenset(),
               validate=None, version=None, roots=None, source_label=None, development_manifests=()):
    relocations = []
    if development_manifests:
        if sorted(development_manifests) != ['package-lock.json', 'package.json']:
            raise RuntimeError('Claude development manifests must name package.json and package-lock.json once each')
        for relative in sorted(development_manifests):
            original = source / relative
            destination = Path('provenance/development') / relative
            if original.is_symlink() or not original.is_file():
                raise RuntimeError(f'Development manifest is missing or linked: {relative}')
            for path in (source / destination, source / destination.parent, source / destination.parent.parent):
                if path.is_symlink() or (path.exists() and (path == source / destination or not path.is_dir())):
                    raise RuntimeError(f'Development manifest preservation path is occupied: {destination}')
            relocations.append({'source': relative, 'destination': posix(destination),
                                'sha256': hashlib.sha256(original.read_bytes()).hexdigest()})
    data = manifest(source)
    selected = selected_skills(source, data, disabled, name, kept_skills)
    if not selected:
        return None
    digest = hashlib.sha256()
    digest.update(json.dumps([posix(path) for path in selected]).encode())
    digest.update(ADAPTATION.encode())
    if relocations:
        digest.update(json.dumps(relocations, sort_keys=True).encode())
    if invocable:
        digest.update(json.dumps(sorted(invocable)).encode())
    for path in sorted(source.rglob('*'), key=lambda path: path.parts):
        if path.is_file() and not any(part in ('.git', 'node_modules', '__pycache__') for part in path.relative_to(source).parts):
            digest.update(posix(path.relative_to(source)).encode())
            content = path.read_bytes()
            if roots is not None and path.name in ('upstream.json', 'codex-alignment.json'):
                content = roots._neutral_file(posix(path.relative_to(source)), content)
            digest.update(content)
    version = version or re.sub(r'\+.*$', '', str(data.get('version', '0.1.0'))) + '+codex-aligned.' + digest.hexdigest()[:12]
    target = catalog_root / 'plugins' / name / version
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent, prefix='.build-') as folder:
            stage = Path(folder) / 'plugin'
            shutil.copytree(source, stage, ignore=shutil.ignore_patterns(
                '.git', '.out-of-scope', 'node_modules', '__pycache__', '.codex-plugin',
                '.claude-plugin', '.cursor-plugin', '.codex-marketplace-install.json'))
            for relocation in relocations:
                destination = stage / relocation['destination']
                destination.parent.mkdir(parents=True, exist_ok=True)
                (stage / relocation['source']).rename(destination)
            # Claude scans default skill directories as well as explicit manifest paths.
            # Remove the excluded skills from this copy; never modify upstream files.
            wanted = set(selected)
            for path in list(stage.rglob('SKILL.md')):
                if path.relative_to(stage) not in wanted:
                    shutil.rmtree(path.parent)
            adjusted = []
            for relative in selected:
                path = stage / relative
                before = read_utf8(source / relative)
                prefix, header, body = before.split('---', 2)
                front = yaml.safe_load(header)
                sidecar = path.parent / 'agents/openai.yaml'
                explicit = sidecar.exists() and (yaml.safe_load(read_utf8(sidecar)) or {}).get('policy', {}).get('allow_implicit_invocation') is False
                # Claude requires a lowercase skill name. Keep the directory slug.
                portable_name = re.sub(r'[^a-z0-9-]+', '-', str(front['name']).lower()).strip('-')
                changes = {}
                if portable_name != front['name']:
                    changes['name'] = portable_name
                if relative.parent.name in invocable:
                    if front.get('disable-model-invocation') is True:
                        changes['disable-model-invocation'] = False
                elif explicit and front.get('disable-model-invocation') is not True:
                    changes['disable-model-invocation'] = True
                if changes:
                    front.update(changes)
                    write_utf8(path, prefix + '---\n' + yaml.safe_dump(front, sort_keys=False, allow_unicode=True) + '---' + body)
                    adjusted.append({'path': posix(relative), 'changes': changes})
                if read_utf8(path).split('---', 2)[2] != body:
                    raise RuntimeError(f'Instruction body changed: {relative}')
            drop_unselected_agents(stage, name, agents)
            selected_agents = package_agents(stage)
            rewritten = []
            documents = dict.fromkeys([*selected_agents.values()] + [path for relative in selected for path in sorted((stage / relative).parent.rglob('*.md'), key=lambda path: path.parts)])
            for path in documents:
                text = read_utf8(path)
                adapted = claude_host_names(text, name, selected_agents)
                if adapted != text:
                    write_utf8(path, adapted)
                    rewritten.append(posix(path.relative_to(stage)))
            for relative in selected:
                body = read_utf8(source / relative).split('---', 2)[2]
                if read_utf8(stage / relative).split('---', 2)[2] != claude_host_names(body, name, selected_agents):
                    raise RuntimeError(f'Instruction body changed beyond host names: {relative}')
            native = {key: data[key] for key in ('description', 'author', 'homepage', 'repository', 'license') if key in data}
            native.update(name=name, version=version, skills=['./' + posix(path.parent) for path in selected],
                          agents=['./' + posix(path.relative_to(stage)) for path in selected_agents.values()])
            write_json(stage / '.claude-plugin/plugin.json', native)
            receipt = {
                'codexSource': str(source_label or source), 'codexVersion': data.get('version'),
                'selectedSkills': [posix(path) for path in selected],
                'excludedSkillNames': excluded_skill_names(disabled, name, kept_skills),
                'headerChanges': adjusted, 'instructionBodiesPreserved': not rewritten,
                'hostNameRewrites': sorted(rewritten), 'agents': sorted(selected_agents),
            }
            if relocations:
                receipt['developmentManifests'] = relocations
            write_json(stage / '.claude-plugin/codex-alignment.json', receipt)
            if validate is not None:
                validate(stage)
            stage.rename(target)
    return {'name': name, 'source': './' + posix(target.relative_to(catalog_root)),
            'version': version, 'description': data.get('description', name)}


def maintenance_selection(installed):
    return {item['id'].split('@', 1)[0] for item in installed
            if item['id'].endswith('@codex-aligned') and item['enabled']
            and (item.get('scope') not in ('project', 'local')
                 or Path(item.get('projectPath', '')).is_dir())}


def merged_catalog(previous, entries):
    replacements = {entry['name']: entry for entry in entries}
    retained = [entry for entry in previous if entry['name'] not in replacements]
    return retained + entries


def refresh_scoped_receipts(identity, version, build_dir, previous):
    """A native update can resolve a worktree to its parent project's receipt."""
    path = claude_home(HOME) / 'plugins/installed_plugins.json'
    registry = read_json(path)
    records = registry.get('plugins', {}).get(identity, [])
    expected = tree_files(build_dir)
    verified = next((record for record in records
                     if record['version'] == version
                     and tree_files(Path(record['installPath'])) == expected), None)
    if verified is None:
        raise RuntimeError(f'Native update has no verified cache: {identity}')
    changed = False
    for record in records:
        if record.get('scope') not in ('project', 'local') or record['version'] == version:
            continue
        if not Path(record.get('projectPath', '')).is_dir():
            continue
        if record not in previous:
            raise RuntimeError(f'Scoped installation changed during maintenance: {identity}')
        # Keep the old cache for recovery; move only this unchanged receipt to
        # bytes already installed and verified through the native CLI.
        record.update(installPath=verified['installPath'], version=version,
                      lastUpdated=verified['lastUpdated'])
        changed = True
    if changed:
        write_json(path, registry)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true', help='Register and install the generated plugins')
    parser.add_argument('--maintenance', action='store_true',
                        help='Update existing enabled copies only; preserve disables, uninstalls and other catalog entries')
    args = parser.parse_args()
    config = tomllib.loads(read_utf8(codex_home(HOME) / 'config.toml'))
    installed_before = json.loads(command('plugin', 'list', '--json')) if args.maintenance else []
    eligible = maintenance_selection(installed_before) if args.maintenance else set(SOURCES)
    entries = [entry for name, marketplace in SOURCES.items()
               if name in eligible
               and config.get('plugins', {}).get(f'{name}@{marketplace}', {}).get('enabled')
               and (entry := build(name, marketplace, config))]
    catalog_path = CATALOG / '.claude-plugin/marketplace.json'
    previous = read_json(catalog_path)['plugins'] if args.maintenance and catalog_path.exists() else []
    catalog_entries = merged_catalog(previous, entries)
    write_json(CATALOG / '.claude-plugin/marketplace.json', {
        'name': 'codex-aligned', 'owner': {'name': 'Max Fabian'},
        'description': 'Personal Claude packages from the active Codex skill selection.',
        'plugins': catalog_entries,
    })
    command('plugin', 'validate', str(CATALOG))
    # Installs copy a build into Claude's cache, so superseded catalog builds are inert.
    current = {(CATALOG / entry['source']).resolve() for entry in catalog_entries}
    for build_dir in sorted((CATALOG / 'plugins').glob('*/*')):
        if build_dir.is_dir() and build_dir.resolve() not in current:
            shutil.rmtree(build_dir)
    if args.install:
        command('plugin', 'marketplace', 'add', str(CATALOG))
        installed = {item['id']: item for item in json.loads(command('plugin', 'list', '--json'))}
        records = read_json(claude_home(HOME) / 'plugins/installed_plugins.json').get('plugins', {})
        for entry in entries:
            identity = entry['name'] + '@codex-aligned'
            if args.maintenance and (identity not in installed or not installed[identity]['enabled']):
                raise RuntimeError(f'Activation changed during maintenance; preserve current state: {identity}')
            command('plugin', 'update' if identity in installed else 'install', identity)
            # Project and local installs pin their own version; refresh each from its project.
            for record in records.get(identity, []):
                if record.get('scope') in ('project', 'local') and Path(record.get('projectPath', '')).is_dir():
                    command('plugin', 'update', identity, '--scope', record['scope'], cwd=record['projectPath'])
            if args.maintenance:
                refresh_scoped_receipts(identity, entry['version'], CATALOG / entry['source'],
                                        records.get(identity, []))
            if not args.maintenance and identity in installed and not installed[identity]['enabled']:
                command('plugin', 'enable', identity)
            print(identity, entry['version'])
        selected = {entry['name'] for entry in entries}
        for name in (() if args.maintenance else SOURCES.keys() - selected):
            identity = name + '@codex-aligned'
            if identity in installed and installed[identity]['enabled']:
                command('plugin', 'disable', identity)
        failed = [item['id'] for item in verify(entries) if not item['installedPackageMatches']]
        if failed:
            raise RuntimeError(f'Installed packages differ from their catalog builds: {failed}')
    else:
        print(json.dumps(entries, indent=2))


if __name__ == '__main__':
    main()
