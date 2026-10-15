"""Package selected upstream skill folders without maintaining shared installations."""
from datetime import datetime, timezone
import base64
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
import tomllib
import yaml

from package import ROOT, files
from verify_candidate import validate
from portable import agents_home, codex_home, posix, read_pointer, read_utf8, set_pointer, write_utf8


def read(path):
    return json.loads(read_utf8(path))


def content_hashes(root):
    return {p: h for p, h in files(root).items()
            if '.DS_Store' not in Path(p).parts and '__pycache__' not in Path(p).parts and not p.endswith('.pyc')}


def blob_hash(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def snapshot(skills, source_record, destination):
    digest = hashlib.sha256(json.dumps({'files': content_hashes(skills), 'source': source_record}, sort_keys=True).encode()).hexdigest()
    target = destination / digest
    source_record = {**source_record, 'snapshotSha256': digest, 'fileHashes': content_hashes(skills)}
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.snapshot-', dir=target.parent) as tmp:
            staged = Path(tmp) / 'snapshot'
            shutil.copytree(skills, staged / 'skills', ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
            write_utf8(staged / 'source.json', json.dumps(source_record, indent=2) + '\n')
            staged.rename(target)
    else:
        if content_hashes(target / 'skills') != source_record['fileHashes']:
            raise RuntimeError(f'Edited source snapshot: {target}')
    return target


def build_snapshot(name, definition, source_snapshot, *, output_root=None):
    record = read(source_snapshot / 'source.json')
    if content_hashes(source_snapshot / 'skills') != record['fileHashes']:
        raise RuntimeError(f'Source snapshot differs from its receipt: {name}')
    selected = sorted(definition['skills'])
    if sorted(p.parent.name for p in (source_snapshot / 'skills').glob('*/SKILL.md')) != selected:
        raise RuntimeError(f'Selected skills changed: {name}')
    version = '0.1.0+snapshot.' + record['snapshotSha256'][:12] + '.personal' + str(definition['packagingRevision'])
    parent = (output_root or ROOT) / name
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.build-', dir=parent) as tmp:
        stage = Path(tmp) / 'package'
        shutil.copytree(source_snapshot / 'skills', stage / 'skills')
        metadata = stage / '.codex-plugin'
        metadata.mkdir()
        manifest = {
            'name': name, 'version': version, 'description': definition['description'],
            'author': {'name': 'Personal packaging'}, 'skills': './skills/',
            'interface': {'displayName': definition['displayName'], 'shortDescription': definition['description'],
                          'developerName': definition.get('repository') or 'Personal workflows',
                          'category': 'Developer Tools'},
        }
        if definition.get('repository'):
            manifest['repository'] = 'https://github.com/' + definition['repository']
            manifest['interface']['websiteURL'] = manifest['repository']
        write_utf8(metadata / 'plugin.json', json.dumps(manifest, indent=2) + '\n')
        receipt = {
            'repository': manifest.get('repository', 'Personal local files'),
            'commit': record.get('commit'), 'sourceKind': definition['sourceKind'], 'version': version,
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


def build(name, config, revision=None, *, output_root=None):
    definition = read(ROOT / 'standalone-packages.json')['collections'][name]
    if revision and revision != 'HEAD':
        candidate = ROOT / 'standalone-candidates' / name / revision / 'candidate.json'
        if not candidate.is_file():
            raise RuntimeError(f'No reviewed source candidate for {name} at {revision}')
        source = Path(read(candidate)['sourceSnapshot'])
    else:
        source = Path(definition['initialSnapshot'])
    return build_snapshot(name, definition, source, output_root=output_root)


def api(repository, endpoint):
    immutable = endpoint.startswith(('git/trees/', 'git/blobs/'))
    cache = ROOT / 'github-object-cache' / hashlib.sha256((repository + '/' + endpoint).encode()).hexdigest()
    if immutable and cache.exists():
        return read(cache)
    process = subprocess.run(['gh', 'api', 'repos/' + repository + '/' + endpoint],
                             capture_output=True, text=True, timeout=35)
    if process.returncode:
        raise RuntimeError(f'GitHub source unavailable: {repository}/{endpoint}: {process.stderr.strip()}')
    result = json.loads(process.stdout)
    if immutable:
        cache.parent.mkdir(exist_ok=True)
        write_utf8(cache, json.dumps(result) + '\n')
    return result


def upstream_revision(definition):
    repository = definition['repository']
    branch = definition.get('branch') or api(repository, '')['default_branch']
    return api(repository, 'git/ref/heads/' + branch)['object']['sha']


def discover_definition(definition, commit):
    resolved = copy.deepcopy(definition)
    discovery = definition.get('autoDiscoverSkills')
    if not discovery:
        return resolved
    root = PurePosixPath(discovery['root'])
    if root.is_absolute() or '..' in root.parts or not root.parts:
        raise RuntimeError('Invalid upstream discovery root')
    repository = definition['repository']
    tree = api(repository, 'git/trees/' + commit + '?recursive=1')
    if tree.get('truncated'):
        raise RuntimeError('Incomplete upstream discovery tree; leave the current package installed')
    known_paths = {value['sourcePath']: name for name, value in definition['skills'].items()}
    found = set()
    for entry in tree['tree']:
        path = PurePosixPath(entry['path'])
        if len(path.parts) != len(root.parts) + 2 or path.parts[:len(root.parts)] != root.parts or path.name != 'SKILL.md':
            continue
        if entry['type'] != 'blob' or entry['mode'] not in ('100644', '100755'):
            raise RuntimeError('Unsupported discovered skill file: ' + str(path))
        source_path = str(path.parent)
        found.add(source_path)
        if source_path in known_paths:
            continue
        blob = api(repository, 'git/blobs/' + entry['sha'])
        if blob.get('encoding') != 'base64':
            raise RuntimeError('Unsupported discovered skill encoding')
        data = base64.b64decode(blob['content'])
        if blob_hash(data) != entry['sha']:
            raise RuntimeError('Discovered skill checksum mismatch')
        parts = data.decode().split('---', 2)
        if len(parts) != 3 or parts[0].strip():
            raise RuntimeError('Invalid discovered skill frontmatter: ' + str(path))
        front = yaml.safe_load(parts[1])
        name = front.get('name') if isinstance(front, dict) else None
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or len(name) > 64:
            raise RuntimeError('Invalid discovered skill name: ' + str(path))
        if name in resolved['skills']:
            raise RuntimeError('Discovered skill name collides with another source: ' + name)
        resolved['skills'][name] = {'sourcePath': source_path}
    missing = set(known_paths) - found
    if missing:
        raise RuntimeError('Existing upstream skills were removed or moved: ' + ', '.join(sorted(missing)))
    return resolved


def selected_sources(definition, commit=None):
    repository = definition['repository']
    commit = commit or upstream_revision(definition)
    result = {}
    for name, source in definition['skills'].items():
        tree = commit
        for part in PurePosixPath(source['sourcePath']).parts:
            if part in ('.', '..'):
                raise RuntimeError('Invalid upstream skill path')
            entries = api(repository, 'git/trees/' + tree)['tree']
            found = next((e for e in entries if e['path'] == part and e['type'] == 'tree'), None)
            if not found:
                raise RuntimeError(f'Upstream selected path is missing: {repository}/{source["sourcePath"]}')
            tree = found['sha']
        content = api(repository, 'git/trees/' + tree + '?recursive=1')
        if content.get('truncated'):
            raise RuntimeError('Incomplete upstream tree; leave the current package installed')
        blobs = [e for e in content['tree'] if e['type'] == 'blob']
        for entry in blobs:
            path = PurePosixPath(entry['path'])
            if path.is_absolute() or '..' in path.parts or entry['mode'] not in ('100644', '100755'):
                raise RuntimeError(f'Unsupported upstream file: {entry["path"]}')
        result[name] = {'repository': repository, 'sourcePath': source['sourcePath'], 'tree': tree,
                        'blobHashes': {e['path']: e['sha'] for e in blobs},
                        'modes': {e['path']: e['mode'] for e in blobs}}
    return commit, result


def prepare_candidate(name, definition, previous, commit, sources):
    with tempfile.TemporaryDirectory(prefix='skill-source-candidate-') as tmp:
        skills = Path(tmp) / 'skills'
        skills.mkdir()
        for skill, source in sources.items():
            current = previous / 'skills' / skill
            for relative, sha in source['blobHashes'].items():
                target = skills / skill / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                old = current / relative
                if old.is_file() and blob_hash(old.read_bytes()) == sha:
                    data = old.read_bytes()
                else:
                    result = api(source['repository'], 'git/blobs/' + sha)
                    if result['encoding'] != 'base64':
                        raise RuntimeError('Unsupported GitHub blob encoding')
                    data = base64.b64decode(result['content'])
                    if blob_hash(data) != sha:
                        raise RuntimeError(f'Upstream blob checksum mismatch: {relative}')
                target.write_bytes(data)
                target.chmod(0o755 if source['modes'][relative] == '100755' else 0o644)
            metadata = current / 'agents/openai.yaml'
            # A collection whose invocation follows upstream keeps upstream's sidecar, or none.
            if metadata.is_file() and not definition.get('metadataFollowsUpstream'):
                target = skills / skill / 'agents/openai.yaml'
                target.parent.mkdir(exist_ok=True)
                shutil.copy2(metadata, target)
        policy = ('Pristine selected upstream folders, including upstream agents/openai.yaml metadata.'
                  if definition.get('metadataFollowsUpstream') else
                  'Pristine selected upstream folders; preserve the installed agents/openai.yaml metadata only.')
        source_record = {'commit': commit, 'selectedSources': sources, 'policy': policy}
        stored = snapshot(skills, source_record, ROOT / 'standalone-snapshots' / name)
    release, receipt = build_snapshot(name, definition, stored)
    destination = ROOT / 'standalone-candidates' / name / commit
    destination.mkdir(parents=True, exist_ok=True)
    write_utf8(destination / 'candidate.json', json.dumps({'sourceSnapshot': str(stored), 'release': str(release)}, indent=2) + '\n')
    return release, receipt


def verify_installed(name):
    marketplace = read(agents_home() / 'plugins/marketplace.json')
    entry = next(p for p in marketplace['plugins'] if p['name'] == name)
    release = Path.home() / entry['source']['path']
    receipt = read(release / '.codex-plugin/upstream.json')
    definition = read(ROOT / 'standalone-packages.json')['collections'][name]
    rebuilt, _ = build_snapshot(name, definition, Path(receipt['sourceSnapshot']))
    if files(rebuilt) != files(release):
        raise RuntimeError(f'Published source differs: {name}')
    cache = codex_home() / 'plugins/cache/personal' / name / receipt['version']
    if content_hashes(cache) != content_hashes(release):
        raise RuntimeError(f'Edited Codex package: {name}')
    return release, receipt


def publish_codex(name, release, backup):
    from claude_package import update_json
    home = Path.home()
    settings = tomllib.loads(read_utf8(codex_home(home) / 'config.toml'))
    if name + '@personal' not in settings.get('plugins', {}):
        raise RuntimeError('Record explicit Codex enablement before installing')
    marketplace = agents_home(home) / 'plugins/marketplace.json'
    old = next((e for e in read(marketplace)['plugins'] if e['name'] == name), None)
    receipt = read(release / '.codex-plugin/upstream.json')
    cache = codex_home(home) / 'plugins/cache/personal' / name / receipt['version']
    if cache.exists():
        if content_hashes(cache) != content_hashes(release):
            raise RuntimeError('Edited Codex destination; preserve it')
    else:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.publish-', dir=cache.parent) as tmp:
            staged = Path(tmp) / 'package'
            shutil.copytree(release, staged)
            staged.rename(cache)
    entry = dict(old) if old else {'name': name, 'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_USE'}, 'category': 'Developer Tools'}
    entry['source'] = {'source': 'local', 'path': './' + posix(release.relative_to(home))}
    backup.mkdir(parents=True, exist_ok=True)
    previous_caches = []
    for previous_cache in cache.parent.iterdir():
        if previous_cache != cache and (previous_cache / '.codex-plugin/plugin.json').is_file():
            if read(previous_cache / '.codex-plugin/plugin.json')['name'] != name:
                raise RuntimeError('Unexpected package in the Codex cache')
            previous_caches.append({'original': str(previous_cache), 'archive': str(backup / 'previous-caches' / previous_cache.name),
                                    'hashes': files(previous_cache)})
    transaction = {'name': name, 'previousEntry': old, 'publishedEntry': entry,
                   'candidateCache': str(cache), 'candidateHashes': files(cache),
                   'previousCaches': previous_caches, 'backup': str(backup)}
    write_utf8(backup / 'codex-publication.json', json.dumps(transaction, indent=2) + '\n')
    def change(data):
        current = next((e for e in data['plugins'] if e['name'] == name), None)
        if current != old:
            raise RuntimeError('Concurrent Codex source change')
        data['plugins'] = [e for e in data['plugins'] if e['name'] != name] + [entry]
    update_json(marketplace, change)
    try:
        for previous_cache in previous_caches:
            source, saved = Path(previous_cache['original']), Path(previous_cache['archive'])
            if files(source) != previous_cache['hashes']:
                raise RuntimeError('Codex cache changed during publication')
            saved.parent.mkdir(parents=True, exist_ok=True)
            source.rename(saved)
    except Exception:
        restore_codex(transaction)
        raise
    return transaction


def restore_codex(transaction):
    from claude_package import update_json
    def change(data):
        current = next((e for e in data['plugins'] if e['name'] == transaction['name']), None)
        if current != transaction['publishedEntry']:
            raise RuntimeError('Concurrent Codex change; selective rollback needs review')
        data['plugins'] = [e for e in data['plugins'] if e['name'] != transaction['name']]
        if transaction['previousEntry'] is not None:
            data['plugins'].append(transaction['previousEntry'])
    update_json(agents_home() / 'plugins/marketplace.json', change)
    for previous in transaction.get('previousCaches', []):
        saved, original = Path(previous['archive']), Path(previous['original'])
        if saved.exists():
            if original.exists():
                raise RuntimeError('Previous cache destination changed during rollback')
            saved.rename(original)
    candidate = Path(transaction['candidateCache'])
    if transaction['previousEntry'] != transaction['publishedEntry'] and candidate.exists():
        if files(candidate) != transaction['candidateHashes']:
            raise RuntimeError('Candidate cache was edited; retain it for manual rollback')
        candidate.rename(Path(transaction['backup']) / 'rolled-back-candidate-cache')


def claude_additions(config, name, added):
    """The Claude collection config with added skills, or None when there is nothing to add.

    A collection without a Claude package reaches Claude through its codex-aligned copy, which picks up
    added skills from the Codex install, so it needs no edit here.
    """
    packages = config['collections'].get(name, [])
    if not added or not packages:
        return None
    if len(packages) != 1:
        raise RuntimeError('Automatic additions require one existing Claude package for the collection')
    updated = copy.deepcopy(config)
    updated['collections'][name][0]['skills'].extend(added)
    return updated


def require_disabled_duplicates(name, definition):
    if not definition.get('duplicateSkills'):
        return
    settings = tomllib.loads(read_utf8(codex_home() / 'config.toml'))
    overrides = {entry['name']: entry.get('enabled') for entry in settings.get('skills', {}).get('config', []) if 'name' in entry}
    for skill in definition.get('duplicateSkills', {}):
        if skill not in definition['skills']:
            raise RuntimeError('Duplicate policy names an unknown upstream skill: ' + skill)
        qualified = name + ':' + skill
        if overrides.get(qualified) is not False:
            raise RuntimeError('Disable the duplicate through Codex skills/config/write before publication: ' + qualified)


def replace_record(path, section, key, previous, replacement):
    from claude_package import update_json
    def change(data):
        owner = data[section]
        if isinstance(owner, list):
            index = next(i for i, entry in enumerate(owner) if entry.get('id') == key)
            if owner[index] != previous:
                raise RuntimeError('Concurrent source record change: ' + str(path))
            owner[index] = replacement
        else:
            if owner.get(key) != previous:
                raise RuntimeError('Concurrent source record change: ' + str(path))
            owner[key] = replacement
    update_json(path, change)


def update(args):
    import claude_package as cp
    name = args.plugin
    definition = read(ROOT / 'standalone-packages.json')['collections'][name]
    previous, previous_receipt = verify_installed(name)
    if definition['sourceKind'] != 'github':
        print(json.dumps({'plugin': name, 'result': 'Local or retired source verified; no automatic upstream replacement.',
                          'sourceKind': definition['sourceKind'], 'updateAvailable': False}, indent=2))
        return
    commit = upstream_revision(definition)
    candidate_definition = discover_definition(definition, commit)
    added = sorted(set(candidate_definition['skills']) - set(definition['skills']))
    commit, sources = selected_sources(candidate_definition, commit)
    changed = any(source['tree'] != previous_receipt['selectedSources'].get(n, {}).get('tree') for n, source in sources.items())
    status = {'plugin': name, 'upstreamCommit': commit, 'updateAvailable': changed,
              'newSkills': added, 'duplicateSkills': candidate_definition.get('duplicateSkills', {})}
    if args.check:
        print(json.dumps(status, indent=2))
        return
    if args.apply_reviewed and commit != args.apply_reviewed:
        raise RuntimeError('Upstream changed after review; prepare the new revision')
    if args.allow_added_skill:
        raise RuntimeError('This route preserves the exact selected skills')
    follows_upstream = bool(definition.get('metadataFollowsUpstream'))
    if changed or follows_upstream:
        # Rebuilding is deterministic, so an unchanged follow-upstream collection resolves to its current release.
        release, receipt = prepare_candidate(name, candidate_definition, previous, commit, sources)
        changed = changed or release.resolve() != previous.resolve()
    if not changed:
        release, receipt = previous, previous_receipt
    status['updateAvailable'] = changed
    status['verification'] = validate(previous, release, allowed_added_skills=added,
                                      allow_implicit_additions=bool(definition.get('autoDiscoverSkills')),
                                      upstream_invocation_skills=set(definition['skills']) if follows_upstream else ())
    claude_config = read(ROOT / 'claude-packages.json')
    next_claude_config = claude_additions(claude_config, name, added) or claude_config
    claude = cp.build(name, release, config=next_claude_config)
    for package in claude:
        if added:
            package['allowedAddedSkills'] = added
        if follows_upstream:
            package['upstreamInvocationSkills'] = sorted(set(package['skills']) & set(definition['skills']))
    cp.validate_packages(claude)
    if not changed and not cp.needs_update(claude):
        require_disabled_duplicates(name, candidate_definition)
        status['result'] = 'already current and verified'
        print(json.dumps(status, indent=2))
        return
    status.update(candidateRelease=str(release), claudeCandidates=claude)
    if not args.apply_reviewed:
        status['result'] = 'candidate prepared; not activated'
        print(json.dumps(status, indent=2))
        return
    require_disabled_duplicates(name, candidate_definition)
    backup = codex_home() / 'skill-updates' / ('plugin-update-' + name + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    backup.mkdir(parents=True, exist_ok=True)
    records = []
    candidate_definition.update(initialSnapshot=receipt['sourceSnapshot'], lastVerifiedUpstreamCommit=commit,
                                lastSourceTreeCheck=str(backup / 'result.json'))
    edits = [(ROOT / 'standalone-packages.json', 'collections', name, definition, candidate_definition)]
    if added and name in claude_config['collections']:
        edits.append((ROOT / 'claude-packages.json', 'collections', name, claude_config['collections'][name], next_claude_config['collections'][name]))
        native_path = ROOT / 'claude-native-plugins.json'
        if native_path.is_file():
            for record in read(native_path)['plugins']:
                if record.get('collection') == name and record.get('distribution') == 'personal-generated':
                    updated = copy.deepcopy(record)
                    updated['selectedSkillRoots'] = ['./skills/' + skill for skill in candidate_definition['skills']]
                    edits.append((native_path, 'plugins', record['id'], record, updated))
    write_utf8(backup / 'source-records.json', json.dumps([{'path':str(p),'section':s,'key':k,'previous':old,'published':new} for p,s,k,old,new in edits], indent=2) + '\n')
    codex_transaction = claude_transaction = None
    current = ROOT / name / 'current'
    old_pointer = read_pointer(current)
    if old_pointer is None:
        raise RuntimeError(f'Expected a publication pointer: {current}')
    try:
        for edit in edits:
            replace_record(*edit)
            records.append(edit)
        codex_transaction = publish_codex(name, release, backup)
        claude_transaction = cp.publish(claude, backup)
        status['claudeVerification'] = cp.verify_installed(claude)
        verify_installed(name)
        set_pointer(current, release.name)
    except Exception:
        try:
            if claude_transaction is not None:
                cp.restore_publication(claude_transaction)
        finally:
            if codex_transaction is not None:
                restore_codex(codex_transaction)
            for path, section, key, old, new in reversed(records):
                replace_record(path, section, key, new, old)
            if read_pointer(current) != old_pointer:
                set_pointer(current, old_pointer)
        raise
    status.update(result='updated and verified', backup=str(backup), sharedCopiesRefreshed=0)
    write_utf8(backup / 'result.json', json.dumps(status, indent=2) + '\n')
    print(json.dumps(status, indent=2))
