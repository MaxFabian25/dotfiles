from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile

from description import (AcademicRecipe, AlignedInstall, BuildSource, CourseRecipe, EngineInstall,
                         GeneratedRoles, GenericRecipe, LocalSource, Package, PluginRoles, PublicEmilRecipe,
                         RepoRoles, RepoSource, Setup, SnapshotRecipe, ToolNotes, plugin_id, via)
from host_state import DeviceRoots, Digest, Host, SkillMode, bytes_digest, clone_url, parse_commit
from portable import GIT, extract_tar, read_utf8


class BuildError(RuntimeError):
    pass


def run_git(repository: Path | None, *arguments: str) -> bytes:
    args = [*GIT, *(['-C', str(repository)] if repository is not None else []), *arguments]
    result = subprocess.run(args, capture_output=True, timeout=300)
    if result.returncode:
        raise BuildError(f'git {arguments[0]} failed: {result.stderr.decode("utf-8", "replace").strip()}')
    return result.stdout


def child(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts or '\\' in relative or ':' in relative:
        raise BuildError(f'Not a repository-relative path: {relative!r}')
    target = root.joinpath(*path.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise BuildError(f'Path leaves its source: {relative!r}')
    return target


class Sources:
    def __init__(self, directory: Path, *, seeds: tuple[Path, ...] = ()):
        self.directory = directory
        self.seeds = seeds

    @staticmethod
    def _available(repository: Path, commit: str, *, history: bool) -> bool:
        try:
            run_git(repository, 'cat-file', '-e', commit + '^{commit}')
            if history:
                if run_git(repository, 'rev-parse', '--is-shallow-repository').strip() == b'true':
                    return False
                run_git(repository, 'rev-list', '--count', commit)
        except BuildError:
            return False
        return True

    def repository(self, url: str, commit: str, *, history: bool = False) -> Path:
        parse_commit(commit)
        identity = clone_url(url)
        for seed in self.seeds:
            try:
                origin = run_git(seed, 'remote', 'get-url', 'origin').decode().strip()
                if clone_url(origin) == identity and self._available(seed, commit, history=history):
                    return seed
            except (BuildError, OSError):
                continue
        key = hashlib.sha256(identity.encode()).hexdigest()[:24]
        target = self.directory / key
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            run_git(None, 'init', '--bare', str(target))
            run_git(target, 'remote', 'add', 'origin', identity)
        actual = run_git(target, 'remote', 'get-url', 'origin').decode().strip()
        if clone_url(actual) != identity:
            raise BuildError(f'Source cache has a different origin: {target}')
        if not self._available(target, commit, history=history):
            options = ['--refetch'] if history else ['--depth=1']
            if history and run_git(target, 'rev-parse', '--is-shallow-repository').strip() == b'true':
                options.append('--unshallow')
            run_git(target, 'fetch', *options, 'origin', commit)
            if not self._available(target, commit, history=history):
                raise BuildError(f'Source lacks the required commit history: {target} at {commit}')
        return target

    def tree(self, url: str, commit: str, destination: Path, relative: str | None = None) -> Path:
        repository = self.repository(url, commit)
        if relative:
            child(Path('/repository'), relative)
        tree = f'{commit}:{relative}' if relative else commit
        destination.mkdir(parents=True, exist_ok=True)
        extract_tar(run_git(repository, 'archive', tree), destination)
        return destination

    def checkout(self, url: str, commit: str, destination: Path, *, history: bool = False) -> Path:
        repository = self.repository(url, commit, history=history)
        created = not destination.exists()
        if created:
            destination.parent.mkdir(parents=True, exist_ok=True)
            run_git(None, 'clone', '--no-checkout', str(repository), str(destination))
            run_git(destination, 'remote', 'set-url', 'origin', url)
        elif clone_url(run_git(destination, 'remote', 'get-url', 'origin').decode().strip()) != clone_url(url):
            raise BuildError(f'Checkout has a different origin: {destination}')
        if not created and run_git(destination, 'status', '--porcelain').strip():
            raise BuildError(f'Checkout has local changes: {destination}')
        if not self._available(destination, commit, history=history):
            options = ['--refetch'] if history else []
            if history and run_git(destination, 'rev-parse', '--is-shallow-repository').strip() == b'true':
                options.append('--unshallow')
            run_git(destination, 'fetch', *options, str(repository), commit)
        # Quiet skips the warning about leaving the old commit, whose walk fails where a native shallow clone
        # holds a pin fetched from the shallow source cache without its ancestry.
        run_git(destination, 'checkout', '--quiet', '--detach', commit)
        return destination


@dataclass(frozen=True)
class Built:
    name: str
    root: Path
    version: str
    digest: Digest


class Builder:
    def __init__(self, setup: Setup, roots: DeviceRoots, directory: Path, *, sources: Sources | None = None):
        self.setup = setup
        self.roots = roots
        self.directory = directory
        self.sources = sources or Sources(directory / 'sources')
        if setup.roots.private:
            stored = setup.roots.private.parent / 'sources'
            self.sources.seeds += tuple(p.parent for p in sorted(stored.glob('*/*/HEAD')))

    def codex(self, package: Package, *, verify: bool = True) -> Built:
        source = package.source
        if isinstance(source, LocalSource):
            owned = self.local(package.visibility, source)
            tree = self.directory / 'owned' / package.name / str(source.digest).removeprefix('sha256:')
            if not tree.exists():
                tree.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(owned, tree)
            if self.roots.tree_digest(tree) != source.digest:
                raise BuildError(f'{package.name}: the cached owned source was edited')
        elif isinstance(source, BuildSource):
            tree = self._build(package, source)
        elif isinstance(source, RepoSource):
            repo = self.setup.repos[source.repo]
            parent = self.directory / 'native' / package.name / repo.commit
            if not verify:
                review = self.directory / '.sync/review-native'
                review.mkdir(parents=True, exist_ok=True)
                parent = Path(tempfile.mkdtemp(prefix=package.name + '-', dir=review)) / 'package'
            if parent.exists():
                tree = parent
            else:
                parent.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix='.native-', dir=parent.parent) as temp:
                    checkout = self.sources.tree(repo.url, repo.commit, Path(temp) / 'repo')
                    relative = source.path or self._plugin_path(checkout, package.name)
                    selected = child(checkout, relative)
                    if selected != checkout:
                        shutil.copytree(selected, Path(temp) / 'package')
                        selected = Path(temp) / 'package'
                    selected.rename(parent)
                tree = parent
        else:
            raise BuildError(f'{package.name} is supplied by the application, not built by sync')
        manifest = plugin_manifest(tree)
        result = Built(package.name, tree, str(manifest.get('version', '0.1.0')), self.roots.tree_digest(tree))
        if verify:
            locked = self.setup.lock.get(package.name)
            expected = locked.trees.get(Host.CODEX) if locked else None
            if expected is not None and result.digest != expected.digest:
                raise BuildError(f'{package.name}: rebuilt tree {result.digest} differs from lock {expected.digest}')
        return result

    def local(self, visibility, source: LocalSource, placement=None) -> Path:
        root = self.setup.roots.of(visibility)
        if root is None:
            raise BuildError('The local source needs its private repository')
        tree = child(root.parent, source.path)
        if not tree.is_dir() or self.roots.tree_digest(tree) != source.digest:
            raise BuildError(f'Owned source {source.path} does not match its declared tree')
        return child(tree, source.variants[placement]) if source.variants and placement else tree

    def _build(self, package: Package, source: BuildSource) -> Path:
        from package import package as build_package
        from standalone_package import build_snapshot

        recipe = source.recipe
        metadata = dict(source.metadata)
        if isinstance(recipe, SnapshotRecipe):
            private = self.setup.roots.private
            if private is None:
                raise BuildError(f'{package.name}: its source snapshot needs the private repository')
            snapshot_source = private / 'snapshots' / str(recipe.snapshot).removeprefix('sha256:')
            if not snapshot_source.is_dir():
                raise BuildError(f'{package.name}: missing source snapshot {recipe.snapshot}')
            snapshot = self.directory / 'standalone-snapshots' / package.name / str(recipe.snapshot).removeprefix('sha256:')
            if not snapshot.exists():
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(snapshot_source, snapshot)
            definition = {**metadata, 'displayName': source.display_name, 'packagingRevision': source.packaging,
                          'skills': {name: {} for name in recipe.origins}}
            if 'description' not in definition or 'sourceKind' not in definition:
                raise BuildError(f'{package.name}: missing snapshot build metadata; migrate its recipe first')
            return build_snapshot(package.name, definition, snapshot, output_root=self.directory)[0]

        checkout = self.sources.repository(recipe.repository, recipe.commit)
        config = {**metadata, 'checkout': str(checkout), 'packagingRevision': source.packaging,
                  'displayName': source.display_name}
        if 'url' not in config:
            raise BuildError(f'{package.name}: missing build metadata; migrate its recipe first')
        if isinstance(recipe, GenericRecipe):
            config.update(subdirectory=recipe.subdirectory or '', manifest=recipe.manifest,
                          skills=list(recipe.select))
        elif isinstance(recipe, (CourseRecipe, PublicEmilRecipe)):
            extras = config.pop('skillMetadata', {})
            selected = []
            for path in recipe.select:
                name = PurePosixPath(path).name
                installed = recipe.prefix + name if isinstance(recipe, CourseRecipe) else recipe.rename.get(name, name)
                selected.append({'sourcePath': path, 'sourceName': name, 'installedName': installed,
                                 **extras.get(installed, {})})
            config.update(kind='course' if isinstance(recipe, CourseRecipe) else 'public-emil',
                          selectedSkills=selected)
            if isinstance(recipe, CourseRecipe):
                config['trimTriggerDescriptions'] = list(recipe.trim_descriptions)
        elif isinstance(recipe, AcademicRecipe):
            config.update(kind='academic', selectedSkills=list(recipe.select),
                          reviewedInstallerSha256=str(recipe.installer).removeprefix('sha256:'),
                          runtimeRequirementsSha256=str(recipe.requirements).removeprefix('sha256:'))
        return build_package(package.name, recipe.commit, config=config, output_root=self.directory)[0]

    def overlay(self, package: Package, host: Host, identity: str, root: Path, *, overlay=None):
        from invocation_overrides import plan, read_text, skill_paths
        import yaml

        label = host.value.capitalize()
        explicit = {name for name, mode in package.modes.items() if mode == SkillMode.EXPLICIT}
        if host == Host.CODEX and package.claude:
            explicit.update(name for name, mode in package.claude.modes.items() if mode == SkillMode.EXPLICIT)
        if host == Host.CODEX:
            paths = skill_paths(root)
            for name in tuple(explicit):
                if name not in paths:
                    continue
                sidecar = paths[name].parent / 'agents/openai.yaml'
                text = read_text(sidecar)
                if overlay is not None:
                    text = overlay.pristine(sidecar.resolve(), text)
                policy = yaml.safe_load(text) if text else {}
                if (policy or {}).get('policy', {}).get('allow_implicit_invocation') is False:
                    explicit.remove(name)
        registry = {'targets': {label: {}}, 'explicitTargets': {label: {identity: sorted(explicit)}}}
        adaptations = {'plugins': {}}
        adaptation = package.codex.adaptation if host == Host.CODEX and package.codex else None
        if adaptation:
            source = self.sources.repository(adaptation.repository, adaptation.base, history=True)
            repository = self.sources.checkout(adaptation.repository, adaptation.adapted,
                                                self.roots.engine / 'harness-adaptations' / package.name,
                                                history=True)
            try:
                run_git(repository, 'cat-file', '-e', adaptation.base + '^{commit}')
            except BuildError:
                run_git(repository, 'fetch', str(source), adaptation.base)
            base = run_git(repository, 'rev-parse', '--verify', adaptation.base + '^{commit}').strip()
            try:
                ancestor = run_git(repository, 'merge-base', adaptation.base, adaptation.adapted).strip()
            except BuildError as error:
                raise BuildError(f'{package.name}: adaptation base must be an ancestor of its adapted commit') from error
            if ancestor != base:
                raise BuildError(f'{package.name}: adaptation base must be an ancestor of its adapted commit')
            adaptations['plugins'][package.name] = {
                'repository': str(repository), 'baseRef': adaptation.base, 'adaptedRef': adaptation.adapted,
                'hosts': [label], 'identities': [identity]}
        if package.tool_notes == ToolNotes.APPLY:
            adaptations['toolNotes'] = {'plugins': [package.name],
                                         'rules': [{'match': r.match, 'note': r.note} for r in self.setup.note_rules]}
        return plan(registry, [(label, identity, root)], overlay=overlay, adaptations=adaptations)[0]

    def effective(self, package: Package, built: Built) -> Path:
        placement = next((i for i in package.codex.installs if i.enabled), None) if package.codex else None
        if placement is None:
            return built.root
        destination = self.directory / '.sync/effective' / package.name / built.version
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(built.root, destination)
        for path, before, after, _, _ in self.overlay(package, Host.CODEX,
                                                     plugin_id(package.name, placement, Host.CODEX), destination):
            if before != after:
                path.parent.mkdir(parents=True, exist_ok=True)
                if after is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(after.encode('utf-8'))
        adaptation = package.codex.adaptation
        if adaptation and adaptation.harness:
            from invocation_overrides import harness_gaps
            if gaps := harness_gaps(destination, adaptation.harness):
                raise BuildError(f'{package.name}: {", ".join(gaps)} use Cursor names without linking {adaptation.harness}')
        return destination

    def claude(self, package: Package, codex: Built, *, verify: bool = True) -> dict[str, Built]:
        from align_with_codex import build_from
        from claude_package import build as build_collection

        if package.claude is None:
            return {}
        result = {}
        locked = self.setup.lock.get(package.name)
        expected = locked.trees.get(Host.CLAUDE) if locked else None
        for install in package.claude.installs:
            if not install.enabled:
                continue
            identity = plugin_id(package.name, install, Host.CLAUDE)
            if isinstance(install, AlignedInstall):
                effective = self.effective(package, codex)
                disabled = {f'{package.name}:{name}' for name, mode in package.modes.items() if mode == SkillMode.OFF}
                provider = next(i for i in package.codex.installs if i.enabled)
                source_label = self.roots.codex_home / 'plugins/cache' / via(provider, Host.CODEX) / package.name / codex.version
                catalog = self.directory / '.sync/claude-aligned'
                if not verify:
                    catalog.parent.mkdir(parents=True, exist_ok=True)
                    catalog = Path(tempfile.mkdtemp(prefix='claude-reviewed-', dir=catalog.parent))
                entry = build_from(package.name, effective, disabled, catalog,
                                   kept_skills=set(package.claude.modes), agents=install.agents,
                                   invocable={s for s, m in package.claude.modes.items() if m == SkillMode.ON},
                                   version=expected.version if expected and verify else None, roots=self.roots, source_label=source_label,
                                   development_manifests=package.source.metadata.get('claudeDevelopmentManifests', ())
                                   if isinstance(package.source, BuildSource) else ())
                if entry is None:
                    raise BuildError(f'{identity}: aligned plugin has no selected skills')
                root = catalog / entry['source']
            elif isinstance(install, EngineInstall):
                metadata = package.source.metadata if isinstance(package.source, BuildSource) else {}
                descriptions = metadata.get('claudeDescriptions', {})
                definitions = []
                for member in package.claude.installs:
                    if not isinstance(member, EngineInstall):
                        continue
                    name = member.name or package.name
                    if name not in descriptions:
                        raise BuildError(f'{identity}: no Claude packaging description in its recipe')
                    skills = member.skills or tuple(sorted(p.parent.name for p in (codex.root / 'skills').glob('*/SKILL.md')))
                    definitions.append({'name': name, 'skills': list(skills), 'description': descriptions[name]})
                config = {'packagingRevision': metadata.get('claudePackaging', 1), 'collections': {package.name: definitions}}
                explicit = frozenset(name for name, mode in package.modes.items() if mode == SkillMode.EXPLICIT)
                packages = build_collection(package.name, codex.root, config, self.directory / 'claude', explicit)
                root = Path(next(p['release'] for p in packages if p['name'] == (install.name or package.name)))
            else:
                root = codex.root
            built = Built(package.name, root, str(plugin_manifest(root).get('version', '0.1.0')), self.roots.tree_digest(root))
            if verify and expected and built.digest != expected.digest:
                raise BuildError(f'{identity}: rebuilt tree {built.digest} differs from lock {expected.digest}')
            result[identity] = built
        return result

    def roles(self, package: Package, codex: Built, *, verify: bool = True) -> dict[str, bytes]:
        from codex_agents import EDITING_TOOLS, NETWORK_TOOLS, frontmatter, rewrite, toml_role
        from invocation_overrides import add_tool_notes

        if package.codex is None or package.codex.roles is None:
            return {}
        role_spec = package.codex.roles
        provider = next(i for i in package.codex.installs if i.enabled)
        installed = self.roots.codex_home / 'plugins/cache' / via(provider, Host.CODEX) / package.name / codex.version
        header_source = self.roots.engine / 'codex_agents.py'
        output = {}
        if isinstance(role_spec, RepoRoles):
            if not isinstance(package.source, RepoSource):
                raise BuildError(f'{package.name}: repository roles require a repository source')
            repo = self.setup.repos[package.source.repo]
            with tempfile.TemporaryDirectory(prefix='roles-') as temp:
                tree = self.sources.tree(repo.url, repo.commit, Path(temp), role_spec.directory)
                for path in sorted(tree.iterdir()):
                    if path.is_file() and fnmatch.fnmatch(path.name, role_spec.pattern):
                        text = rewrite(read_utf8(path), role_spec.rewrite, installed / role_spec.skill,
                                       child(codex.root, role_spec.skill))
                        label = clone_url(repo.url).removeprefix('https://github.com/').removesuffix('.git')
                        header = f'# Generated by {header_source} from {label}@{repo.commit}. Do not edit.\n'
                        output[path.name] = (header + text).encode()
        else:
            effective = self.effective(package, codex)
            directory = child(effective, role_spec.directory)
            if isinstance(role_spec, PluginRoles):
                output = {p.name: p.read_bytes() for p in sorted(directory.glob('*.toml'))}
            elif isinstance(role_spec, GeneratedRoles):
                for path in sorted(directory.glob('*.md')):
                    fields, body = frontmatter(read_utf8(path))
                    if package.tool_notes == ToolNotes.APPLY:
                        body = add_tool_notes(body, [{'match': r.match, 'note': r.note} for r in self.setup.note_rules])
                    tools = {tool.strip() for tool in fields.get('tools', '').split(',') if tool.strip()}
                    sandbox = None if not tools or tools & (EDITING_TOOLS | NETWORK_TOOLS) else 'read-only'
                    source = installed / role_spec.directory / path.name
                    header = f'# Generated by {header_source} from {source}. Do not edit.\n'
                    output[f'{fields["name"]}.toml'] = (header + toml_role(fields, body, sandbox)).encode()
        if verify:
            expected = self.setup.lock[package.name].roles
            actual = {name: bytes_digest(self.roots.neutral(data)) for name, data in output.items()}
            if actual != expected:
                raise BuildError(f'{package.name}: rebuilt agent roles differ from the lock')
        return output

    @staticmethod
    def _plugin_path(checkout: Path, name: str) -> str:
        for relative in ('.codex-plugin/marketplace.json', '.claude-plugin/marketplace.json'):
            manifest = checkout / relative
            if not manifest.is_file():
                continue
            for entry in json.loads(read_utf8(manifest)).get('plugins', []):
                if entry.get('name') != name:
                    continue
                source = entry.get('source')
                if isinstance(source, str):
                    return source
                if isinstance(source, dict) and source.get('source') == 'local':
                    return source['path']
        if plugin_manifest(checkout).get('name') == name:
            return '.'
        raise BuildError(f'{name}: its pinned repository does not declare a local plugin path')


def plugin_manifest(root: Path) -> dict:
    for relative in ('.codex-plugin/plugin.json', '.claude-plugin/plugin.json', 'plugin.json'):
        path = root / relative
        if path.is_file():
            return json.loads(read_utf8(path))
    raise BuildError(f'No plugin manifest in {root}')
