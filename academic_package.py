"""Package the selected AWT skills using its maintained Codex preparation code."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

import yaml

from package import ROOT, files, git
from portable import extract_tar, posix, read_utf8, write_utf8


def load_installer(source, expected_hash):
    path = source / 'scripts/install-codex-skills.py'
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
        raise RuntimeError('Upstream Codex installer changed; review it before approving its new hash')
    spec = importlib.util.spec_from_file_location('awt_upstream_installer', path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def preserve_policy(folder, policy):
    path = folder / 'SKILL.md'
    _, header, body = read_utf8(path).split('---', 2)
    front = yaml.safe_load(header)
    for key in ('disable-model-invocation', 'user-invocable', 'allowed-tools'):
        if key in policy['frontmatter']:
            front[key] = policy['frontmatter'][key]
        else:
            front.pop(key, None)
    # The upstream installer owns path conversion and skill content. This
    # overlay carries only the user's existing invocation/tool settings.
    write_utf8(path, '---\n' + yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=1000) + '---' + body)
    sidecar = folder / 'agents/openai.yaml'
    sidecar.parent.mkdir(exist_ok=True)
    write_utf8(sidecar, policy['openaiYaml'])


def build(name, config, revision='HEAD', *, output_root=None):
    repository = Path(config['checkout'])
    if git(repository, 'status', '--porcelain').strip():
        raise RuntimeError(f'Upstream checkout has local changes: {repository}')
    commit = git(repository, 'rev-parse', revision).decode().strip()
    version = '0.1.0+upstream.' + commit[:12] + '.codex' + str(config['packagingRevision'])
    release_root = (output_root or ROOT) / name
    release_root.mkdir(parents=True, exist_ok=True)
    archive = git(repository, 'archive', commit)
    with tempfile.TemporaryDirectory(prefix='.build-', dir=release_root) as temporary:
        temporary = Path(temporary)
        source = temporary / 'source'
        source.mkdir()
        extract_tar(archive, source)
        installer = load_installer(source, config['reviewedInstallerSha256'])
        requirements = source / 'scripts/codex-skills-requirements.txt'
        if hashlib.sha256(requirements.read_bytes()).hexdigest() != config['runtimeRequirementsSha256']:
            raise RuntimeError('Upstream runtime requirements changed; prepare and verify a new private runtime')
        selected = config['selectedSkills']
        if len(set(selected)) != len(selected) or not set(selected).issubset(installer.NAMES):
            raise RuntimeError('A selected academic skill is missing or duplicated')
        if set(config['invocationPolicies']) != set(selected):
            raise RuntimeError('Invocation policies must cover exactly the selected academic skills')
        prepared = temporary / 'prepared'
        prepared.mkdir()
        # Prepare only; the upstream installer never writes to live roots.
        installer.prepare(source, prepared, temporary / 'empty-destination', Path(config['python']))
        stage = temporary / 'package'
        (stage / 'skills').mkdir(parents=True)
        prepared_hashes = {}
        portable_references = []
        for skill in selected:
            prepared_hashes[skill] = files(prepared / skill)
            shutil.copytree(prepared / skill, stage / 'skills' / skill)
            # References must keep working after standalone Claude aliases retire.
            for reference in (stage / 'skills' / skill / 'references').rglob('*.md'):
                original = read_utf8(reference)
                portable = original.replace('.claude/skills/' + skill + '/', '{skill_dir}/')
                if portable != original:
                    write_utf8(reference, portable)
                    portable_references.append(posix(reference.relative_to(stage)))
            preserve_policy(stage / 'skills' / skill, config['invocationPolicies'][skill])
        shutil.copy2(source / 'LICENSE', stage / 'LICENSE')
        metadata = stage / '.codex-plugin'
        metadata.mkdir()
        (metadata / 'upstream-source.tar').write_bytes(archive)
        manifest = {
            'name': name, 'version': version,
            'description': 'Eight academic writing skills prepared by the upstream Codex installer.',
            'author': {'name': 'yha9806'}, 'repository': config['url'],
            'skills': './skills/',
            'interface': {'displayName': config['displayName'],
                          'shortDescription': 'Upstream academic writing skills',
                          'developerName': 'yha9806', 'category': 'Productivity',
                          'websiteURL': config['url']},
        }
        write_utf8(metadata / 'plugin.json', json.dumps(manifest, indent=2) + '\n')
        receipt = {
            'repository': config['url'], 'commit': commit, 'subdirectory': '', 'version': version,
            'selectedSkillRoots': ['./skills/' + s for s in selected],
            'preparation': 'upstream scripts/install-codex-skills.py:prepare',
            'reviewedInstallerSha256': config['reviewedInstallerSha256'],
            'sourceArchiveSha256': hashlib.sha256(archive).hexdigest(),
            'runtimeRequirementsSha256': config['runtimeRequirementsSha256'], 'python': config['python'],
            'upstreamPreparedFileHashes': prepared_hashes,
            'invocationPolicies': config['invocationPolicies'],
            'portableReferencePaths': portable_references,
            'localChanges': 'Plugin metadata, recorded invocation/tool-policy overlays and skill-relative helper paths in references. No workflow rewrites.',
        }
        write_utf8(metadata / 'upstream.json', json.dumps(receipt, indent=2) + '\n')
        release = release_root / version
        if release.exists():
            if files(release) != files(stage):
                raise RuntimeError(f'Existing release differs: {release}')
        else:
            stage.rename(release)
    return release, receipt


def verify_helpers(release):
    receipt = json.loads(read_utf8(release / '.codex-plugin/upstream.json'))
    archive = (release / '.codex-plugin/upstream-source.tar').read_bytes()
    if hashlib.sha256(archive).hexdigest() != receipt['sourceArchiveSha256']:
        raise RuntimeError('Original upstream archive differs from its receipt')
    with tempfile.TemporaryDirectory(prefix='awt-package-check-') as temporary:
        source = Path(temporary)
        extract_tar(archive, source)
        installer = load_installer(source, receipt['reviewedInstallerSha256'])
        installer.check_runtime(receipt['python'])
        installer.check_node()
        installer.smoke(release / 'skills', receipt['python'])
    for path in (release / 'skills').glob('*/SKILL.md'):
        for relative in re.findall(r'\{skill_dir\}/([A-Za-z0-9/._-]+)', read_utf8(path)):
            if not (path.parent / relative).is_file():
                raise RuntimeError(f'Missing packaged helper: {path.parent / relative}')
    return {'upstreamInstallerSmoke': 'passed', 'portableHelperReferences': 'passed'}
