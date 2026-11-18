from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tomllib

import tomlkit

import portable
from accepted_installations import AcceptedInstallations, HeldMove, _linked, copy_tree, image
from claude_host import native_marketplace
from codex_host import EVERY_SKILL
import codex_runtime
import profile_plugins
from description import EngineInstall, plugin_id, via
from device_sync import changed_packages
from host_observation import Unread
from host_state import EngineMarketplace, FactKey, Host, PluginValue, Released, SkillMode, Surface, clone_url
from sync_files import Files, ConcurrentEdit, RehearsalFiles, backup_directory
from sync_sources import child


class Apply:
    def __init__(self, setup, device, current, builder):
        self.setup, self.device, self.current, self.builder = setup, device, current, builder
        self.roots = device.roots
        self.files = Files(backup_directory(self.roots))
        # Each engine catalog that differs from what the description wants -> the plugins it must list.
        self.catalogs = {(d.key.host, d.key.subject): d.expected.plugins for d in current.changes
                         if d.key is not None and d.key.surface == Surface.MARKETPLACE and isinstance(d.expected, EngineMarketplace)}
        catalog_packages = {p.name for p in setup.packages.values() for host in Host
                            if p.placement(host) and any((host, via(i, host)) in self.catalogs and i.managed
                                                        for i in p.placement(host).installs)}
        self.packages = (changed_packages(current) | catalog_packages) & setup.packages.keys()
        self.owner = AcceptedInstallations(setup, device, configurations=current.configurations)
        self.held = []

    def prepare(self):
        self.prepared = self.owner.prepare(self.current.observations, self.builder, packages=self.packages)
        self.built, self.claude = self.prepared.built, self.prepared.claude

    def run(self):
        self.owner.preflight(self.prepared)
        profile_plugins.preflight(self.current.profile_mirrors)
        self._marketplaces()
        self._runtime()
        self.owner.preflight(self.prepared)
        self._catalogs()
        for artifact in self.prepared.artifacts:
            try:
                self.owner.apply(artifact, self.files, native=self._install, catalog=self._point)
            except HeldMove as held:
                self.held.append(held)
        # The loop writes the mise config after its plugin installs, and Codex runs through a launcher that asks mise
        # for the pinned Node, so the new pins install before the next Codex command and the plan after this run.
        if self.device.install_tools is not None:
            try:
                self.device.install_tools()
            except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                print(f'Sync: the tools the mise config pins did not install: {error}', file=sys.stderr, flush=True)
        self._plugins()
        self._skills()
        # Planned again, because the steps above can replace the accepted ~/.codex copy the homes mirror.
        mirrors, _, _ = profile_plugins.inspect(self.setup, self.roots,
                                                self.setup.account_homes.get(self.device.device_list, ()), self.owner.accepted)
        profile_plugins.apply(mirrors, self.roots, self.owner.accepted, self.files)

    def command(self, host, *args):
        state = self.device.host(host)
        protected = [self.roots.codex_home / 'config.toml'] if host == Host.CODEX else [
            self.roots.claude_home / 'settings.json',
            self.roots.claude_home / 'plugins/installed_plugins.json',
            self.roots.claude_home / 'plugins/known_marketplaces.json']
        for path in protected:
            self.files.remember(path)
        environment = portable.native_cli_environment()
        environment['CODEX_HOME'] = str(self.roots.codex_home)
        environment['CLAUDE_CONFIG_DIR'] = str(self.roots.claude_home)
        try:
            return portable.checked_output([*state.cli, *args], text=True, timeout=180,
                                           cwd=self.roots.home, env=environment)
        except subprocess.CalledProcessError as error:
            said = (error.stderr or error.stdout).decode('utf-8', 'replace').replace('\r\n', '\n').replace('\r', '\n')
            raise RuntimeError(f'{host.value} {" ".join(args[:3])} failed: {said[-2000:]}') from error

    def _marketplaces(self):
        for difference in self.current.changes:
            if difference.key is None or difference.key.surface != Surface.MARKETPLACE or difference.expected is None:
                continue
            if difference.entry is None or difference.entry.name not in self.setup.repos:
                continue
            repo = self.setup.repos[difference.entry.name]
            host = difference.key.host
            spec = repo.marketplace(host)
            if host == Host.CODEX:
                self.command(host, 'plugin', 'marketplace', 'add', repo.url, '--ref', repo.commit, '--json')
            else:
                self._claude_git_marketplace(repo, spec)

    def _runtime(self):
        """The bundle, then its catalog, before any plugin from it. The plan infers registration from the plugins
        Codex lists, installed ones included, so it can be wrong. The catalog is therefore registered whenever the
        runtime changes, and registering it again at the same path is harmless."""
        if not any(d.key == codex_runtime.KEY for d in self.current.changes):
            return
        catalog = codex_runtime.install(self.setup.runtime, self.roots)
        self.command(Host.CODEX, 'plugin', 'marketplace', 'add', str(catalog), '--json')

    def _claude_git_marketplace(self, repo, spec):
        settings_path = self.roots.claude_home / 'settings.json'
        registry_path = self.roots.claude_home / 'plugins/known_marketplaces.json'

        def registration():
            settings = json.loads(settings_path.read_bytes()) if settings_path.exists() else {}
            registry = json.loads(registry_path.read_bytes()) if registry_path.exists() else {}
            configured = settings.get('extraKnownMarketplaces', {}).get(spec.name, {})
            registered = registry.get(spec.name)
            try:
                native = native_marketplace(self.roots, spec.name, configured, registered)
            except Unread as error:
                raise ConcurrentEdit(str(error)) from error
            if native and native.url != clone_url(repo.url):
                raise ConcurrentEdit(f'Native marketplace has a different source: {spec.name}')
            return configured, registered, native

        configured, registered, native = registration()
        if native and native.pinned and native.commit == repo.commit:
            return
        if native is None:
            destination = self.roots.claude_home / 'plugins/marketplaces' / spec.name
            if configured or destination.exists() or destination.is_symlink():
                raise ConcurrentEdit(f'Unregistered native marketplace state preserved: {spec.name}')
            sparse = ('--sparse', *spec.sparse) if spec.sparse else ()
            self.command(Host.CLAUDE, 'plugin', 'marketplace', 'add', repo.url, '--scope', 'user', '--json', *sparse)
            configured, registered, native = registration()
            if native is None:
                raise RuntimeError(f'Native marketplace registration is missing: {spec.name}')

        def pin_source(data, *, settings):
            entries = data.get('extraKnownMarketplaces', {}) if settings else data
            expected = configured if settings else registered
            if entries.get(spec.name) != expected:
                raise ConcurrentEdit(f'Native marketplace registration changed: {spec.name}')
            entries[spec.name]['source']['ref'] = repo.commit
            entries[spec.name]['autoUpdate'] = False

        self.files.json(settings_path, lambda data: pin_source(data, settings=True))
        self.files.json(registry_path, lambda data: pin_source(data, settings=False))
        _, _, native = registration()
        self.builder.sources.checkout(repo.url, repo.commit, native.root)
        _, _, actual = registration()
        if not actual.pinned or actual.commit != repo.commit:
            raise RuntimeError(f'Native marketplace did not retain its exact pin: {spec.name}')

    def _catalogs(self):
        """Each engine catalog gets the entries of the packages built in this run. One that differs from what the
        description wants is rewritten to list exactly its managed plugins, so a package that is no longer managed
        leaves it, even when nothing else changed."""
        entries = []
        for name in self.built:
            package = self.setup.packages[name]
            install = next((i for i in package.codex.installs if isinstance(i, EngineInstall) and i.managed), None
                           ) if package.codex else None
            if install is None:
                continue
            identity = f'plugin:{Host.CODEX}:{plugin_id(name, install, Host.CODEX)}'
            source = self.owner.catalog_source(identity)
            if source is None and self.owner.names_later(identity):
                continue
            entries.append(self._entry(name, source))
        managed = self.catalogs.get((Host.CODEX, 'personal'))
        if entries or managed is not None:
            changed = self.files.json(self.roots.agents_home / 'plugins/marketplace.json',
                                      lambda data: merge_catalog(data, 'personal', entries, managed))
            if changed or managed is not None:
                self.command(Host.CODEX, 'plugin', 'marketplace', 'add', str(self.roots.agents_home.parent), '--json')
        directories = {'codex-aligned': self.roots.claude_home / 'marketplaces/codex-aligned',
                       'personal-collections': self.roots.engine / 'claude'}
        catalogs = {marketplace: [] for marketplace in directories}
        for identity, built in self.claude.items():
            name, marketplace = identity.split('@', 1)
            if marketplace not in directories:
                continue
            catalog = directories[marketplace]
            destination = catalog / 'plugins' / name / built.version
            if destination != built.root:
                self._stage_source(built.root, destination)
            catalogs[marketplace].append(
                {'name': name, 'source': './' + destination.relative_to(catalog).as_posix(), 'version': built.version})
        for name, entries in catalogs.items():
            managed = self.catalogs.get((Host.CLAUDE, name))
            if not entries and managed is None:
                continue
            directory = directories[name]
            path = directory / '.claude-plugin/marketplace.json'
            if name == 'codex-aligned' and path in self.prepared.catalogs:
                before = self.prepared.catalogs[path]
                data = json.loads(before) if before is not None else {}
                original = json.dumps(data, sort_keys=True)
                merge_catalog(data, name, entries, managed)
                after = (before if json.dumps(data, sort_keys=True) == original else
                         (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode())
                changed = self.files.write(path, after, expected=before)
            else:
                changed = self.files.json(path, lambda data, n=name, e=entries, m=managed: merge_catalog(data, n, e, m))
            if changed or managed is not None:
                self.command(Host.CLAUDE, 'plugin', 'marketplace', 'add', str(directory), '--scope', 'user', '--json')

    def _entry(self, name, source=None):
        """A personal catalog entry that names source, or, when None, the release built in this run."""
        if source is None:
            built = self.built[name]
            source = built.root
            if not source.is_relative_to(self.roots.agents_home.parent):
                source = self.roots.agents_home / 'plugins/sources' / name / str(built.digest).removeprefix('sha256:')
                self._stage_source(built.root, source)
        location = {'source': 'local', 'path': './' + source.relative_to(self.roots.agents_home.parent).as_posix()}
        return {'name': name, 'source': location, 'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_USE'},
                'category': 'Developer Tools'}

    def _point(self, plugin, source):
        """Name source, or the release when None, as the plugin's source in the personal catalog, once the plugin's
        folder is in place. Codex compares the version that source gives with the name of the plugin's highest folder
        whenever a client lists plugins, and where they differ it installs the source and deletes the plugin's other
        folders. The plugin's other staged sources go."""
        name = plugin.split('@', 1)[0]
        entry = self._entry(name, source)
        if self.files.json(self.roots.agents_home / 'plugins/marketplace.json',
                           lambda data: merge_catalog(data, 'personal', [entry])):
            self.command(Host.CODEX, 'plugin', 'marketplace', 'add', str(self.roots.agents_home.parent), '--json')
        named = self.roots.agents_home.parent / entry['source']['path']
        folder = self.roots.agents_home / 'plugins/sources' / name
        for path in sorted(folder.iterdir()) if folder.is_dir() else ():
            if path == named or not re.fullmatch('[0-9a-f]{64}', path.name):
                continue
            try:
                if _linked(path) or not path.is_dir() or self.roots.tree_digest(path) != 'sha256:' + path.name:
                    reason = 'it is not the tree its name is the digest of'
                else:
                    shutil.rmtree(path)
                    continue
            except OSError as error:
                reason = error
            print(f'Staged source left in place: {path}: {reason}', file=sys.stderr)

    @staticmethod
    def _stage_source(source, destination):
        expected = image(source)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            copy_tree(source, destination)
        if image(destination) != expected:
            raise ConcurrentEdit(f'Cached immutable source was edited: {destination}')

    def _install(self, host, identity):
        existing = self.current.observations[host].facts.get(FactKey(host, Surface.PLUGIN, identity))
        # `codex plugin add` and `claude plugin install` switch the plugin on, possibly before they fail.
        kept = any(plugin_id(name, install, host) == identity and install.managed and not install.enabled
                   for name, package in self.setup.packages.items() if package.placement(host)
                   for install in package.placement(host).installs)
        try:
            if host == Host.CODEX:
                self.command(host, 'plugin', 'add', identity, '--json')
            else:
                self.command(host, 'plugin', 'update' if existing else 'install', identity, '--scope', 'user', '--json')
        except BaseException:
            if kept:
                try:
                    if self._has_switch(host, identity):
                        self._enabled(host, identity, False)
                except Exception as failed:
                    # Printed, so that the sync stops with the native command's error and not with this one.
                    print(f'{host.value}: could not switch {identity} off after its failed install, so it may still be '
                          f'on: {type(failed).__name__}: {failed}', file=sys.stderr)
            raise
        if kept:
            self._enabled(host, identity, False)

    def _has_switch(self, host, identity):
        if host == Host.CODEX:
            path, parse, table = self.roots.codex_home / 'config.toml', tomllib.loads, 'plugins'
        else:
            path, parse, table = self.roots.claude_home / 'settings.json', json.loads, 'enabledPlugins'
        return path.is_file() and identity in parse(portable.read_utf8(path)).get(table, {})

    def _plugins(self):
        wanted = {d.key: d.expected for d in self.current.changes if d.key and d.key.surface == Surface.PLUGIN}
        for state in self.device.hosts:
            host = state.host
            observation = self.current.observations[host]
            for name in sorted(self.packages):
                package = self.setup.packages[name]
                placement = package.placement(host)
                if placement is None:
                    continue
                for install in placement.installs:
                    identity = plugin_id(name, install, host)
                    key = FactKey(host, Surface.PLUGIN, identity)
                    existing = observation.facts.get(key)
                    if not install.enabled:
                        if existing and (existing.value.enabled or existing.value.file_on):
                            self._enabled(host, identity, False)
                        continue
                    if package.source is None and (existing is None or _behind(wanted.get(key), existing.value)):
                        self._install(host, identity)
                    if existing is None or not existing.value.enabled:
                        self._enabled(host, identity, True)

    def _enabled(self, host, identity, enabled):
        if host == Host.CODEX:
            self.files.toml(self.roots.codex_home / 'config.toml', lambda data: set_plugin_enabled(data, identity, enabled))
        else:
            self.files.json(self.roots.claude_home / 'settings.json',
                            lambda data: data.setdefault('enabledPlugins', {}).__setitem__(identity, enabled))

    def _skills(self):
        # Codex applies the last row that matches a skill, so a path row that turns an entry on goes after the name rows.
        for difference in sorted(self.current.changes, key=lambda d: d.key is not None and d.key.host == Host.CODEX
                                 and d.key.subject.startswith('{') and d.expected == SkillMode.ON):
            if difference.key is None or difference.key.surface != Surface.SKILL:
                continue
            host, subject, mode = difference.key.host, difference.key.subject, difference.expected
            if host == Host.CODEX and subject.endswith(':' + EVERY_SKILL):
                self._every_skill_off(self.current.observations[host].details[difference.key].names)
            elif host == Host.CODEX:
                is_path = subject.startswith('{')
                name = str(self.expand(subject)) if is_path else subject
                field = 'path' if is_path else 'name'

                def change(data, field=field, name=name, mode=mode):
                    skills = data.setdefault('skills', {})
                    current = list(skills.get('config', []))
                    matching = [i for i in current if i.get(field) == name]
                    wanted = mode in (SkillMode.OFF, SkillMode.ON)
                    if wanted and len(matching) == 1 and matching[0].get('enabled') == (mode == SkillMode.ON):
                        return
                    current = [i for i in current if i.get(field) != name]
                    if wanted:
                        current.append({field: name, 'enabled': mode == SkillMode.ON})
                    skills['config'] = current
                self.files.toml(self.roots.codex_home / 'config.toml', change)
            else:
                name = subject
                if '@' in subject:
                    identity, _, skill = subject.partition(':')
                    if identity.endswith('@codex-aligned'):
                        continue
                    name = identity.split('@')[0] + ':' + skill

                def change(data, name=name, mode=mode):
                    overrides = data.setdefault('skillOverrides', {})
                    if mode is None:
                        overrides.pop(name, None)
                    else:
                        overrides[name] = 'user-invocable-only' if mode == SkillMode.EXPLICIT else mode.value
                self.files.json(self.roots.claude_home / 'settings.json', change)

    def _every_skill_off(self, names):
        """An off row for each name the check gave a plugin's `:*` fact, and every other row of that name switched off.
        Each new row is a [[skills.config]] table added to the others, which stay as they are."""
        path = self.roots.codex_home / 'config.toml'

        def change(data):
            rows = skill_tables(data)
            for name in sorted(names):
                matching = [row for row in rows if row.get('name') == name]
                for row in matching:
                    if row.get('enabled') is not False:
                        row['enabled'] = False
                if not matching:
                    row = tomlkit.table()
                    row.add('name', name)
                    row.add('enabled', False)
                    row.add(tomlkit.nl())
                    rows.append(row)
            # Read back before the write: tomlkit 0.15.2 loses edits to [[skills.config]] tables that another table
            # splits, and an inline skills table cannot take tables (both tried on 2026-11-14).
            try:
                written = tomllib.loads(data.as_string()).get('skills', {}).get('config', [])
            except tomllib.TOMLDecodeError as error:
                raise ValueError(f'{path}: the off rows would leave TOML that does not parse: {error}') from error
            if any({row.get('enabled') for row in written if row.get('name') == name} != {False} for name in names):
                raise ValueError(f'{path}: the off rows did not land; put the [[skills.config]] tables together')
        self.files.toml(path, change)

    def expand(self, value):
        for token in ('home', 'codex_home', 'claude_home', 'agents_home', 'engine'):
            prefix = '{' + token + '}'
            if value == prefix or value.startswith(prefix + '/'):
                return child(getattr(self.roots, token), value[len(prefix):].lstrip('/'))
        raise ValueError(f'Not a managed device-relative path: {value}')


class Rehearsal(Apply):
    """The pull's plugin and skill switches, run on this device's real settings with nothing written and no native
    command run. They come after the build in a pull, so without this a crash in one first shows once the release is
    pushed. Marketplaces, catalogs and installed files are not rehearsed here."""

    def __init__(self, setup, device, current):
        super().__init__(setup, device, current, builder=None)
        self.files = RehearsalFiles()

    def command(self, host, *args):
        return ''

    def run(self):
        self._plugins()
        self._skills()


def _behind(wanted: PluginValue | None, installed: PluginValue) -> bool:
    """A catalog plugin the description pins by version, held here at another version. Codex keeps an installed
    plugin when its catalog moves, and adding it again installs the catalog's version. An Unpinned expectation never
    counts, so a version Codex reports for an app-managed catalog never causes a re-add."""
    return wanted is not None and isinstance(wanted.pin, Released) and installed.pin != wanted.pin


def set_plugin_enabled(data, identity, enabled):
    """When config.toml's [plugins."…"] tables are spread out, tomlkit hands back a proxy, and a key set on a table
    the proxy has just created is lost, leaving an empty header. So a new table is inserted with its value."""
    plugins = data.setdefault('plugins', {})
    if identity in plugins:
        plugins[identity]['enabled'] = enabled
    else:
        plugins[identity] = {'enabled': enabled}


def skill_tables(data):
    """config.toml's skills.config as [[skills.config]] tables to add rows to. An array written on one line, such as
    the `config = []` a write of an empty list leaves, becomes tables first, because a table added to it cannot be
    written back as TOML."""
    skills = data.setdefault('skills', {})
    rows = skills.get('config')
    if isinstance(rows, tomlkit.items.AoT):
        # The last table of a file without a final newline has none, and a table added after it would start on
        # its last line.
        if rows and not rows[-1].as_string().endswith('\n'):
            rows[-1].add(tomlkit.nl())
        return rows
    tables = tomlkit.aot()
    for row in rows or ():
        table = tomlkit.table()
        for field, value in row.items():
            table.add(field, value)
        tables.append(table)
    skills['config'] = tables
    return tables


def merge_catalog(data, name, entries, managed=None):
    """Adds or updates each entry. With `managed`, the names the catalog must list, every other entry goes."""
    if data.get('name', name) != name:
        raise RuntimeError(f'Refusing to overwrite a different marketplace with {name}')
    data.setdefault('name', name)
    if name == 'personal':
        data.setdefault('interface', {'displayName': 'Personal'})
    else:
        data.setdefault('owner', {'name': 'Dotfiles'})
    current = data.setdefault('plugins', [])
    if managed is not None:
        current[:] = [i for i in current if i.get('name') in managed]
    for entry in entries:
        previous = next((i for i in current if i.get('name') == entry['name']), None)
        if previous is None:
            current.append(entry)
        else:
            previous.update(entry)
