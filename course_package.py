"""Build Codex course plugins from the recorded public Git repositories."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

import yaml

from portable import GIT, extract_tar, read_utf8, write_utf8

ROOT = Path(__file__).resolve().parent


def sha(data):
    return hashlib.sha256(data).hexdigest()


def files(root):
    return {p.relative_to(root).as_posix(): sha(p.read_bytes())
            for p in sorted(root.rglob('*'), key=lambda path: path.parts) if p.is_file()}


def git(repository, *args):
    return subprocess.check_output([*GIT, '-C', str(repository), *args])


def build(name, config, revision='HEAD', *, output_root=None):
    repository = Path(config['checkout'])
    from package import require_clean
    require_clean(repository)
    commit = git(repository, 'rev-parse', revision).decode().strip()
    version = f"0.1.0+upstream.{commit[:12]}.codex{config['packagingRevision']}"
    release_root = (output_root or ROOT) / name
    release_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.build-', dir=release_root) as temporary:
        temporary = Path(temporary)
        source, stage = temporary/'source', temporary/'package'
        source.mkdir(); stage.mkdir()
        paths = [s['sourcePath'] for s in config['selectedSkills']]
        archive = git(repository, 'archive', commit, *paths)
        extract_tar(archive, source)
        source_hashes, changes = {}, {}
        replacements = config.get('markdownReplacements', [])
        for selection in config['selectedSkills']:
            original = source / selection['sourcePath']
            target = stage / 'skills' / selection['installedName']
            if not (original/'SKILL.md').is_file():
                raise RuntimeError(f'Missing selected upstream skill: {selection["sourcePath"]}')
            original_files = files(original)
            source_hashes[selection['installedName']] = original_files
            for relative in original_files:
                source_file = original/relative
                destination = target/relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                data = source_file.read_bytes()
                if replacements and source_file.suffix == '.md':
                    text = data.decode()
                    for old, new in replacements:
                        text = text.replace(old, new)
                    data = text.encode()
                destination.write_bytes(data)
            skill = target/'SKILL.md'
            _, header, body = read_utf8(skill).split('---', 2)
            metadata = yaml.safe_load(header)
            metadata['name'] = selection['installedName']
            explicit = metadata.pop('disable-model-invocation', False)
            if selection['installedName'] in config.get('trimTriggerDescriptions', []):
                metadata['description'] = metadata['description'].split(' Triggers on:', 1)[0]
            if len(metadata.get('description', '')) > 1024:
                raise RuntimeError(f'Description needs a reviewed Codex adaptation: {skill}')
            write_utf8(skill, '---\n'+yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True, width=1000)+'---'+body)
            sidecar = target/'agents/openai.yaml'
            agent = yaml.safe_load(read_utf8(sidecar)) if sidecar.exists() else {}
            interface = agent.setdefault('interface', {})
            if config.get('skillDisplayNamePrefix'):
                interface['display_name'] = config['skillDisplayNamePrefix']+interface.get('display_name', selection['sourceName'])
            interface['default_prompt'] = 'Use $'+selection['installedName']+' for this task.'
            if selection.get('allowImplicitInvocation') is True:
                agent.setdefault('policy', {})['allow_implicit_invocation'] = True
            elif explicit:
                agent.setdefault('policy', {})['allow_implicit_invocation'] = False
            sidecar.parent.mkdir(exist_ok=True)
            write_utf8(sidecar, yaml.safe_dump(agent, sort_keys=False, allow_unicode=True, width=100000))
            adapted = files(target)
            changes[selection['installedName']] = [p for p in adapted if adapted[p] != original_files.get(p)]
            provenance = {
                'repo': config['repository'], 'commit': commit, 'collection': config['collection'],
                'sourcePath': selection['sourcePath'], 'sourceName': selection['sourceName'],
                'installedName': selection['installedName'],
                'normalizations': selection['normalizations'], 'sourceFiles': original_files,
            }
            write_utf8(target/config.get('provenanceFile', '.course-source.json'), json.dumps(provenance, indent=2)+'\n')
        root_files = git(repository, 'ls-tree', '--name-only', commit).decode().splitlines()
        for filename in root_files:
            if re.fullmatch(r'(LICENSE|LICENCE|COPYING)(\..+)?', filename, re.I):
                (stage/filename).write_bytes(git(repository, 'show', commit+':'+filename))
        interface = {
            'displayName': config['displayName'],
            'shortDescription': config['description'],
            'developerName': config['repository'], 'category': 'Developer Tools',
            'websiteURL': config['url'],
        }
        portable = {
            '$schema': 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json',
            'name': name, 'version': version, 'description': config['description'],
            'repository': config['url'], 'extensions': {'com.openai': {'interface': interface}},
        }
        write_utf8(stage/'plugin.json', json.dumps(portable, indent=2)+'\n')
        metadata_dir = stage/'.codex-plugin'; metadata_dir.mkdir()
        compatibility = {k: portable[k] for k in ('name','version','description','repository')}
        compatibility.update(skills='./skills/', interface=interface)
        write_utf8(metadata_dir/'plugin.json', json.dumps(compatibility, indent=2)+'\n')
        receipt = {
            'repository': config['url'], 'branch': config['branch'], 'commit': commit,
            'version': version, 'selectedSkills': config['selectedSkills'],
            'upstreamFileHashes': source_hashes, 'compatibilityChangedFiles': changes,
            'packagedFileHashes': files(stage),
        }
        write_utf8(metadata_dir/'upstream.json', json.dumps(receipt, indent=2)+'\n')
        release = release_root/version
        if release.exists():
            if files(release) != files(stage):
                raise RuntimeError(f'Existing immutable release differs: {release}')
        else:
            stage.rename(release)
    return release, receipt
