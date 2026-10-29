"""Every Claude encoding: plugin ids, settings.json, install records, frontmatter, aligned copies.

Claude has no per-plugin skill toggle, so an off skill is spelled per install: an aligned copy leaves the
skill out, any other enabled install turns it off in skillOverrides, and a disabled install has no off skills.
An aligned copy keeps a skill the package has off when the package gives it a Claude mode, and takes that mode.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
import re
import stat
import subprocess
from typing import Sequence

import yaml

from description import (HOST_TOGGLES, RULES, AlignedInstall, BuildSource, EngineInstall, LocalSource, MarketplaceMode, RepoSource, Setup, SkillRoot,
                         ToolNotes, plugin_id, via)
from host_observation import Observer, Unread, checkout, observe_loose, observe_trees, run
from invocation_overrides import frontmatter, read_text, skill_paths
from host_state import (ALIGNED_MARKETPLACE as ALIGNED_VIA, ANTHROPIC_SKILLS, CATALOGS as ALL_CATALOGS, ENGINE_MARKETPLACE, DeviceRoots,
                        CheckoutMarketplace, Commit, EngineMarketplace, Fact, FactKey, FactSet, FileValue, GitMarketplace, Host, InstallDetail,
                        MarketplaceDetail, NoteKind, Observation, OverlaySpec, Pinned, PluginValue, Released, Request,
                        SkillMode, Surface, TreeValue, Unpinned, bytes_digest, clone_url, parse_commit)
import portable

_HOST = Host.CLAUDE
CATALOGS = ALL_CATALOGS[_HOST]
ENGINE = ENGINE_MARKETPLACE[_HOST]


class MissingCache(Unread):
    pass


def _key(surface: Surface, subject: str) -> FactKey:
    return FactKey(_HOST, surface, subject)


def portable_name(skill: str) -> str:
    """Claude requires a lowercase skill name; aligned copies rewrite Codex names this way."""
    return re.sub(r'[^a-z0-9-]+', '-', skill.lower()).strip('-')


def _loaded(catalogs: dict[str, tuple[Path, dict]], plugin_id: str, install: Path) -> Path:
    """The folder Claude Code runs a plugin from. A directory marketplace's plugin whose catalog source is a
    relative path runs from that folder, not from its install cache, so its flags are read there."""
    name, marketplace = plugin_id.split('@', 1)
    if marketplace not in catalogs:
        return install
    directory, catalog = catalogs[marketplace]
    entry = next((p for p in catalog.get('plugins', []) if isinstance(p, dict) and p.get('name') == name), None)
    source = entry.get('source') if entry else None
    if not isinstance(source, str):
        return install
    root = directory / source
    if not root.resolve().is_relative_to(directory.resolve()):
        raise Unread(f'catalog source leaves its marketplace: {plugin_id}')
    if not root.is_dir():
        raise Unread(f'the folder Claude Code loads {plugin_id} from is not on disk: {source}')
    return root


@dataclass(frozen=True)
class NativeMarketplace:
    root: Path
    url: str
    commit: Commit
    pinned: bool


def native_marketplace(roots: DeviceRoots, name: str, configured: dict, registered: dict | None) -> NativeMarketplace | None:
    if registered is None:
        return None
    source = configured.get('source', {})
    if source != registered.get('source') or source.get('source') not in ('git', 'github'):
        raise Unread(f'Native marketplace source disagrees with settings: {name}')
    url = clone_url(source.get('url' if source['source'] == 'git' else 'repo', ''))
    root = Path(registered.get('installLocation', ''))
    expected = roots.claude_home / 'plugins/marketplaces' / name
    if root != expected or root.resolve() != root.absolute():
        raise Unread(f'Native marketplace has an unexpected cache path: {name}')
    manifest_path = Path(source.get('path', '.claude-plugin/marketplace.json'))
    if manifest_path.is_absolute() or '..' in manifest_path.parts:
        raise Unread(f'Native marketplace manifest leaves its checkout: {name}')
    manifest = root / manifest_path
    if (not (root / '.git').is_dir() or (root / '.git').resolve() != (root / '.git').absolute()
            or manifest.resolve() != manifest.absolute()):
        raise Unread(f'Native marketplace has a linked Git directory or manifest: {name}')
    try:
        if json.loads(portable.read_utf8(manifest)).get('name') != name:
            raise Unread(f'Native marketplace manifest has a different name: {name}')
        found = checkout(root)
        if found is None or found[0] != url:
            raise Unread(f'Native marketplace checkout has a different origin: {name}')
        origin, commit, clean = found
        if not clean:
            raise Unread(f'Native marketplace checkout has local changes: {name}')
        detached = portable.read_utf8(root / '.git/HEAD').strip() == commit
    except (OSError, ValueError) as error:
        raise Unread(f'Cannot read native marketplace {name}: {error}') from error
    pinned = (detached and source.get('ref') == commit
              and configured.get('autoUpdate') is False and registered.get('autoUpdate') is False)
    return NativeMarketplace(root, origin, commit, pinned)


class ClaudeState:
    host = _HOST

    def __init__(self, roots: DeviceRoots, cli: str | Sequence[str]) -> None:
        self.roots = roots
        self.cli = [cli] if isinstance(cli, str) else list(cli)

    def engine_outputs(self) -> dict[str, Path]:
        return {ENGINE: self.roots.engine / 'claude', ALIGNED_VIA: self.roots.claude_home / 'marketplaces' / ALIGNED_VIA}

    def project(self, setup: Setup) -> FactSet:
        facts: dict[FactKey, Fact] = {}

        def add(surface, subject, value, entry, follows=None):
            key = _key(surface, subject)
            facts[key] = Fact(key, value, entry, follows)
            return key

        for repo in setup.repos.values():
            if repo.claude:
                kind = CheckoutMarketplace if repo.claude.mode == MarketplaceMode.CHECKOUT else GitMarketplace
                add(Surface.MARKETPLACE, repo.claude.name, kind(repo.url, repo.commit), repo.name)
        for package in setup.packages.values():
            if package.claude is None:
                continue
            lock = setup.lock.get(package.name)
            tree = lock.trees.get(_HOST) if lock else None
            for install in package.claude.installs:
                marketplace = via(install, _HOST)
                if isinstance(install, (EngineInstall, AlignedInstall)):
                    add(Surface.MARKETPLACE, marketplace, EngineMarketplace(), None)
                plugin = plugin_id(package.name, install, _HOST)
                if isinstance(install, AlignedInstall) or isinstance(package.source, (BuildSource, LocalSource)):
                    pin = Released(tree.version) if install.enabled and tree else Unpinned()
                elif isinstance(package.source, RepoSource):
                    pin = Pinned(setup.repos[package.source.repo].commit)
                else:
                    pin = Unpinned()
                key = add(Surface.PLUGIN, plugin, PluginValue(install.enabled, pin), package.name)
                if install.enabled and tree:
                    add(Surface.TREE, plugin, TreeValue(tree.digest), package.name, key)
                share = install.skills if isinstance(install, EngineInstall) and install.skills else None
                if install.enabled and package.tool_notes == ToolNotes.APPLY and not isinstance(install, AlignedInstall):
                    explicit = tuple(sorted(s for s, mode in package.modes.items()
                                            if mode == SkillMode.EXPLICIT and (share is None or s in share)))
                    add(Surface.OVERLAY, plugin, OverlaySpec(setup.note_rules, None, explicit), package.name, key)
                own = package.claude.modes if isinstance(install, AlignedInstall) else {}
                modes = {**{s: m for s, m in package.modes.items() if s not in own},
                         **{s: m for s, m in own.items() if m == SkillMode.EXPLICIT}}
                for skill, mode in modes.items():
                    if share is not None and skill not in share:
                        continue
                    if mode != SkillMode.EXPLICIT and not install.enabled:
                        continue
                    name = portable_name(skill) if isinstance(install, AlignedInstall) else skill
                    add(Surface.SKILL, f'{plugin}:{name}', mode, package.name, key)
        for skill in setup.loose.values():
            if SkillRoot.CLAUDE not in skill.place:
                continue
            lock = setup.lock.get(skill.name)
            key = None
            if lock and SkillRoot.CLAUDE in lock.placements:
                key = add(Surface.LOOSE_SKILL, f'{{claude_home}}/skills/{skill.name}',
                          FileValue(lock.placements[SkillRoot.CLAUDE]), skill.name)
            if not skill.place[SkillRoot.CLAUDE]:
                add(Surface.SKILL, skill.name, SkillMode.OFF, skill.name, key)
        for subject, mode in setup.host_skills.get(_HOST, {}).items():
            add(Surface.SKILL, subject, mode, HOST_TOGGLES[_HOST])
        if setup.rules is not None:
            add(Surface.RULES, 'CLAUDE.md', FileValue(setup.rules.digest), RULES)
        return facts

    def observe(self, request: Request) -> Observation:
        roots = self.roots
        seen = Observer(_HOST, roots)

        def read_json(path: Path, surfaces, default):
            try:
                return json.loads(portable.read_utf8(path)) if path.is_file() else default
            except (OSError, ValueError) as error:
                seen.unread(path, error, surfaces)
                return default

        try:
            version = run([*self.cli, '--version']).strip()
        except (OSError, subprocess.SubprocessError):
            version = 'unknown'
        try:
            listed = json.loads(run([*self.cli, 'plugin', 'list', '--available', '--json']))
            inventory, available = listed['installed'], listed['available']
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            seen.unread('claude plugin list --available --json', error,
                        {Surface.PLUGIN, Surface.TREE, Surface.OVERLAY, Surface.SKILL})
            inventory, available = None, []
        listed = inventory is not None
        inventory = inventory or []
        records_path = roots.claude_home / 'plugins/installed_plugins.json'
        records = read_json(records_path, {Surface.PLUGIN}, {}).get('plugins', {})
        settings = read_json(roots.claude_home / 'settings.json', {Surface.MARKETPLACE, Surface.SKILL}, {})
        marketplaces_path = roots.claude_home / 'plugins/known_marketplaces.json'
        marketplaces = read_json(marketplaces_path, {Surface.MARKETPLACE}, {})

        user = [plugin for plugin in inventory if plugin.get('scope') == 'user']
        scoped = len(inventory) - len(user)
        if scoped:
            seen.note(NoteKind.DEVICE_LOCAL, 'project installs', f'{scoped} project-scoped installs')
        offered = {plugin['id'] for plugin in inventory} | {plugin['pluginId'] for plugin in available}

        def pin(plugin):
            marketplace = plugin['id'].split('@', 1)[1]
            if marketplace in CATALOGS:
                return Unpinned()
            if marketplace in (ENGINE, ALIGNED_VIA):
                return Released(plugin['version'])
            sha = next((record.get('gitCommitSha') for record in records.get(plugin['id'], [])
                        if record.get('scope') == 'user'), None)
            try:
                return Pinned(parse_commit(sha))
            except ValueError:
                return None

        pinless = set()
        for plugin in user:
            value = pin(plugin)
            if value is None:
                pinless.add(plugin['id'])
                continue
            seen.add(Surface.PLUGIN, plugin['id'], PluginValue(plugin['enabled'], value),
                     detail=InstallDetail(plugin['version'], Path(plugin['installPath'])))
        if pinless:
            seen.unread(records_path, 'no commit is recorded for these installs', {Surface.PLUGIN}, pinless)
        if listed:
            ids = {plugin['id'] for plugin in inventory}
            orphans = sorted(set(settings.get('enabledPlugins', {})) - ids)
            if orphans:
                seen.note(NoteKind.RESIDUE, 'enabledPlugins', f'{len(orphans)} entries for plugins that are not installed')

        outputs = self.engine_outputs()
        catalogs: dict[str, tuple[Path, dict]] = {}
        for name, entry in settings.get('extraKnownMarketplaces', {}).items():
            source = entry.get('source', {})
            kind = source.get('source')
            if kind in ('git', 'github'):
                try:
                    native = native_marketplace(roots, name, entry, marketplaces.get(name))
                except Unread as error:
                    seen.unread(marketplaces_path, error, {Surface.MARKETPLACE}, {name})
                    continue
                url = clone_url(source.get('url' if kind == 'git' else 'repo', ''))
                seen.add(Surface.MARKETPLACE, name, GitMarketplace(url, native.commit if native and native.pinned else None),
                         detail=MarketplaceDetail(tuple(source.get('sparsePaths', ()))))
            elif kind == 'directory':
                directory = Path(source.get('path', ''))
                manifest = directory / '.claude-plugin/marketplace.json'
                try:
                    catalog = json.loads(portable.read_utf8(manifest)) if manifest.is_file() else {}
                except (OSError, ValueError):
                    catalog = {}
                matches = isinstance(catalog, dict) and catalog.get('name') == name
                if matches:
                    catalogs[name] = (directory, catalog)
                if matches and name in outputs:
                    seen.add(Surface.MARKETPLACE, name, EngineMarketplace())
                    continue
                if matches:
                    try:
                        found = checkout(directory)
                    except Unread as error:
                        seen.unread(directory, error, {Surface.MARKETPLACE}, {name})
                        continue
                    if found:
                        url, head, clean = found
                        if clean:
                            seen.add(Surface.MARKETPLACE, name, CheckoutMarketplace(url, head))
                        else:
                            seen.unread(directory, 'marketplace checkout has local changes', {Surface.MARKETPLACE}, {name})
                        continue
                seen.note(NoteKind.UNMODELED, f'extraKnownMarketplaces.{name}', 'directory source')
            else:
                seen.note(NoteKind.UNMODELED, f'extraKnownMarketplaces.{name}', f'{kind} source')

        enabled = {}
        for plugin in user:
            if plugin['enabled']:
                enabled.setdefault(plugin['id'].split('@', 1)[0], []).append(plugin['id'])
        for subject, value in settings.get('skillOverrides', {}).items():
            if value not in ('on', 'off', 'user-invocable-only'):
                seen.note(NoteKind.UNMODELED, f'skillOverrides.{subject}', repr(value))
                continue
            mode = SkillMode.EXPLICIT if value == 'user-invocable-only' else SkillMode(value)
            plugin, _, skill = subject.partition(':')
            if not skill:
                seen.add(Surface.SKILL, subject, mode)
            elif len(enabled.get(plugin, [])) == 1:
                owner = enabled[plugin][0]
                seen.add(Surface.SKILL, f'{owner}:{skill}', mode, seen.key(Surface.PLUGIN, owner))
            elif plugin == ANTHROPIC_SKILLS:
                # No install owns these, so they keep their listed name, which hosts/claude.toml can turn off.
                seen.add(Surface.SKILL, subject, mode)
            else:
                seen.note(NoteKind.RESIDUE, f'skillOverrides.{subject}', 'no single enabled install of that plugin')
        local = roots.claude_home / '.claude/settings.local.json'
        if local.is_file():
            seen.note(NoteKind.DEVICE_LOCAL, 'settings.local.json', 'skill overrides scoped to the project at {claude_home}')

        named_skills: dict[str, set[str]] = {}
        for subject in request.skills:
            plugin, _, skill = subject.partition(':')
            if skill and '@' in plugin:
                named_skills.setdefault(plugin, set()).add(skill)
        for plugin in user:
            root = Path(plugin['installPath'])
            marketplace = plugin['id'].split('@', 1)[1]
            follows = seen.key(Surface.PLUGIN, plugin['id'])
            detail = seen.details.get(follows)
            try:
                loaded = _loaded(catalogs, plugin['id'], root)
            except Unread:
                loaded = root   # the flag read below records why
            if isinstance(detail, InstallDetail) and loaded != root:
                seen.details[follows] = replace(detail, loaded=loaded)
            if marketplace == ALIGNED_VIA and plugin['enabled']:
                try:
                    root.stat()
                except FileNotFoundError:
                    seen.unread(root, MissingCache(f'cache not on disk: {roots.tokens(root)}'),
                                {Surface.SKILL}, {plugin['id']}, missing_tree=seen.key(Surface.TREE, plugin['id']))
                    continue
                except OSError:
                    pass
                alignment = root / '.claude-plugin/codex-alignment.json'
                try:
                    excluded = json.loads(portable.read_utf8(alignment)).get('excludedSkillNames', [])
                except (OSError, ValueError) as error:
                    seen.unread(alignment, error, {Surface.SKILL}, {plugin['id']})
                    excluded = []
                for name in excluded:
                    seen.add(Surface.SKILL, f'{plugin["id"]}:{portable_name(name.split(":", 1)[1])}', SkillMode.OFF, follows)
            # A disabled install's flags change nothing, and an off skill keeps its upstream flag there, so
            # disabled installs, like catalogs, are read only for the skills the description names.
            partial = marketplace in CATALOGS or not (plugin['enabled'] or request.everything)
            wanted = named_skills.get(plugin['id'], set()) if partial else None
            if partial and not wanted:
                continue
            try:
                explicit = self._explicit(root, wanted, catalogs, plugin['id'])
                implicit = (named_skills.get(plugin['id'], set()) & skill_paths(loaded).keys()) - explicit
            except Unread as error:
                seen.unread(root, error, {Surface.SKILL}, {plugin['id']},
                            missing_tree=seen.key(Surface.TREE, plugin['id']) if isinstance(error, MissingCache) else None)
                continue
            for skill in explicit:
                key = seen.key(Surface.SKILL, f'{plugin["id"]}:{skill}')
                if key not in seen.facts:
                    seen.add(Surface.SKILL, key.subject, SkillMode.EXPLICIT, follows)
            if marketplace not in CATALOGS:
                for skill in implicit:
                    key = seen.key(Surface.SKILL, f'{plugin["id"]}:{skill}')
                    if key not in seen.facts:
                        seen.add(Surface.SKILL, key.subject, SkillMode.ON, follows)

        # synced/ holds skills the Claude app manages for the account.
        observe_loose(seen, roots.claude_home / 'skills', lambda child: not child.name.startswith('.') and child.name != 'synced')
        rules = roots.claude_home / 'CLAUDE.md'
        try:
            if rules.is_file():
                seen.add(Surface.RULES, 'CLAUDE.md', FileValue(bytes_digest(rules.read_bytes())))
        except OSError as error:
            seen.unread(rules, error, {Surface.RULES})

        installed = {plugin['id']: Path(plugin['installPath']) for plugin in user}
        enabled_trees = {plugin['id']: Path(plugin['installPath']) for plugin in user
                         if plugin['enabled'] and plugin['id'].split('@', 1)[1] not in CATALOGS}
        trees = {plugin: installed[plugin] for plugin in request.trees if plugin in installed}
        observe_trees(seen, {**trees, **(enabled_trees if request.everything else {})},
                      {plugin: (installed[plugin], spec) for plugin, spec in request.overlays.items() if plugin in installed})
        return seen.finish(version, offered)

    def _explicit(self, root: Path, wanted: set[str] | None, catalogs: dict[str, tuple[Path, dict]] | None = None,
                  plugin_id: str = '') -> set[str]:
        """Skills whose SKILL.md frontmatter sets disable-model-invocation, read from the folder Claude Code runs the
        plugin from once its install cache is known to be there."""
        try:
            mode = root.stat().st_mode
        except FileNotFoundError as error:
            raise MissingCache(f'cache not on disk: {self.roots.tokens(root)}') from error
        except OSError as error:
            raise Unread(f'cache unreadable: {self.roots.tokens(root)}: {error}') from error
        if not stat.S_ISDIR(mode):
            raise Unread(f'cache is not a directory: {self.roots.tokens(root)}')
        if catalogs:
            root = _loaded(catalogs, plugin_id, root)
        try:
            return {skill for skill, path in skill_paths(root).items()
                    if (wanted is None or skill in wanted)
                    and frontmatter(read_text(path))[1].get('disable-model-invocation') is True}
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise Unread(f'skills unreadable in {self.roots.tokens(root)}: {error}') from error
