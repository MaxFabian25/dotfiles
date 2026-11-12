"""Package selected upstream skill folders without maintaining shared installations."""
import hashlib
import json
from pathlib import Path
import shutil

from description import BuildSource, SnapshotRecipe
from package import files
from portable import read_utf8, staging_directory, write_utf8


def read(path):
    return json.loads(read_utf8(path))


def content_hashes(root):
    return {p: h for p, h in files(root).items()
            if '.DS_Store' not in Path(p).parts and '__pycache__' not in Path(p).parts and not p.endswith('.pyc')}


def snapshot(skills, source_record, destination):
    digest = hashlib.sha256(json.dumps({'files': content_hashes(skills), 'source': source_record}, sort_keys=True).encode()).hexdigest()
    target = destination / digest
    source_record = {**source_record, 'snapshotSha256': digest, 'fileHashes': content_hashes(skills)}
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with staging_directory(prefix='.snapshot-', dir=target.parent) as tmp:
            staged = Path(tmp) / 'snapshot'
            shutil.copytree(skills, staged / 'skills', ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
            write_utf8(staged / 'source.json', json.dumps(source_record, indent=2) + '\n')
            staged.rename(target)
    else:
        if content_hashes(target / 'skills') != source_record['fileHashes']:
            raise RuntimeError(f'Edited source snapshot: {target}')
    return target


def build_snapshot(name: str, recipe: SnapshotRecipe, source: BuildSource, source_snapshot: Path, *, output_root: Path):
    record = read(source_snapshot / 'source.json')
    if content_hashes(source_snapshot / 'skills') != record['fileHashes']:
        raise RuntimeError(f'Source snapshot differs from its receipt: {name}')
    selected = sorted(recipe.origins)
    if sorted(p.parent.name for p in (source_snapshot / 'skills').glob('*/SKILL.md')) != selected:
        raise RuntimeError(f'Selected skills changed: {name}')
    version = '0.1.0+snapshot.' + record['snapshotSha256'][:12] + '.personal' + str(source.packaging)
    parent = output_root / name
    parent.mkdir(parents=True, exist_ok=True)
    with staging_directory(prefix='.build-', dir=parent) as tmp:
        stage = Path(tmp) / 'package'
        shutil.copytree(source_snapshot / 'skills', stage / 'skills')
        metadata = stage / '.codex-plugin'
        metadata.mkdir()
        manifest = {
            'name': name, 'version': version, 'description': source.metadata['description'],
            'author': {'name': 'Personal packaging'}, 'skills': './skills/',
            'interface': {'displayName': source.display_name, 'shortDescription': source.metadata['description'],
                          'developerName': source.metadata.get('repository') or 'Personal workflows',
                          'category': 'Developer Tools'},
        }
        if source.metadata.get('repository'):
            manifest['repository'] = 'https://github.com/' + source.metadata['repository']
            manifest['interface']['websiteURL'] = manifest['repository']
        write_utf8(metadata / 'plugin.json', json.dumps(manifest, indent=2) + '\n')
        receipt = {
            'repository': manifest.get('repository', 'Personal local files'),
            'commit': record.get('commit'), 'sourceKind': source.metadata['sourceKind'], 'version': version,
            'selectedSkills': selected, 'selectedSources': record.get('selectedSources', {}),
            'sourceSnapshot': str(source_snapshot), 'snapshotSha256': record['snapshotSha256'],
            'sourceFileHashes': record['fileHashes'], 'portabilityChanges': record.get('portabilityChanges', []),
            'packagedFileHashes': files(stage),
        }
        write_utf8(metadata / 'upstream.json', json.dumps(receipt, indent=2) + '\n')
        release = parent / version
        if release.exists():
            if files(release) != files(stage):
                raise RuntimeError(f'Immutable release differs: {release}')
        else:
            stage.rename(release)
    return release, receipt
