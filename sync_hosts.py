from __future__ import annotations

from pathlib import Path
import json
import subprocess
import shutil

import portable
from accepted_installations import AcceptedInstallations, image
from claude_host import native_marketplace
from description import EngineInstall, MarketplaceMode, plugin_id, via
from device_sync import changed_packages
from host_observation import Unread
from host_state import EngineMarketplace, FactKey, Host, SkillMode, Surface, clone_url
from sync_files import Files, ConcurrentEdit, backup_directory
from sync_sources import child


class Apply:
    def __init__(self, setup, device, current, builder):
        self.setup, self.device, self.current, self.builder = setup, device, current, builder
        self.roots = device.roots
        self.files = Files(backup_directory(self.roots))
        catalogs = {(d.key.host, d.key.subject) for d in current.changes
                    if d.key is not None and d.key.surface == Surface.MARKETPLACE and isinstance(d.expected, EngineMarketplace)}
        self.catalogs_needed = catalogs
        catalog_packages = {p.name for p in setup.packages.values() for host in Host
                            if p.placement(host) and any((host, via(i, host)) in catalogs and i.enabled
                                                        for i in p.placement(host).installs)}
        self.packages = (changed_packages(current) | catalog_packages) & setup.packages.keys()
        self.owner = AcceptedInstallations(setup, device, configurations=current.configurations)

    def prepare(self):
        self.prepared = self.owner.prepare(self.current.observations, self.builder, packages=self.packages)
        self.built, self.claude = self.prepared.built, self.prepared.claude

    def run(self):
        self.owner.preflight(self.prepared)
        self._marketplaces()
        self._catalogs()
        for artifact in self.prepared.artifacts:
            self.owner.apply(artifact, self.files, native=self._install)
        self._plugins()
        self._skills()

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
        result = subprocess.run([*state.cli, *args], capture_output=True, text=True, encoding='utf-8', timeout=180,
                                cwd=self.roots.home, env=environment)
        if result.returncode:
            raise RuntimeError(f'{host.value} {" ".join(args[:3])} failed: {(result.stderr or result.stdout)[-2000:]}')
        return result.stdout

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
        entries = []
        for name, built in self.built.items():
            package = self.setup.packages[name]
            if not package.codex or not any(isinstance(i, EngineInstall) and i.enabled for i in package.codex.installs):
                continue
            root = built.root
            if not root.is_relative_to(self.roots.agents_home.parent):
                root = self.roots.agents_home / 'plugins/sources' / name / str(built.digest).removeprefix('sha256:')
                self._stage_source(built.root, root)
            location = {'source': 'local', 'path': './' + root.relative_to(self.roots.agents_home.parent).as_posix()}
            entries.append({'name': name, 'source': location,
                            'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_USE'},
                            'category': 'Developer Tools'})
        if entries:
            changed = self.files.json(self.roots.agents_home / 'plugins/marketplace.json',
                                      lambda data: merge_catalog(data, 'personal', entries))
            if changed or (Host.CODEX, 'personal') in self.catalogs_needed:
                self.command(Host.CODEX, 'plugin', 'marketplace', 'add', str(self.roots.agents_home.parent), '--json')
        catalogs = {}
        for identity, built in self.claude.items():
            name, marketplace = identity.split('@', 1)
            if marketplace not in ('codex-aligned', 'personal-collections'):
                continue
            catalog = (self.roots.claude_home / 'marketplaces/codex-aligned' if marketplace == 'codex-aligned'
                       else self.roots.engine / 'claude')
            destination = catalog / 'plugins' / name / built.version
            if destination != built.root:
                self._stage_source(built.root, destination)
            catalogs.setdefault((marketplace, catalog), []).append(
                {'name': name, 'source': './' + destination.relative_to(catalog).as_posix(), 'version': built.version})
        for (name, directory), entries in catalogs.items():
            changed = self.files.json(directory / '.claude-plugin/marketplace.json',
                                      lambda data, n=name, e=entries: merge_catalog(data, n, e))
            if changed or (Host.CLAUDE, name) in self.catalogs_needed:
                self.command(Host.CLAUDE, 'plugin', 'marketplace', 'add', str(directory), '--scope', 'user', '--json')

    @staticmethod
    def _stage_source(source, destination):
        expected = image(source)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, destination)
        if image(destination) != expected:
            raise ConcurrentEdit(f'Cached immutable source was edited: {destination}')

    def _install(self, host, identity):
        existing = self.current.observations[host].facts.get(FactKey(host, Surface.PLUGIN, identity))
        if host == Host.CODEX:
            self.command(host, 'plugin', 'add', identity, '--json')
        else:
            self.command(host, 'plugin', 'update' if existing else 'install', identity, '--scope', 'user', '--json')

    def _plugins(self):
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
                        if existing and existing.value.enabled:
                            self._enabled(host, identity, False)
                        continue
                    if package.source is None and existing is None:
                        self._install(host, identity)
                    if existing is None or not existing.value.enabled:
                        self._enabled(host, identity, True)

    def _enabled(self, host, identity, enabled):
        if host == Host.CODEX:
            self.files.toml(self.roots.codex_home / 'config.toml',
                            lambda data: data.setdefault('plugins', {}).setdefault(identity, {}).__setitem__('enabled', enabled))
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
                    if mode in (SkillMode.OFF, SkillMode.ON):
                        overrides[name] = mode.value
                    else:
                        overrides.pop(name, None)
                self.files.json(self.roots.claude_home / 'settings.json', change)

    def expand(self, value):
        for token in ('home', 'codex_home', 'claude_home', 'agents_home', 'engine'):
            prefix = '{' + token + '}'
            if value == prefix or value.startswith(prefix + '/'):
                return child(getattr(self.roots, token), value[len(prefix):].lstrip('/'))
        raise ValueError(f'Not a managed device-relative path: {value}')

def merge_catalog(data, name, entries):
    if data.get('name', name) != name:
        raise RuntimeError(f'Refusing to overwrite a different marketplace with {name}')
    data.setdefault('name', name)
    if name == 'personal':
        data.setdefault('interface', {'displayName': 'Personal'})
    else:
        data.setdefault('owner', {'name': 'Dotfiles'})
    current = data.setdefault('plugins', [])
    for entry in entries:
        previous = next((i for i in current if i.get('name') == entry['name']), None)
        if previous is None:
            current.append(entry)
        else:
            previous.update(entry)
