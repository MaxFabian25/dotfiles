"""Package the selected AWT skills using its maintained Codex preparation code."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import yaml

from description import AcademicRecipe, BuildSource
from package import files, git
from portable import extract_tar, posix, read_utf8, staging_directory, write_utf8


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


def build(name: str, recipe: AcademicRecipe, source: BuildSource, checkout: Path, *, output_root: Path):
    if git(checkout, 'status', '--porcelain').strip():
        raise RuntimeError(f'Upstream checkout has local changes: {checkout}')
    installer_sha256 = str(recipe.installer).removeprefix('sha256:')
    requirements_sha256 = str(recipe.requirements).removeprefix('sha256:')
    commit = git(checkout, 'rev-parse', recipe.commit).decode().strip()
    version = '0.1.0+upstream.' + commit[:12] + '.codex' + str(source.packaging)
    release_root = output_root / name
    release_root.mkdir(parents=True, exist_ok=True)
    archive = git(checkout, 'archive', commit)
    with staging_directory(prefix='.build-', dir=release_root) as temporary:
        temporary = Path(temporary)
        upstream = temporary / 'source'
        upstream.mkdir()
        extract_tar(archive, upstream)
        installer = load_installer(upstream, installer_sha256)
        requirements = upstream / 'scripts/codex-skills-requirements.txt'
        if hashlib.sha256(requirements.read_bytes()).hexdigest() != requirements_sha256:
            raise RuntimeError('Upstream runtime requirements changed; prepare and verify a new private runtime')
        selected = recipe.select
        if len(set(selected)) != len(selected) or not set(selected).issubset(installer.NAMES):
            raise RuntimeError('A selected academic skill is missing or duplicated')
        if set(source.metadata['invocationPolicies']) != set(selected):
            raise RuntimeError('Invocation policies must cover exactly the selected academic skills')
        prepared = temporary / 'prepared'
        prepared.mkdir()
        # Prepare only; the upstream installer never writes to live roots.
        installer.prepare(upstream, prepared, temporary / 'empty-destination', Path(source.metadata['python']))
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
            preserve_policy(stage / 'skills' / skill, source.metadata['invocationPolicies'][skill])
        shutil.copy2(upstream / 'LICENSE', stage / 'LICENSE')
        metadata = stage / '.codex-plugin'
        metadata.mkdir()
        (metadata / 'upstream-source.tar').write_bytes(archive)
        manifest = {
            'name': name, 'version': version,
            'description': 'Eight academic writing skills prepared by the upstream Codex installer.',
            'author': {'name': 'yha9806'}, 'repository': source.metadata['url'],
            'skills': './skills/',
            'interface': {'displayName': source.display_name,
                          'shortDescription': 'Upstream academic writing skills',
                          'developerName': 'yha9806', 'category': 'Productivity',
                          'websiteURL': source.metadata['url']},
        }
        write_utf8(metadata / 'plugin.json', json.dumps(manifest, indent=2) + '\n')
        receipt = {
            'repository': source.metadata['url'], 'commit': commit, 'subdirectory': '', 'version': version,
            'selectedSkillRoots': ['./skills/' + s for s in selected],
            'preparation': 'upstream scripts/install-codex-skills.py:prepare',
            'reviewedInstallerSha256': installer_sha256,
            'sourceArchiveSha256': hashlib.sha256(archive).hexdigest(),
            'runtimeRequirementsSha256': requirements_sha256, 'python': source.metadata['python'],
            'upstreamPreparedFileHashes': prepared_hashes,
            'invocationPolicies': source.metadata['invocationPolicies'],
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
