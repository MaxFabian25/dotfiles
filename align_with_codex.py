"""Build Claude's aligned copy of a Codex plugin; sync calls build_from for each aligned Claude install."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

import yaml

from portable import posix, read_utf8, replace, staging_directory, write_utf8

# Bump when the Claude adaptation changes, so unchanged Codex sources still rebuild.
ADAPTATION = 'claude-host-names-1'


def read_json(path):
    return json.loads(read_utf8(path))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write((json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode())
    temporary.chmod(path.stat().st_mode & 0o777 if path.exists() else 0o600)
    replace(temporary, path)


def manifest(root):
    for relative in ('.codex-plugin/plugin.json', '.claude-plugin/plugin.json', 'plugin.json'):
        path = root / relative
        if path.exists():
            return read_json(path)
    raise RuntimeError(f'Missing plugin manifest: {root}')


def selected_skills(root, data, disabled, plugin, kept=frozenset()):
    paths = data.get('skills', ['./skills'])
    if isinstance(paths, str):
        paths = [paths]
    result = []
    base = root.resolve()
    for relative in paths:
        directory = (root / relative).resolve()
        if not directory.is_relative_to(base):
            raise RuntimeError(f'Skill escapes plugin: {directory}')
        candidates = [directory / 'SKILL.md'] if (directory / 'SKILL.md').exists() else directory.rglob('SKILL.md')
        for path in candidates:
            front = yaml.safe_load(read_utf8(path).split('---', 2)[1])
            name = str(front.get('name', path.parent.name))
            if f'{plugin}:{name}' not in disabled or name in kept:
                result.append(path.relative_to(base))
    return sorted(set(result), key=lambda path: path.parts)


def excluded_skill_names(disabled, plugin, kept=frozenset()):
    return sorted(key for key in disabled if key.startswith(plugin + ':') and key.split(':', 1)[1] not in kept)


def drop_unselected_agents(stage, keep):
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


def build_from(name, source, disabled, catalog_root, *, kept_skills=frozenset(), agents=None, invocable=frozenset(),
               version=None, roots=None, source_label=None, development_manifests=()):
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
    # Only when set, so a copy that keeps every agent keeps the version it had before agents counted.
    if agents is not None:
        digest.update(json.dumps({'agents': sorted(agents)}).encode())
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
        with staging_directory(dir=target.parent, prefix='.build-') as folder:
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
            drop_unselected_agents(stage, agents)
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
            stage.rename(target)
    return {'name': name, 'source': './' + posix(target.relative_to(catalog_root)),
            'version': version, 'description': data.get('description', name)}
