#!/usr/bin/env python3
"""Package pristine upstream files for Codex without rewriting skill content."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from portable import GIT, agents_home, codex_home, extract_tar, posix, read_utf8, write_utf8

ROOT = Path(__file__).resolve().parent


def files(root):
    # Dependencies a packaged script installs beside itself at run time are not package content,
    # nor are the .in_use and .orphaned_at markers Claude Code writes into installed plugin caches,
    # nor the __pycache__ folders Python writes beside any module it imports.
    return {
        posix(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob('*'), key=lambda path: path.parts)
        if path.is_file() and path.name != '.orphaned_at'
        and not {'node_modules', '.in_use', '__pycache__'} & set(path.relative_to(root).parts)
    }


def git(repository, *arguments):
    return subprocess.check_output([*GIT, '-C', str(repository), *arguments])


def require_clean(repository):
    if git(repository, 'rev-parse', '--is-bare-repository').strip() != b'true':
        if git(repository, 'status', '--porcelain').strip():
            raise RuntimeError(f'Upstream checkout has local changes: {repository}')


def package(name, revision='HEAD', *, config=None, output_root=None):
    config = config if config is not None else json.loads(read_utf8(ROOT / 'sources.json'))[name]
    if config.get('kind') == 'standalone':
        from standalone_package import build
        return build(name, config, revision, output_root=output_root)
    if config.get('kind') == 'public-emil':
        from public_emil_package import build
        return build(name, config, revision, output_root=output_root)
    if config.get('kind') == 'course':
        from course_package import build
        return build(name, config, revision, output_root=output_root)
    if config.get('kind') == 'academic':
        from academic_package import build
        return build(name, config, revision, output_root=output_root)
    repository = Path(config['checkout'])
    require_clean(repository)
    commit = git(repository, 'rev-parse', revision).decode().strip()
    tree = f"{commit}:{config['subdirectory']}" if config['subdirectory'] else commit
    release_root = (output_root or ROOT) / name
    release_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.build-', dir=release_root) as temporary:
        stage = Path(temporary) / 'package'
        stage.mkdir()
        archive = git(repository, 'archive', tree)
        # Codex's package copier skips symlinks, so they become copies of their targets:
        # upstream's AGENTS.md -> CLAUDE.md is included without text changes.
        extract_tar(archive, stage)
        original = files(stage)
        upstream = json.loads(read_utf8(stage / config['manifest']))
        version = upstream['version'] + '+upstream.' + commit[:12]
        if config.get('packagingRevision'):
            version += '.codex' + str(config['packagingRevision'])
        skills = config.get('skills') or ['./skills/']
        for skill in skills:
            if not (stage / skill).is_dir():
                raise RuntimeError(f'Missing selected skill directory: {skill}')
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
            'skills': skills,
            'interface': {
                'displayName': config['displayName'],
                'shortDescription': 'Original upstream skills',
                'developerName': upstream['author']['name'],
                'category': 'Developer Tools',
                'websiteURL': config['url'],
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
        assert all(actual.get(preserved.get(path, path)) == digest for path, digest in original.items()), 'Upstream file changed'
        receipt = {
            'repository': config['url'], 'commit': commit,
            'subdirectory': config['subdirectory'], 'version': version,
            'selectedSkillRoots': skills,
            'upstreamFileHashes': original,
            'packagingOnlyFiles': sorted(set(actual) - set(original)),
        }
        if preserved:
            receipt['preservedUpstreamFiles'] = preserved
            receipt['adaptedManifestFiles'] = ['plugin.json']
        write_utf8(metadata / 'upstream.json', json.dumps(receipt, indent=2) + '\n')
        release = release_root / version
        if release.exists():
            if files(release) != files(stage):
                raise RuntimeError(f'Existing release differs: {release}')
        else:
            stage.rename(release)
    return release, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('build', 'verify'))
    parser.add_argument('plugin', choices=tuple(json.loads(read_utf8(ROOT / 'sources.json'))))
    args = parser.parse_args()
    profile = json.loads(read_utf8(ROOT / 'sources.json'))[args.plugin]
    if args.action == 'verify' and profile.get('kind') == 'standalone':
        from standalone_package import verify_installed
        release, receipt = verify_installed(args.plugin)
        print(json.dumps({'plugin': args.plugin, 'release': str(release), 'version': receipt['version'],
                          'preservedSkills': len(receipt['selectedSkills']), 'installedCacheVerified': True}, indent=2))
        return
    revision='HEAD'
    if args.action=='verify':
        marketplace=json.loads(read_utf8(agents_home()/'plugins/marketplace.json'))
        entry=next(e for e in marketplace['plugins'] if e['name']==args.plugin)
        installed_release=Path.home()/entry['source']['path']
        revision=json.loads(read_utf8(installed_release/'.codex-plugin/upstream.json'))['commit']
    release, receipt = package(args.plugin, revision)
    result = {'plugin': args.plugin, 'release': str(release), 'version': receipt['version'],
              'commit': receipt['commit']}
    if 'preparation' in receipt:
        result['preservedSkills'] = len(receipt['selectedSkillRoots'])
        result['preparation'] = receipt['preparation']
    elif 'selectedSkills' in receipt:
        result['preservedSkills'] = len(receipt['selectedSkills'])
        result['upstreamFilesRecorded'] = sum(len(v) for v in receipt['upstreamFileHashes'].values())
    else:
        result['upstreamFilesUnchanged'] = len(receipt['upstreamFileHashes'])
    if args.action == 'verify':
        cache = codex_home() / 'plugins/cache/personal' / args.plugin / receipt['version']
        if not cache.is_dir() or files(cache) != files(release):
            raise RuntimeError(f'Installed cache does not match upstream release: {cache}')
        result['installedCacheVerified'] = str(cache)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
