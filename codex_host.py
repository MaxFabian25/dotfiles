"""Every Codex encoding: plugin ids, skills.config, marketplaces, sidecars, agent roles, hooks.

Codex resolves account plugins itself, so the plugin inventory comes from `codex plugin list`, never
from config.toml; a config entry the CLI contradicts is residue.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
import json
from pathlib import Path
import re
import subprocess
import tomllib
from typing import Sequence

import yaml

from description import (HOST_TOGGLES, RULES, BuildSource, EngineInstall, Hooks, LocalSource, MarketplaceMode, RepoSource, Setup, SkillRoot,
                         ToolNotes, plugin_id, via)
from host_observation import Observer, Unread, checkout, observe_loose, observe_trees, run
from invocation_overrides import skill_paths
from host_state import (CATALOGS as ALL_CATALOGS, ENGINE_MARKETPLACE, Approved, CheckoutMarketplace, DeviceRoots,
                        Commit, EngineMarketplace, Fact, FactKey, FactSet, FileValue, GitMarketplace, Host, InstallDetail,
                        NoteKind, Observation, OverlaySpec, Pinned, PluginValue, Released, Request, SkillMode, Surface,
                        TreeValue, Unpinned, bytes_digest, clone_url, parse_commit)
import portable

_HOST = Host.CODEX
CATALOGS = ALL_CATALOGS[_HOST]
ENGINE = ENGINE_MARKETPLACE[_HOST]
# Codex's record, in a git marketplace's snapshot, of the commit it fetched.
_FETCHED = '.codex-marketplace-install.json'


def _key(surface: Surface, subject: str) -> FactKey:
    return FactKey(_HOST, surface, subject)


def _commit_or_none(text) -> str | None:
    try:
        return parse_commit(text)
    except ValueError:
        return None


def _loose(child: Path) -> bool:
    return child.is_dir() and not child.name.startswith('.')


def _children(folder: Path) -> list[Path]:
    try:
        return [child for child in folder.iterdir() if _loose(child)] if folder.is_dir() else []
    except OSError:
        return []


def _hook_keys(plugin_id: str, root: Path) -> set[str]:
    """The hooks.state keys of the hooks an installed plugin defines, spelled as Codex spells them."""
    path = root / 'hooks/hooks.json'
    if not path.is_file():
        return set()
    events = json.loads(portable.read_utf8(path))['hooks']
    return {f'{plugin_id}:hooks/hooks.json:{re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()}:{group}:{index}'
            for event, groups in events.items() for group, entry in enumerate(groups)
            for index, _ in enumerate(entry['hooks'])}


class CodexState:
    host = _HOST

    def __init__(self, roots: DeviceRoots, cli: str | Sequence[str]) -> None:
        self.roots = roots
        self.cli = [cli] if isinstance(cli, str) else list(cli)

    def project(self, setup: Setup) -> FactSet:
        facts: dict[FactKey, Fact] = {}

        def add(surface, subject, value, entry, follows=None):
            key = _key(surface, subject)
            facts[key] = Fact(key, value, entry, follows)
            return key

        for repo in setup.repos.values():
            if repo.codex:
                value = (CheckoutMarketplace(repo.url, repo.commit) if repo.codex.mode == MarketplaceMode.CHECKOUT
                         else GitMarketplace(repo.url, repo.commit))
                add(Surface.MARKETPLACE, repo.codex.name, value, repo.name)
        for package in setup.packages.values():
            placement = package.codex
            if placement is None:
                continue
            lock = setup.lock.get(package.name)
            tree = lock.trees.get(_HOST) if lock else None
            owner = None
            for install in placement.installs:
                if via(install, _HOST) == ENGINE:
                    add(Surface.MARKETPLACE, ENGINE, EngineMarketplace(), None)
                plugin = plugin_id(package.name, install, _HOST)
                repo = setup.repos[package.source.repo] if isinstance(package.source, RepoSource) else None
                # Codex records no commit for a copy taken from a checkout, only its version.
                copied = repo is not None and repo.codex is not None and repo.codex.mode == MarketplaceMode.CHECKOUT
                if isinstance(install, EngineInstall) or isinstance(package.source, (BuildSource, LocalSource)) or copied:
                    pin = Released(tree.version) if install.enabled and tree else Unpinned()
                elif repo is not None:
                    pin = Pinned(repo.commit)
                else:
                    pin = Unpinned()
                key = add(Surface.PLUGIN, plugin, PluginValue(install.enabled, pin), package.name)
                owner = owner or key
                if not install.enabled:
                    continue
                owner = key
                if tree:
                    add(Surface.TREE, plugin, TreeValue(tree.digest), package.name, key)
                if package.tool_notes == ToolNotes.APPLY or placement.adaptation:
                    rules = setup.note_rules if package.tool_notes == ToolNotes.APPLY else ()
                    explicit = tuple(sorted(s for s, mode in package.modes.items() if mode == SkillMode.EXPLICIT))
                    add(Surface.OVERLAY, plugin, OverlaySpec(rules, placement.adaptation, explicit), package.name, key)
                if placement.hooks == Hooks.TRUSTED and lock and lock.hooks:
                    add(Surface.HOOK, plugin, Approved(lock.hooks), package.name, key)
                for role, digest in (lock.roles if lock else {}).items():
                    add(Surface.AGENT, role, FileValue(digest), package.name, key)
            for skill, mode in package.modes.items():
                add(Surface.SKILL, f'{package.name}:{skill}', mode, package.name, owner)
        for skill in setup.loose.values():
            lock = setup.lock.get(skill.name)
            for root, token in ((SkillRoot.CODEX, '{codex_home}'), (SkillRoot.AGENTS, '{agents_home}')):
                if root not in skill.place:
                    continue
                subject = f'{token}/skills/{skill.name}'
                key = None
                if lock and root in lock.placements:
                    key = add(Surface.LOOSE_SKILL, subject, FileValue(lock.placements[root]), skill.name)
                if not skill.place[root]:
                    add(Surface.SKILL, f'{subject}/SKILL.md', SkillMode.OFF, skill.name, key)
        for subject, mode in setup.host_skills.get(_HOST, {}).items():
            add(Surface.SKILL, subject, mode, HOST_TOGGLES[_HOST])
        if setup.rules is not None:
            add(Surface.RULES, 'AGENTS.md', FileValue(setup.rules.digest), RULES)
        return facts

    def observe(self, request: Request) -> Observation:
        roots = self.roots
        seen = Observer(_HOST, roots)
        try:
            version = run([*self.cli, '--version']).strip()
        except (OSError, subprocess.SubprocessError):
            version = 'unknown'
        listing = 'codex plugin list --available --json'
        try:
            listed = json.loads(run([*self.cli, 'plugin', 'list', '--available', '--json']))
            inventory, available = listed['installed'], listed['available']
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
            seen.unread(listing, error, {Surface.PLUGIN, Surface.TREE, Surface.OVERLAY, Surface.HOOK, Surface.SKILL})
            inventory, available = None, []
        listed = inventory is not None
        inventory = inventory or []
        config_path = roots.codex_home / 'config.toml'
        try:
            config = tomllib.loads(portable.read_utf8(config_path)) if config_path.is_file() else {}
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            seen.unread(config_path, error, {Surface.MARKETPLACE, Surface.PLUGIN, Surface.SKILL, Surface.HOOK})
            config = {}

        installed = {plugin['pluginId']: plugin for plugin in inventory}
        named = defaultdict(list)
        for plugin in inventory:
            named[plugin['name']].append(plugin)
        offered = set(installed) | {plugin['pluginId'] for plugin in available}

        checkouts = set()
        for name, table in config.get('marketplaces', {}).items():
            if name in CATALOGS:
                continue
            source = str(table.get('source', ''))
            if table.get('source_type') == 'git':
                seen.add(Surface.MARKETPLACE, name, GitMarketplace(clone_url(source), _commit_or_none(table.get('ref'))))
                continue
            if table.get('source_type') == 'local':
                checkouts.add(name)
                try:
                    found = checkout(Path(source))
                except Unread as error:
                    seen.unread(source, error, {Surface.MARKETPLACE}, {name})
                    continue
                if found:
                    url, head, clean = found
                    seen.add(Surface.MARKETPLACE, name, CheckoutMarketplace(url, head))
                    if not clean:
                        seen.note(NoteKind.DEVICE_LOCAL, name, f'the checkout at {roots.tokens(source)} has uncommitted changes')
                    continue
            seen.note(NoteKind.UNMODELED, f'marketplaces.{name}', f'{table.get("source_type")} source {roots.tokens(source)}')
        personal = roots.agents_home / 'plugins/marketplace.json'
        catalog = {}
        if personal.is_file():
            try:
                catalog = json.loads(portable.read_utf8(personal))
            except (OSError, ValueError) as error:
                seen.unread(personal, error, {Surface.MARKETPLACE}, {ENGINE})
                catalog = {}
            if catalog.get('name') == ENGINE:
                seen.add(Surface.MARKETPLACE, ENGINE, EngineMarketplace())
                orphans = [entry['name'] for entry in catalog.get('plugins', [])
                           if listed and f'{entry["name"]}@{ENGINE}' not in installed]
                if orphans:
                    seen.note(NoteKind.RESIDUE, ENGINE, f'{len(orphans)} marketplace entries without an install: {", ".join(orphans)}')

        local_sources = {entry['name']: entry['source'] for entry in catalog.get('plugins', [])
                         if catalog.get('name') == ENGINE and isinstance(entry.get('source'), dict)}
        releases: dict[str, Path | Unread] = {}
        for plugin in inventory:
            source = plugin.get('source') or {}
            if source.get('source') != 'local':
                continue
            path = Path(source.get('path', ''))
            if path.is_relative_to(roots.engine) or (path / '.codex-plugin/upstream.json').is_file():
                releases[plugin['pluginId']] = path
                continue
            registered = local_sources.get(plugin['name'], {})
            if (plugin['marketplaceName'] == ENGINE and registered.get('source') == 'local'
                    and path == roots.agents_home.parent / registered.get('path', '')
                    and path.parent == roots.agents_home / 'plugins/sources' / plugin['name']
                    and re.fullmatch(r'[0-9a-f]{64}', path.name)):
                try:
                    if roots.tree_digest(path) == 'sha256:' + path.name:
                        releases[plugin['pluginId']] = path
                except OSError as error:
                    releases[plugin['pluginId']] = Unread(f'The staged engine source is unreadable: {error}')

        fetched: dict[Path, Commit] = {}

        def pin(plugin) -> Pinned | Released | Unpinned:
            marketplace, source = plugin['marketplaceName'], plugin.get('source') or {}
            path = Path(source.get('path', ''))
            if marketplace in CATALOGS:
                return Unpinned()
            release = releases.get(plugin['pluginId'])
            if isinstance(release, Unread):
                raise release
            if release is not None:
                return Released(plugin['version'])
            if source.get('source') in ('git', 'git-subdir') and (commit := _commit_or_none(source.get('ref'))):
                return Pinned(commit)
            if source.get('source') == 'local' and marketplace not in checkouts:
                record = next((folder / _FETCHED for folder in (path, *path.parents)
                               if (folder / _FETCHED).is_file() and folder.is_relative_to(roots.codex_home)), None)
                if record is None:
                    directory = roots.codex_home / '.tmp/marketplaces' / marketplace
                    table = config.get('marketplaces', {}).get(marketplace, {})
                    found = checkout(directory) if path.resolve().is_relative_to(directory.resolve()) else None
                    if found and table.get('source_type') == 'git':
                        url, head, clean = found
                        if url != clone_url(table.get('source', '')):
                            raise Unread(f'The fetched marketplace has a different origin for {marketplace}')
                        if not clean:
                            raise Unread(f'The fetched marketplace has local changes for {marketplace}')
                        return Pinned(head)
                    raise Unread(f'Codex keeps no record of the commit it fetched for {marketplace}')
                if record not in fetched:
                    try:
                        fetched[record] = parse_commit(json.loads(portable.read_utf8(record))['revision'])
                    except (OSError, ValueError, KeyError, TypeError) as error:
                        raise Unread(f'{roots.tokens(record)} unreadable: {error}') from error
                return Pinned(fetched[record])
            # A checkout's copies and branch-following sources: Codex records only the installed version.
            return Released(plugin['version'])

        hooks: dict[str, set[str]] = {}
        for plugin in inventory:
            if plugin['marketplaceName'] not in CATALOGS:
                try:
                    hooks[plugin['pluginId']] = _hook_keys(plugin['pluginId'], self._cache(plugin))
                except (OSError, ValueError, TypeError, AttributeError) as error:
                    seen.unread(self._cache(plugin) / 'hooks/hooks.json', error, {Surface.HOOK}, {plugin['pluginId']})
        for plugin in inventory:
            source = plugin.get('source') or {}
            detail = InstallDetail(plugin['version'], self._cache(plugin), hooks=len(hooks.get(plugin['pluginId'], ())))
            if source.get('source') in ('git', 'git-subdir'):
                detail = replace(detail, url=clone_url(source.get('url', '')), path=source.get('path'))
            elif isinstance(releases.get(plugin['pluginId']), Path):
                detail = replace(detail, release=releases[plugin['pluginId']])
            try:
                value = PluginValue(plugin['enabled'], pin(plugin))
            except Unread as error:
                seen.unread(source.get('path', ''), error, {Surface.PLUGIN}, {plugin['pluginId']})
                continue
            seen.add(Surface.PLUGIN, plugin['pluginId'], value, detail=detail)
        for plugin_id, table in config.get('plugins', {}).items():
            if 'mcp_servers' in table:
                seen.note(NoteKind.UNMODELED, f'plugins."{plugin_id}".mcp_servers', ', '.join(sorted(table['mcp_servers'])))
            if 'enabled' not in table or not listed:
                continue
            if plugin_id not in installed:
                seen.note(NoteKind.RESIDUE, plugin_id, 'config.toml has an entry for a plugin Codex does not have')
            elif installed[plugin_id]['enabled'] != table['enabled']:
                seen.note(NoteKind.RESIDUE, plugin_id, f'config.toml says {"on" if table["enabled"] else "off"}, '
                                                       f'Codex reports {"on" if installed[plugin_id]["enabled"] else "off"}')

        def owner(name):
            plugins = named.get(name, [])
            chosen = next((p for p in plugins if p['enabled']), plugins[0] if plugins else None)
            return seen.key(Surface.PLUGIN, chosen['pluginId']) if chosen else None

        def toggle(subject: str, mode: SkillMode, follows: FactKey | None = None) -> None:
            known = seen.facts.get(seen.key(Surface.SKILL, subject))
            seen.add(Surface.SKILL, subject, SkillMode.OFF if known and known.value == SkillMode.OFF else mode, follows)

        loose_homes = defaultdict(list)
        for folder in (roots.codex_home / 'skills', roots.agents_home / 'skills'):
            for child in _children(folder):
                loose_homes[child.name].append(roots.tokens(child / 'SKILL.md'))
        stale_toggles, redundant, local_toggles = Counter(), 0, 0
        for entry in config.get('skills', {}).get('config', []):
            mode = SkillMode.ON if entry.get('enabled') else SkillMode.OFF
            if 'path' in entry:
                path = Path(entry['path'])
                if path.is_relative_to(roots.codex_home) or path.is_relative_to(roots.agents_home):
                    toggle(roots.tokens(path), mode)
                else:
                    local_toggles += 1
                continue
            name = entry.get('name', '')
            plugin, _, skill = name.partition(':')
            if not skill:
                for subject in loose_homes.get(name) or [name]:
                    toggle(subject, mode)
            elif not listed:
                continue
            elif plugin not in named:
                stale_toggles[plugin] += 1
            elif mode == SkillMode.OFF:
                seen.add(Surface.SKILL, name, mode, owner(plugin))
            elif any(p['marketplaceName'] in CATALOGS for p in named[plugin]):
                seen.add(Surface.SKILL, name, mode, owner(plugin))
            else:
                redundant += 1
        if stale_toggles:
            seen.note(NoteKind.RESIDUE, 'skills.config', f'{sum(stale_toggles.values())} toggles for {len(stale_toggles)} plugins '
                                                         f'Codex no longer has: {", ".join(sorted(stale_toggles))}')
        if redundant:
            seen.note(NoteKind.RESIDUE, 'skills.config', f'{redundant} enables on skills of pinned plugins, which are on by default')

        named_skills = defaultdict(set)
        for subject in request.skills:
            plugin, _, skill = subject.partition(':')
            if skill:
                named_skills[plugin].add(skill)
        enabled_names = {plugin['name'] for plugin in inventory if plugin['enabled']}
        for plugin in inventory:
            # Codex keys skill flags by plugin name, not plugin id.
            if not plugin['enabled'] and plugin['name'] in enabled_names:
                continue
            root = self._cache(plugin)
            partial = plugin['marketplaceName'] in CATALOGS or not (plugin['enabled'] or request.everything)
            wanted = named_skills.get(plugin['name'], set()) if partial else None
            if partial and not wanted:
                continue
            try:
                explicit = self._explicit(root, wanted)
            except Unread as error:
                seen.unread(root, error, {Surface.SKILL}, {plugin['pluginId']})
                continue
            for skill in explicit:
                key = seen.key(Surface.SKILL, f'{plugin["name"]}:{skill}')
                if key not in seen.facts:
                    seen.add(Surface.SKILL, key.subject, SkillMode.EXPLICIT, seen.key(Surface.PLUGIN, plugin['pluginId']))

        agents = roots.codex_home / 'agents'
        try:
            for path in sorted(agents.glob('*.toml')) if agents.is_dir() else ():
                seen.add(Surface.AGENT, path.name, FileValue(roots.file_digest(path)))
        except OSError as error:
            seen.unread(agents, error, {Surface.AGENT})
        state = config.get('hooks', {}).get('state', {})
        approved = defaultdict(set)
        for name, value in state.items():
            if value.get('enabled') and value.get('trusted_hash'):
                approved[name.split(':', 1)[0]].add(name)
        stray = 0
        for plugin_id, names in approved.items():
            defined = hooks.get(plugin_id, set())
            stray += len(names - defined)
            if names & defined:
                seen.add(Surface.HOOK, plugin_id, Approved(len(names & defined)), seen.key(Surface.PLUGIN, plugin_id))
        if stray:
            seen.note(NoteKind.RESIDUE, 'hooks.state', f'{stray} approvals of hooks no installed plugin defines')
        for folder in (roots.codex_home / 'skills', roots.agents_home / 'skills'):
            observe_loose(seen, folder, _loose)
        rules = roots.codex_home / 'AGENTS.md'
        try:
            if rules.is_file():
                seen.add(Surface.RULES, 'AGENTS.md', FileValue(bytes_digest(rules.read_bytes())))
        except OSError as error:
            seen.unread(rules, error, {Surface.RULES})

        for count, what in ((len(config.get('projects', {})), 'project trusts'), (local_toggles, 'project skill toggles'),
                            (len(state), 'hook trust hashes'), (len(config.get('mcp_servers', {})), 'MCP servers'),
                            (len(config.get('apps', {})), 'app connectors')):
            if count:
                seen.note(NoteKind.DEVICE_LOCAL, what, f'{count} {what}')

        enabled = {plugin_id: self._cache(plugin) for plugin_id, plugin in installed.items()
                   if plugin['enabled'] and plugin['marketplaceName'] not in CATALOGS}
        trees = {plugin_id: self._cache(installed[plugin_id]) for plugin_id in request.trees if plugin_id in installed}
        observe_trees(seen, {**trees, **(enabled if request.everything else {})},
                      {plugin_id: (self._cache(installed[plugin_id]), spec) for plugin_id, spec in request.overlays.items()
                       if plugin_id in installed})
        return seen.finish(version, offered)

    def _cache(self, plugin) -> Path:
        return self.roots.codex_home / 'plugins/cache' / plugin['marketplaceName'] / plugin['name'] / plugin['version']

    def _explicit(self, root: Path, wanted: set[str] | None) -> set[str]:
        """Skills whose agents/openai.yaml sidecar turns implicit invocation off."""
        if not root.is_dir():
            raise Unread(f'cache not on disk: {self.roots.tokens(root)}')
        explicit = set()
        try:
            for skill, path in skill_paths(root).items():
                sidecar = path.parent / 'agents/openai.yaml'
                if (wanted is None or skill in wanted) and sidecar.is_file():
                    data = yaml.safe_load(portable.read_utf8(sidecar)) or {}
                    policy = data.get('policy') or {} if isinstance(data, dict) else None
                    if not isinstance(policy, dict):
                        raise ValueError(f'{skill}: agents/openai.yaml has no policy table')
                    if policy.get('allow_implicit_invocation') is False:
                        explicit.add(skill)
        except (OSError, ValueError, yaml.YAMLError) as error:
            raise Unread(f'skills unreadable in {self.roots.tokens(root)}: {error}') from error
        return explicit
