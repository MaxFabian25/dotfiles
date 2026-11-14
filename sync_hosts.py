from __future__ import annotations

import json
import subprocess
import sys
import tomllib

import portable
from accepted_installations import AcceptedInstallations, HeldCache, HeldMove, copy_tree, image
from claude_host import native_marketplace
import codex_runtime
import profile_plugins
from description import EngineInstall, MarketplaceMode, plugin_id, via
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
        self._catalogs()
        for artifact in self.prepared.artifacts:
            try:
                self.owner.apply(artifact, self.files, native=self._install)
            except (HeldCache, HeldMove) as held:
                self.held.append(held)
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
                if spec.mode == MarketplaceMode.CHECKOUT:
                    checkout = self.builder.sources.checkout(repo.url, repo.commit,
                                                             self.roots.engine / '.sync/checkouts' / repo.name)
                    self.command(host, 'plugin', 'marketplace', 'add', str(checkout), '--json')
                else:
                    self.command(host, 'plugin', 'marketplace', 'add', repo.url, '--ref', repo.commit, '--json')
            elif spec.mode == MarketplaceMode.CHECKOUT:
                checkout = self.builder.sources.checkout(repo.url, repo.commit,
                                                         self.roots.engine / '.sync/checkouts' / repo.name)
                self.command(host, 'plugin', 'marketplace', 'add', str(checkout), '--scope', 'user', '--json')
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
        for name, built in self.built.items():
            package = self.setup.packages[name]
            if not package.codex or not any(isinstance(i, EngineInstall) and i.managed for i in package.codex.installs):
                continue
            root = built.root
            if not root.is_relative_to(self.roots.agents_home.parent):
                root = self.roots.agents_home / 'plugins/sources' / name / str(built.digest).removeprefix('sha256:')
                self._stage_source(built.root, root)
            location = {'source': 'local', 'path': './' + root.relative_to(self.roots.agents_home.parent).as_posix()}
            entries.append({'name': name, 'source': location,
                            'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_USE'},
                            'category': 'Developer Tools'})
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
            changed = self.files.json(directory / '.claude-plugin/marketplace.json',
                                      lambda data, n=name, e=entries, m=managed: merge_catalog(data, n, e, m))
            if changed or managed is not None:
                self.command(Host.CLAUDE, 'plugin', 'marketplace', 'add', str(directory), '--scope', 'user', '--json')

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
        for difference in self.current.changes:
            if difference.key is None or difference.key.surface != Surface.SKILL:
                continue
            host, subject, mode = difference.key.host, difference.key.subject, difference.expected
            if host == Host.CODEX:
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
