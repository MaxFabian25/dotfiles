"""Build Claude plugins from verified, immutable Codex collections."""
import hashlib
import json
from pathlib import Path
import re
import shutil

import yaml

from invocation_overrides import transform_policy
from package import ROOT, files
from portable import read_utf8, staging_directory, write_utf8

MARKETPLACE = 'personal-collections'
CATALOG_ROOT = ROOT / 'claude'


def read(path):
    return json.loads(read_utf8(path))


def contract(release):
    result = {}
    for skill in sorted((release / 'skills').glob('*/SKILL.md')):
        front = yaml.safe_load(read_utf8(skill).split('---', 2)[1])
        result[front['name']] = {k: front.get(k, default) for k, default in (
            ('disable-model-invocation', False), ('user-invocable', True), ('allowed-tools', None))}
    return result


def build(collection, codex_release, config=None, catalog_root=CATALOG_ROOT, explicit=frozenset()):
    """Claude Code runs a directory marketplace's plugins from the catalog, never from its install cache, so a
    skill the setup makes explicit-only is explicit in the release itself, and the version names those skills."""
    config = config or read(ROOT / 'claude-packages.json')
    definitions = config['collections'].get(collection, [])
    if not definitions:
        return []
    source = read(codex_release / '.codex-plugin/upstream.json')
    actual = {p.parent.name for p in (codex_release / 'skills').glob('*/SKILL.md')}
    selected = [n for entry in definitions for n in entry['skills']]
    if len(selected) != len(set(selected)) or set(selected) != actual:
        raise RuntimeError(f'Claude selection does not cover the collection exactly: {collection}')
    packages = []
    for entry in definitions:
        name = entry['name']
        parent = catalog_root / 'plugins' / name
        parent.mkdir(parents=True, exist_ok=True)
        with staging_directory(prefix='.build-', dir=parent) as tmp:
            stage = Path(tmp) / 'package'
            (stage / 'skills').mkdir(parents=True)
            original_hashes = {}
            changed_headers, changed_policies, chosen = [], [], []
            for skill_name in entry['skills']:
                original = codex_release / 'skills' / skill_name
                original_hashes[skill_name] = files(original)
                target = stage / 'skills' / skill_name
                shutil.copytree(original, target)
                path = target / 'SKILL.md'
                prefix, header, body = read_utf8(path).split('---', 2)
                sidecar = target / 'agents/openai.yaml'
                policy = yaml.safe_load(read_utf8(sidecar)) or {} if sidecar.exists() else {}
                upstream = policy.get('policy', {}).get('allow_implicit_invocation') is False
                front = yaml.safe_load(header)
                if (upstream or skill_name in explicit) and front.get('disable-model-invocation') is not True:
                    header = re.sub(r'^disable-model-invocation:.*\n', '', header, flags=re.M)
                    header += 'disable-model-invocation: true\n'
                    write_utf8(path, prefix + '---' + header + '---' + body)
                    changed_headers.append(skill_name)
                    if not upstream:
                        chosen.append(skill_name)
                # The same sidecar policy the install overlay writes, so the overlay leaves this release as it is.
                if skill_name in explicit and sidecar.exists():
                    before = read_utf8(sidecar)
                    if (after := transform_policy(before, allow=False)) != before:
                        write_utf8(sidecar, after)
                        changed_policies.append(skill_name)
                        if skill_name not in chosen:
                            chosen.append(skill_name)
                if read_utf8(path).split('---', 2)[2] != body:
                    raise RuntimeError(f'Instruction body changed: {skill_name}')
                target_hashes = files(target)
                for relative, digest in original_hashes[skill_name].items():
                    if relative == 'agents/openai.yaml' and skill_name in changed_policies:
                        continue
                    if relative != 'SKILL.md' and target_hashes.get(relative) != digest:
                        raise RuntimeError(f'Supporting content changed: {skill_name}/{relative}')
            for license_file in codex_release.glob('LICENSE*'):
                if license_file.is_file():
                    shutil.copy2(license_file, stage / license_file.name)
            version = source['version'] + '.claude' + str(config['packagingRevision'])
            if chosen:
                version += '.explicit.' + hashlib.sha256('\n'.join(sorted(chosen)).encode()).hexdigest()[:12]
            metadata = stage / '.claude-plugin'
            metadata.mkdir()
            manifest = {
                'name': name, 'version': version, 'description': entry['description'],
                'author': {'name': 'Personal packaging; content from ' + source['repository']},
                'repository': source['repository'], 'skills': './skills/',
            }
            write_utf8(metadata / 'plugin.json', json.dumps(manifest, indent=2) + '\n')
            receipt = {
                'collection': collection, 'repository': source['repository'], 'commit': source['commit'],
                'version': version, 'codexRelease': str(codex_release), 'selectedSkills': entry['skills'],
                'sourceFileHashes': original_hashes, 'explicitInvocationHeadersAdded': changed_headers,
                'instructionBodiesPreserved': True, 'supportingFilesPreserved': True,
                'invocationContract': contract(stage), 'packagedFileHashes': files(stage),
            }
            if changed_policies:
                receipt['explicitInvocationPoliciesSet'] = changed_policies
            write_utf8(metadata / 'upstream.json', json.dumps(receipt, indent=2) + '\n')
            release = parent / version
            if release.exists():
                if files(release) != files(stage):
                    raise RuntimeError(f'Immutable Claude release differs: {release}')
            else:
                stage.rename(release)
        packages.append({'name': name, 'id': name + '@' + MARKETPLACE, 'version': version,
                         'commit': source['commit'], 'release': str(release), 'skills': entry['skills']})
    return packages
