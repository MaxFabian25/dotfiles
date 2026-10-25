"""Build and publish Claude plugins from verified, immutable Codex collections."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import yaml

from invocation_overrides import transform_policy
from package import ROOT, files
from portable import claude_cli, claude_home, posix, read_utf8, replace, write_utf8

MARKETPLACE = 'personal-collections'
CATALOG_ROOT = ROOT / 'claude'


def read(path):
    return json.loads(read_utf8(path))


def update_json(path, change):
    before = path.read_bytes()
    data = json.loads(before)
    change(data)
    if path.read_bytes() != before:
        raise RuntimeError(f'Concurrent change: {path}')
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.publish-', delete=False) as f:
        temporary = Path(f.name)
        f.write((json.dumps(data, indent=2) + '\n').encode())
    temporary.chmod(path.stat().st_mode & 0o777)
    replace(temporary, path)


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
        with tempfile.TemporaryDirectory(prefix='.build-', dir=parent) as tmp:
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


def validate_packages(packages):
    for package in packages:
        subprocess.run([claude_cli(), 'plugin', 'validate', package['release'], '--strict'],
                       check=True, capture_output=True, text=True, timeout=30)


def verify_installed(packages):
    results = []
    for package in packages:
        record = current_record(package, Path.home())
        if record is None:
            results.append({'id': package['id'], 'state': 'uninstalled; preserved'})
            continue
        if files(Path(record['installPath'])) != files(Path(package['release'])):
            raise RuntimeError(f'Claude installed files differ: {package["id"]}')
        process = subprocess.run([claude_cli(), 'plugin', 'details', package['id']],
                                 check=True, capture_output=True, text=True, timeout=30)
        inventory = re.search(r'Skills \((\d+)\)\s+([^\n]+)', process.stdout)
        if not inventory or int(inventory[1]) != len(package['skills']) or \
                set(inventory[2].split(', ')) != set(package['skills']):
            raise RuntimeError(f'Claude native skill selection differs: {package["id"]}')
        if not all(re.search(component + r' \(0\)', process.stdout)
                   for component in ('Agents', 'Hooks', 'MCP servers', 'LSP servers')):
            raise RuntimeError(f'Unexpected Claude runtime component: {package["id"]}')
        results.append({'id': package['id'], 'skills': int(inventory[1]), 'filesVerified': True})
    return results


def current_record(package, home):
    records = read(claude_home(home) / 'plugins/installed_plugins.json')['plugins'].get(package['id'], [])
    if not records:
        return None
    if len(records) != 1 or records[0]['scope'] != 'user':
        raise RuntimeError(f'Unexpected Claude installation scope: {package["id"]}')
    record = records[0]
    cache = Path(record['installPath'])
    receipt = read(cache / '.claude-plugin/upstream.json')
    expected = dict(receipt['packagedFileHashes'])
    actual = files(cache)
    actual.pop('.claude-plugin/upstream.json', None)
    if actual != expected:
        raise RuntimeError(f'Edited Claude cache: {package["id"]}')
    previous_contract = contract(cache)
    next_contract = contract(Path(package['release']))
    # A recorded follow-upstream collection may change only its skills' model-invocation flag.
    followed = set(package.get('upstreamInvocationSkills', []))
    for value in (previous_contract, next_contract):
        for name in followed & value.keys():
            value[name] = {k: v for k, v in value[name].items() if k != 'disable-model-invocation'}
    allowed = set(package.get('allowedAddedSkills', []))
    existing_contract = {name: value for name, value in next_contract.items() if name not in allowed}
    if previous_contract != next_contract and (not allowed or previous_contract != existing_contract):
        raise RuntimeError(f'Claude invocation or selection changed: {package["id"]}')
    return record


def needs_update(packages, home=None):
    home = home or Path.home()
    changes = []
    for package in packages:
        record = current_record(package, home)
        changes.append(bool(record) and (record['version'] != package['version'] or
                       files(Path(record['installPath'])) != files(Path(package['release']))))
    return any(changes)


def restore_publication(transaction, home=None, catalog_root=CATALOG_ROOT):
    home = home or Path.home()
    def restore_registry(data):
        for identity, previous in transaction['previousRecords'].items():
            if data['plugins'].get(identity) != transaction['publishedRecords'].get(identity):
                raise RuntimeError(f'Claude installation changed during rollback: {identity}')
            if previous is None:
                data['plugins'].pop(identity, None)
            else:
                data['plugins'][identity] = previous
    update_json(claude_home(home) / 'plugins/installed_plugins.json', restore_registry)
    def restore_catalog(data):
        for name, previous in transaction['previousEntries'].items():
            existing = next((e for e in data['plugins'] if e['name'] == name), None)
            if existing != transaction['publishedEntries'][name]:
                raise RuntimeError(f'Claude catalog changed during rollback: {name}')
            data['plugins'] = [e for e in data['plugins'] if e['name'] != name]
            if previous is not None:
                data['plugins'].append(previous)
    update_json(catalog_root / '.claude-plugin/marketplace.json', restore_catalog)


def publish(packages, backup, home=None, catalog_root=CATALOG_ROOT, bootstrap=False):
    """Publish verified files without enabling plugins or running their components."""
    home = home or Path.home()
    settings = read(claude_home(home) / 'settings.json').get('enabledPlugins', {})
    registry = read(claude_home(home) / 'plugins/installed_plugins.json')
    active = []
    for package in packages:
        previous = current_record(package, home)
        if previous is None and not bootstrap:
            continue  # A user uninstall must not be undone by the updater.
        if package['id'] not in settings:
            raise RuntimeError(f'Explicit Claude enablement is required before publication: {package["id"]}')
        active.append(package)
    catalog_path = catalog_root / '.claude-plugin/marketplace.json'
    catalog = read(catalog_path)
    transaction = {'previousRecords': {}, 'publishedRecords': {}, 'previousEntries': {}, 'publishedEntries': {}}
    stamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    for p in active:
        source = Path(p['release'])
        target = claude_home(home) / 'plugins/cache' / MARKETPLACE / p['name'] / p['version']
        if target.exists():
            if files(target) != files(source):
                raise RuntimeError(f'Claude destination has different content: {target}')
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.publish-', dir=target.parent) as tmp:
                staged = Path(tmp) / 'package'
                shutil.copytree(source, staged)
                if files(staged) != files(source):
                    raise RuntimeError(f'Claude copy failed: {p["id"]}')
                staged.rename(target)
        previous = registry['plugins'].get(p['id'])
        transaction['previousRecords'][p['id']] = previous
        transaction['publishedRecords'][p['id']] = [{
            'scope': 'user', 'installPath': str(target), 'version': p['version'],
            'installedAt': previous[0]['installedAt'] if previous else stamp,
            'lastUpdated': stamp, 'gitCommitSha': p['commit'],
        }]
        if p['commit'] is None:
            transaction['publishedRecords'][p['id']][0].pop('gitCommitSha')
        transaction['previousEntries'][p['name']] = next((e for e in catalog['plugins'] if e['name'] == p['name']), None)
        transaction['publishedEntries'][p['name']] = {
            'name': p['name'], 'source': './' + posix(source.relative_to(catalog_root)),
            'version': p['version'], 'description': read(source / '.claude-plugin/plugin.json')['description'],
        }
    backup.mkdir(parents=True, exist_ok=True)
    write_utf8(backup / 'claude-publication.json', json.dumps(transaction, indent=2) + '\n')
    def catalog_change(data):
        for name, previous in transaction['previousEntries'].items():
            if next((e for e in data['plugins'] if e['name'] == name), None) != previous:
                raise RuntimeError(f'Claude catalog changed concurrently: {name}')
            data['plugins'] = [e for e in data['plugins'] if e['name'] != name]
            data['plugins'].append(transaction['publishedEntries'][name])
    def registry_change(data):
        for identity, previous in transaction['previousRecords'].items():
            if data['plugins'].get(identity) != previous:
                raise RuntimeError(f'Claude registry changed concurrently: {identity}')
            data['plugins'][identity] = transaction['publishedRecords'][identity]
    update_json(catalog_path, catalog_change)
    try:
        update_json(claude_home(home) / 'plugins/installed_plugins.json', registry_change)
    except Exception:
        # Registry was not committed; restore only this publication's catalog entries.
        transaction['publishedRecords'] = transaction['previousRecords']
        restore_publication(transaction, home, catalog_root)
        raise
    return transaction


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('collection')
    parser.add_argument('release', type=Path)
    args = parser.parse_args()
    packages = build(args.collection, args.release.resolve())
    validate_packages(packages)
    print(json.dumps({'claudeCandidates': packages, 'installedUpdateNeeded': needs_update(packages)}, indent=2))


if __name__ == '__main__':
    main()
