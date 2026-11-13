"""Package pristine upstream files for Codex without rewriting skill content."""
import hashlib
import json
from pathlib import Path
import re
import shutil

from description import BuildSource, GenericRecipe
from portable import (GIT, checked_output, extract_tar, posix, read_utf8, replace, require_readable, staging_directory,
                      write_utf8)


def files(root):
    # Dependencies a packaged script installs beside itself at run time are not package content,
    # nor are the .in_use and .orphaned_at markers Claude Code writes into installed plugin caches,
    # nor the __pycache__ folders Python writes beside any module it imports.
    require_readable(root)
    return {
        posix(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob('*'), key=lambda path: path.parts)
        if path.is_file() and path.name != '.orphaned_at'
        and not {'node_modules', '.in_use', '__pycache__'} & set(path.relative_to(root).parts)
    }


def git(repository, *arguments):
    return checked_output([*GIT, '-C', str(repository), *arguments])


def require_clean(repository):
    if git(repository, 'rev-parse', '--is-bare-repository').strip() != b'true':
        if git(repository, 'status', '--porcelain').strip():
            raise RuntimeError(f'Upstream checkout has local changes: {repository}')


def export_root(stage, selected):
    """Codex's skill list gives each root a plugin names its own row of about 160 characters, and every description
    shares what the rows leave. So a selection under skills/ is exported as that one folder, and the skills it leaves
    out are removed, because Codex lists everything under a root. Returns the exported roots and the files removed."""
    base = stage.resolve()
    root = base / 'skills'
    chosen = [(stage / path).resolve() for path in selected]
    if not all(path.is_relative_to(root) for path in chosen):
        return selected, set()
    omitted = set()
    for skill in list(root.rglob('SKILL.md')):
        if skill.exists() and not any(skill.parent.is_relative_to(path) for path in chosen):
            omitted |= {posix(path.relative_to(base)) for path in skill.parent.rglob('*') if path.is_file()}
            shutil.rmtree(skill.parent)
    return ['./skills/'], omitted


def package(name: str, recipe: GenericRecipe, source: BuildSource, checkout: Path, *, output_root: Path):
    require_clean(checkout)
    commit = git(checkout, 'rev-parse', recipe.commit).decode().strip()
    subdirectory = recipe.subdirectory or ''
    tree = f"{commit}:{subdirectory}" if subdirectory else commit
    release_root = output_root / name
    release_root.mkdir(parents=True, exist_ok=True)
    with staging_directory(prefix='.build-', dir=release_root) as temporary:
        stage = Path(temporary) / 'package'
        stage.mkdir()
        archive = git(checkout, 'archive', tree)
        # Codex's package copier skips symlinks, so they become copies of their targets:
        # upstream's AGENTS.md -> CLAUDE.md is included without text changes.
        extract_tar(archive, stage)
        original = files(stage)
        upstream = json.loads(read_utf8(stage / recipe.manifest))
        version = upstream['version'] + '+upstream.' + commit[:12]
        if source.packaging:
            version += '.codex' + str(source.packaging)
        skills = list(recipe.select) or ['./skills/']
        for skill in skills:
            if not (stage / skill).is_dir():
                raise RuntimeError(f'Missing selected skill directory: {skill}')
        exported, omitted = export_root(stage, skills)
        # Codex reads agents/openai.yaml for invocation policy. Preserve all
        # upstream files, adding a sidecar only where upstream has none.
        for skill in (stage / 'skills').rglob('SKILL.md'):
            frontmatter = read_utf8(skill).split('---', 2)[1]
            if re.search(r'^disable-model-invocation:\s*true\s*$', frontmatter, re.M):
                sidecar = skill.parent / 'agents/openai.yaml'
                if not sidecar.exists():
                    sidecar.parent.mkdir(exist_ok=True)
                    write_utf8(sidecar, 'policy:\n  allow_implicit_invocation: false\n')
        manifest = {
            key: upstream[key] for key in
            ('description', 'author', 'homepage', 'license', 'keywords')
            if key in upstream
        }
        manifest.update({
            'name': name,
            'version': version,
            'skills': exported,
            'interface': {
                'displayName': source.display_name,
                'shortDescription': 'Original upstream skills',
                'developerName': upstream['author']['name'],
                'category': 'Developer Tools',
                'websiteURL': source.metadata['url'],
            },
        })
        metadata = stage / '.codex-plugin'
        metadata.mkdir(exist_ok=True)
        preserved = {}
        if 'plugin.json' in original:
            saved_manifest = metadata / 'upstream-manifests/plugin.json'
            if saved_manifest.exists():
                raise RuntimeError('Upstream uses the manifest preservation path')
            saved_manifest.parent.mkdir(exist_ok=True)
            (stage / 'plugin.json').rename(saved_manifest)
            preserved['plugin.json'] = posix(saved_manifest.relative_to(stage))
            write_utf8(stage / 'plugin.json', json.dumps(manifest, indent=2) + '\n')
        write_utf8(metadata / 'plugin.json', json.dumps(manifest, indent=2) + '\n')
        actual = files(stage)
        assert all(actual.get(preserved.get(path, path)) == digest for path, digest in original.items() if path not in omitted), 'Upstream file changed'
        receipt = {
            'repository': source.metadata['url'], 'commit': commit,
            'subdirectory': subdirectory, 'version': version,
            'selectedSkillRoots': skills,
            'upstreamFileHashes': original,
            'packagingOnlyFiles': sorted(set(actual) - set(original)),
        }
        if omitted:
            receipt['omittedUpstreamFiles'] = sorted(omitted)
        if preserved:
            receipt['preservedUpstreamFiles'] = preserved
            receipt['adaptedManifestFiles'] = ['plugin.json']
        write_utf8(metadata / 'upstream.json', json.dumps(receipt, indent=2) + '\n')
        release = release_root / version
        if release.exists():
            if files(release) != files(stage):
                raise RuntimeError(f'Existing release differs: {release}')
        else:
            replace(stage, release)
    return release, receipt
