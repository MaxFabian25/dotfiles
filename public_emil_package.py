"""Package Emil's public skills with the recorded Codex compatibility metadata."""
import json
from pathlib import Path, PurePosixPath
import re

from description import BuildSource, PublicEmilRecipe
from package import files, git, require_clean
from portable import extract_tar, read_utf8, replace, staging_directory, write_utf8


def build(name: str, recipe: PublicEmilRecipe, source: BuildSource, checkout: Path, *, output_root: Path):
    require_clean(checkout)
    extras = source.metadata.get('skillMetadata', {})
    selected = [{'sourcePath': path, 'sourceName': PurePosixPath(path).name, 'installedName': installed,
                 **extras.get(installed, {})} for path, installed in zip(recipe.select, recipe.skills())]
    commit = git(checkout, 'rev-parse', recipe.commit).decode().strip()
    version = f"0.1.0+upstream.{commit[:12]}.codex{source.packaging}"
    release_root = output_root / name
    release_root.mkdir(parents=True, exist_ok=True)
    with staging_directory(prefix='.build-', dir=release_root) as temporary:
        upstream = Path(temporary) / 'source'
        stage = Path(temporary) / 'package'
        upstream.mkdir()
        stage.mkdir()
        paths = [s['sourcePath'] for s in selected]
        archive = git(checkout, 'archive', commit, 'LICENSE', *paths)
        extract_tar(archive, upstream)
        license_bytes = (upstream / 'LICENSE').read_bytes()
        (stage / 'LICENSE').write_bytes(license_bytes)
        upstream_hashes, compatibility_changes = {}, {}
        for selection in selected:
            original = upstream / selection['sourcePath']
            installed_name = selection['installedName']
            target = stage / 'skills' / installed_name
            if not (original / 'SKILL.md').is_file():
                raise RuntimeError(f'Missing selected upstream skill: {original}')
            source_files = files(original)
            upstream_hashes[installed_name] = source_files
            for relative in source_files:
                destination = target / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((original / relative).read_bytes())
            skill = target / 'SKILL.md'
            prefix, header, body = read_utf8(skill).split('---', 2)
            source_name = selection['sourceName']
            if not re.search(r'^name:\s*' + re.escape(source_name) + r'\s*$', header, re.M):
                raise RuntimeError(f'Upstream skill identity changed: {source_name}')
            if installed_name != source_name:
                header = re.sub(r'^(name:\s*)' + re.escape(source_name) + r'\s*$',
                                lambda m: m[1] + installed_name, header, flags=re.M)
            if selection.get('removeDisableModelInvocation'):
                header = re.sub(r'^disable-model-invocation:\s*true\n', '', header, flags=re.M)
            write_utf8(skill, prefix + '---' + header + '---' + body)
            (target / 'LICENSE.txt').write_bytes(license_bytes)
            if 'openaiYaml' in selection:
                sidecar = target / 'agents/openai.yaml'
                if sidecar.exists() and read_utf8(sidecar) != selection['openaiYaml']:
                    raise RuntimeError(f'Upstream added conflicting invocation metadata: {installed_name}')
                sidecar.parent.mkdir(exist_ok=True)
                write_utf8(sidecar, selection['openaiYaml'])
            if selection.get('legacyProvenance'):
                legacy = {
                    'source': source.metadata['url'] + '.git', 'ref': source.metadata['branch'], 'commit': commit,
                    'sourceDirectory': selection['sourcePath'], 'licenseSource': 'LICENSE',
                    'sourceName': source_name, 'localName': installed_name,
                    'normalizations': {
                        'removedFrontmatter': ['disable-model-invocation'],
                        'codexInvocationPolicy': 'explicit-only',
                    },
                    'files': sorted(files(target)),
                }
                write_utf8(target / '.emil-upstream.json', json.dumps(legacy, indent=2) + '\n')
            packaged = files(target)
            compatibility_changes[installed_name] = [
                path for path, digest in packaged.items() if source_files.get(path) != digest
            ]
            assert read_utf8(skill).split('---', 2)[2] == body, 'Instruction body changed'

        interface = {
            'displayName': source.display_name, 'shortDescription': source.metadata['description'],
            'developerName': 'Emil Kowalski (upstream); personal Codex packaging',
            'category': 'Developer Tools', 'websiteURL': source.metadata['url'],
        }
        portable = {
            '$schema': 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json',
            'name': name, 'version': version, 'description': source.metadata['description'],
            'repository': source.metadata['url'], 'license': 'MIT',
            'extensions': {'com.openai': {'interface': interface}},
        }
        write_utf8(stage / 'plugin.json', json.dumps(portable, indent=2) + '\n')
        metadata = stage / '.codex-plugin'
        metadata.mkdir()
        compatibility = {k: portable[k] for k in ('name', 'version', 'description', 'repository', 'license')}
        compatibility.update(skills='./skills/', interface=interface)
        write_utf8(metadata / 'plugin.json', json.dumps(compatibility, indent=2) + '\n')
        receipt = {
            'repository': source.metadata['url'], 'branch': source.metadata['branch'], 'commit': commit,
            'version': version, 'selectedSkills': selected,
            'upstreamFileHashes': upstream_hashes,
            'compatibilityChangedFiles': compatibility_changes,
            'packagedFileHashes': files(stage),
        }
        write_utf8(metadata / 'upstream.json', json.dumps(receipt, indent=2) + '\n')
        release = release_root / version
        if release.exists():
            if files(release) != files(stage):
                raise RuntimeError(f'Existing immutable release differs: {release}')
        else:
            replace(stage, release)
    return release, receipt
