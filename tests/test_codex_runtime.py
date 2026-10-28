from dataclasses import replace
import functools
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))
sys.path.insert(0, str(TESTS))


import homes
import codex_runtime as cr
import description as d
from drift import Kind
import host_state as hs
import runtime_pin as rp

BUILD = '/tmp/codex-primary-runtime-AbC123/python-download/python'
GIT = ('git', '-c', 'user.name=Runtime Test', '-c', 'user.email=runtime-test@example.invalid',
       '-c', 'commit.gpgsign=false')


def pack(members) -> bytes:
    """A .tar.xz of (name, kind, value) members: kind is dir, file, exec, link, hard or fifo."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:xz') as tar:
        for name, kind, value in members:
            info = tarfile.TarInfo(name)
            if kind in ('file', 'exec'):
                data = value.encode() if isinstance(value, str) else value
                info.size, info.mode = len(data), 0o755 if kind == 'exec' else 0o644
                tar.addfile(info, io.BytesIO(data))
                continue
            info.type = {'dir': tarfile.DIRTYPE, 'link': tarfile.SYMTYPE, 'hard': tarfile.LNKTYPE,
                         'fifo': tarfile.FIFOTYPE}[kind]
            info.mode = 0o755
            info.linkname = value or ''
            tar.addfile(info)
    return buffer.getvalue()


def bundle(version, *, plugins=('documents', 'pdf'), plugin_version=None, extra=()):
    """Members shaped like OpenAI's runtime archive: one top folder, runtime.json, the catalog with plugins at the
    bundle's version, a relative link into node, node_modules, an executable and a hard link to it, a shipped
    __pycache__, and a terminfo link into the folder the bundle was built in."""
    catalog = f'{cr.ROOT}/{cr.CATALOG}'
    members = [
        (cr.ROOT, 'dir', None),
        (f'{cr.ROOT}/runtime.json', 'file', json.dumps({
            'bundleVersion': version, 'targetPlatform': 'linux', 'targetArch': 'x64', 'nodeVersion': 'v24.0.0',
            'pythonVersion': '3.12.14', 'libreOfficeVersion': '25.2-headless'})),
        (f'{catalog}/.agents/plugins/marketplace.json', 'file', json.dumps({'name': 'openai-primary-runtime', 'plugins': [
            {'name': name, 'source': {'source': 'local', 'path': f'./plugins/{name}'}} for name in plugins]})),
        (f'{cr.ROOT}/dependencies/node/bin/node', 'exec', '#!node\n'),
        (f'{cr.ROOT}/dependencies/node/bin/nodejs', 'hard', f'{cr.ROOT}/dependencies/node/bin/node'),
        (f'{cr.ROOT}/dependencies/bin/node', 'link', '../node/bin/node'),
        (f'{cr.ROOT}/dependencies/node/node_modules/x/index.js', 'file', 'module.exports = 1\n'),
        (f'{cr.ROOT}/dependencies/python/share/terminfo/f/f100', 'file', 'terminfo\n'),
        (f'{cr.ROOT}/dependencies/python/share/terminfo/f/freedom', 'link', f'{BUILD}/share/terminfo/f/f100'),
        (f'{cr.ROOT}/dependencies/python/lib/__pycache__/m.cpython-312.pyc', 'file', 'bytecode'),
    ]
    for name in plugins:
        members.append((f'{catalog}/plugins/{name}/.codex-plugin/plugin.json', 'file',
                        json.dumps({'name': name, 'version': plugin_version or version})))
        members.append((f'{catalog}/plugins/{name}/skills/{name}/SKILL.md', 'file',
                        f'---\nname: {name}\ndescription: {name}\n---\n'))
    return pack([*members, *extra])


class Server:
    """OpenAI's download host: LATEST.json names the archive published last."""

    def __init__(self):
        self.files, self.calls = {}, []

    def publish(self, version, data):
        url = f'{rp.ORIGIN}{version}/codex-primary-runtime-linux-x64-{version}.tar.xz'
        self.files[url] = data
        self.files[cr.LATEST] = json.dumps({
            'archiveUrl': url, 'archiveSha256': hashlib.sha256(data).hexdigest(), 'archiveSizeBytes': len(data),
            'bundleVersion': version, 'format': 'tar.xz', 'runtimeRootDirectoryName': cr.ROOT,
            'targetArch': 'x64', 'targetPlatform': 'linux'}).encode()
        return url

    def __call__(self, url):
        self.calls.append(url)
        data = self.files[url]
        return iter([data[start:start + 4096] for start in range(0, len(data), 4096)])

    def downloads(self):
        return [url for url in self.calls if url != cr.LATEST]


def quiet(_):
    pass


class Runtime(homes.Home):
    """A device home, a description to pin into, and the download host. The public root sits in a linked worktree
    of the main checkout, as a release's does."""

    def __enter__(self):
        super().__enter__()
        self.server = Server()
        base = self.roots.home
        self.checkout = base / 'checkout'
        subprocess.run([*GIT, 'init', '-q', str(self.checkout)], check=True)
        subprocess.run([*GIT, '-C', str(self.checkout), 'commit', '-q', '--allow-empty', '-m', 'Fixture'], check=True)
        subprocess.run([*GIT, '-C', str(self.checkout), 'worktree', 'add', '-q', '--detach', str(base / 'public')],
                       check=True)
        self.setup = d.Roots(base / 'public/setup', base / 'private/setup')
        self.write(self.setup.public / 'setup.toml', 'schema = 1\npublic = true\n')
        self.setup.private.mkdir(parents=True)
        self.bundle = self.roots.home / '.cache/codex-runtimes' / cr.ROOT
        self.work = self.bundle.with_name(f'.{cr.ROOT}.dotfiles')
        self.record = self.roots.home / '.local/state/dotfiles/codex-runtime.json'
        self.patches = [mock.patch('platform.machine', return_value='x86_64'),
                        mock.patch.object(sys, 'platform', 'linux')]
        for patcher in self.patches:
            patcher.start()
        return self

    def __exit__(self, *exc):
        for patcher in self.patches:
            patcher.stop()
        super().__exit__(*exc)

    def publish(self, version, **options):
        self.server.publish(version, bundle(version, **options))
        code = cr.review(self.setup, write=True, fetch=self.server, say=quiet)
        assert code in (0, 1), f'review --write exited {code}'
        return rp.RuntimePin.read(self.setup.public / rp.PIN, [])

    def install(self, pin):
        return cr.install(pin, self.roots, fetch=self.server, say=quiet)

    def inspect(self, pin, offered=frozenset({'pdf@openai-primary-runtime'})):
        return cr.inspect(pin, self.roots, offered)

    def verify(self, pin):
        return cr.verify(pin, self.roots, say=quiet)


def snapshot(root):
    return {path: (path.lstat().st_mtime_ns, path.read_bytes() if path.is_file() and not path.is_symlink() else None)
            for path in sorted(Path(root).rglob('*'))}


class InstallTest(unittest.TestCase):
    def test_review_pins_the_archive_and_install_unpacks_exactly_that_tree(self):
        with Runtime() as runtime:
            pin = runtime.publish('26.1.1')
            data = runtime.server.files[pin.url]
            self.assertEqual((pin.version, pin.archive, pin.archive_bytes, pin.plugins),
                             ('26.1.1', 'sha256:' + hashlib.sha256(data).hexdigest(), len(data), ('documents', 'pdf')))
            catalog = runtime.install(pin)
            self.assertEqual(catalog, runtime.bundle / 'plugins/openai-primary-runtime')
            self.assertFalse(runtime.bundle.is_symlink(), 'the bundle is a real folder')
            self.assertEqual(os.readlink(runtime.bundle / 'dependencies/python/share/terminfo/f/freedom'), 'f100',
                             'a link into the build folder points at the same file in the bundle')
            self.assertEqual(os.readlink(runtime.bundle / 'dependencies/bin/node'), '../node/bin/node')
            nodejs = runtime.bundle / 'dependencies/node/bin/nodejs'
            self.assertFalse(nodejs.is_symlink())
            self.assertEqual(nodejs.read_bytes(), b'#!node\n')
            self.assertTrue(os.access(nodejs, os.X_OK), 'the hard link keeps the execute bit')
            self.assertTrue((runtime.bundle / 'dependencies/node/node_modules/x/index.js').is_file())
            self.assertEqual(runtime.verify(pin), 0)
            self.assertEqual(runtime.inspect(pin), (), 'installed, recorded and registered')
            unregistered = runtime.inspect(pin, offered=frozenset({'gmail@openai-curated'}))
            self.assertEqual([(x.kind, x.observed) for x in unregistered],
                             [(Kind.CHANGED, cr.RuntimeCatalog('26.1.1', False))])

    def test_a_rerun_at_the_pin_hashes_no_file_and_downloads_nothing(self):
        with Runtime() as runtime:
            pin = runtime.publish('26.1.1')
            runtime.install(pin)
            downloads = runtime.server.downloads()
            with mock.patch.object(cr, '_hash', side_effect=AssertionError('a file was hashed')):
                runtime.install(pin)
                self.assertEqual(runtime.inspect(pin), ())
            self.assertEqual(runtime.server.downloads(), downloads)

    def test_an_archive_or_tree_other_than_the_pins_installs_nothing(self):
        for field, value in (('archive', 'sha256:' + '0' * 64), ('archive_bytes', 7), ('tree', 'sha256:' + '0' * 64)):
            with self.subTest(field), Runtime() as runtime:
                pin = replace(runtime.publish('26.1.1'), **{field: value})
                with self.assertRaises(RuntimeError):
                    runtime.install(pin)
                self.assertFalse(runtime.bundle.exists(), 'nothing is swapped in')
                self.assertFalse(runtime.work.exists(), 'the failed attempt leaves no work folder')
                self.assertEqual([x.kind for x in runtime.inspect(pin)], [Kind.MISSING])

    def test_a_bump_replaces_the_tree_and_leaves_one_folder(self):
        with Runtime() as runtime:
            old = runtime.publish('26.1.1')
            runtime.install(old)
            (runtime.bundle / 'dependencies/node/bin').chmod(0o555)
            new = runtime.publish('26.2.1')
            [change] = runtime.inspect(new)
            self.assertEqual((change.kind, change.observed), (Kind.CHANGED, cr.RuntimeCatalog('26.1.1', True)))
            runtime.install(new)
            self.assertEqual(json.loads((runtime.bundle / 'runtime.json').read_text())['bundleVersion'], '26.2.1')
            self.assertEqual(os.listdir(runtime.bundle.parent), [cr.ROOT])
            self.assertEqual((runtime.inspect(new), runtime.verify(new)), ((), 0))

    def test_a_crash_after_the_swap_is_finished_without_a_download(self):
        with Runtime() as runtime:
            runtime.install(runtime.publish('26.1.1'))
            new = runtime.publish('26.2.1')
            save, saves = cr._Bundle.save, []

            def crash_after_the_swap(bundle, receipt):
                saves.append(receipt)
                if len(saves) == 2:
                    raise OSError('power cut')
                save(bundle, receipt)

            with mock.patch.object(cr._Bundle, 'save', crash_after_the_swap), self.assertRaises(OSError):
                runtime.install(new)
            self.assertEqual(json.loads((runtime.bundle / 'runtime.json').read_text())['bundleVersion'], '26.2.1')
            self.assertEqual([(x.kind, x.observed.problem) for x in runtime.inspect(new)],
                             [(Kind.CHANGED, 'an unfinished install')])
            downloads = runtime.server.downloads()
            runtime.install(new)
            self.assertEqual(runtime.server.downloads(), downloads)
            self.assertFalse(runtime.work.exists())
            self.assertEqual((runtime.inspect(new), runtime.verify(new)), ((), 0))

    def test_a_crash_between_the_renames_is_finished_without_a_download(self):
        with Runtime() as runtime:
            runtime.install(runtime.publish('26.1.1'))
            (runtime.bundle / 'dependencies/node/bin').chmod(0o555)
            new = runtime.publish('26.2.1')
            rename, renames = Path.rename, []

            def crash_on_the_second_rename(path, target):
                renames.append(path)
                if len(renames) == 2:
                    raise OSError('power cut')
                return rename(path, target)

            with mock.patch.object(Path, 'rename', crash_on_the_second_rename), self.assertRaises(OSError):
                runtime.install(new)
            self.assertFalse(runtime.bundle.exists(), 'the old tree is aside and the new one not yet in place')
            self.assertEqual([x.kind for x in runtime.inspect(new)], [Kind.MISSING])
            downloads = runtime.server.downloads()
            runtime.install(new)
            self.assertEqual(runtime.server.downloads(), downloads)
            self.assertEqual(os.listdir(runtime.bundle.parent), [cr.ROOT])
            self.assertEqual((runtime.inspect(new), runtime.verify(new)), ((), 0))

    def test_a_folder_this_device_did_not_install_is_refused_and_left_alone(self):
        with Runtime() as runtime:
            pin = runtime.publish('26.1.1')
            runtime.write(runtime.bundle / 'runtime.json', json.dumps({'bundleVersion': '26.0.9'}))
            runtime.write(runtime.bundle / 'dependencies/bin/node', 'hand-unpacked')
            before, downloads = snapshot(runtime.bundle), runtime.server.downloads()
            [change] = runtime.inspect(pin)
            self.assertEqual((change.kind, change.observed.version), (Kind.LOCAL_EDIT, '26.0.9'))
            self.assertIn('not installed by this device', change.observed.problem)
            with self.assertRaises(cr.ConcurrentEdit):
                runtime.install(pin)
            self.assertEqual(snapshot(runtime.bundle), before)
            self.assertEqual(runtime.server.downloads(), downloads)

    def test_a_receiptless_copy_of_the_pin_is_adopted_without_a_download(self):
        with Runtime() as runtime:
            pin = runtime.publish('26.1.1')
            runtime.install(pin)
            runtime.record.unlink()
            self.assertEqual([(x.kind, x.observed.problem) for x in runtime.inspect(pin)],
                             [(Kind.CHANGED, 'not recorded by this device')])
            downloads = runtime.server.downloads()
            runtime.install(pin)
            self.assertEqual(runtime.server.downloads(), downloads)
            self.assertEqual(runtime.inspect(pin), ())

    def test_an_edited_file_is_a_local_edit_and_new_bytecode_is_not(self):
        with Runtime() as runtime:
            pin = runtime.publish('26.1.1')
            runtime.install(pin)
            runtime.write(runtime.bundle / f'{cr.CATALOG}/plugins/pdf/__pycache__/x.cpython-312.pyc', 'bytecode')
            runtime.write(runtime.bundle / 'dependencies/python/lib/__pycache__/m.cpython-312.pyc', 'recompiled')
            self.assertEqual(runtime.inspect(pin), (), 'Python bytecode is not part of the bundle')
            runtime.write(runtime.bundle / 'dependencies/node/node_modules/x/index.js', 'module.exports = 2\n')
            [change] = runtime.inspect(pin)
            self.assertEqual(change.kind, Kind.LOCAL_EDIT)
            self.assertIn('1 changed (dependencies/node/node_modules/x/index.js)', change.observed.problem)
            with self.assertRaises(cr.ConcurrentEdit):
                runtime.install(pin)
            self.assertEqual(runtime.verify(pin), 2)

    def test_an_edit_that_keeps_the_size_and_mtime_is_a_local_edit(self):
        with Runtime() as runtime:
            pin = runtime.publish('26.1.1')
            runtime.install(pin)
            path = runtime.bundle / 'dependencies/node/node_modules/x/index.js'
            before = path.stat()
            path.write_text('module.exports = 2\n')
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns), (before.st_size, before.st_mtime_ns))
            [change] = runtime.inspect(pin)
            self.assertEqual(change.kind, Kind.LOCAL_EDIT)
            self.assertIn('1 changed (dependencies/node/node_modules/x/index.js)', change.observed.problem)
            with self.assertRaises(cr.ConcurrentEdit):
                runtime.install(pin)

    def test_a_linked_bundle_or_runtimes_folder_is_refused(self):
        for linked in ('runtimes', 'bundle'):
            with self.subTest(linked), Runtime() as runtime:
                pin = runtime.publish('26.1.1')
                elsewhere = runtime.roots.home / 'elsewhere'
                elsewhere.mkdir()
                path = runtime.bundle.parent if linked == 'runtimes' else runtime.bundle
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(elsewhere, target_is_directory=True)
                [change] = runtime.inspect(pin)
                self.assertEqual(change.kind, Kind.LOCAL_EDIT)
                self.assertIn('is a link', change.observed.problem)
                with self.assertRaises(cr.ConcurrentEdit):
                    runtime.install(pin)
                self.assertEqual(list(elsewhere.iterdir()), [])

    def test_unsafe_members_are_refused_by_review_and_by_install(self):
        cases = {
            'an absolute link': (f'{cr.ROOT}/dependencies/passwd', 'link', '/etc/passwd'),
            'a link that leaves the bundle': (f'{cr.ROOT}/dependencies/up', 'link', '../../outside'),
            'a path that leaves the bundle': (f'{cr.ROOT}/../outside', 'file', 'escaped'),
            'a second top folder': ('other/x', 'file', 'stray'),
            'a hard link outside the top folder': (f'{cr.ROOT}/dependencies/hard', 'hard', 'other/x'),
            'a fifo': (f'{cr.ROOT}/dependencies/pipe', 'fifo', None),
        }
        for case, member in cases.items():
            with self.subTest(case), Runtime() as runtime:
                data = bundle('26.1.1', extra=[member])
                url = runtime.server.publish('26.1.1', data)
                self.assertEqual(cr.review(runtime.setup, write=True, fetch=runtime.server, say=quiet), 3)
                self.assertFalse((runtime.setup.public / rp.PIN).exists())
                pin = rp.RuntimePin('26.1.1', url, hs.bytes_digest(data), len(data), 'sha256:' + '0' * 64, 1, ('pdf',))
                with self.assertRaises(ValueError):
                    runtime.install(pin)
                self.assertFalse(runtime.bundle.exists())
                self.assertFalse(runtime.work.exists())

    def test_too_little_free_space_stops_before_any_download(self):
        with Runtime() as runtime:
            pin = runtime.publish('26.1.1')
            with mock.patch('shutil.disk_usage', return_value=SimpleNamespace(free=10)):
                with self.assertRaisesRegex(RuntimeError, 'free'):
                    runtime.install(pin)
            self.assertEqual(runtime.server.downloads(), [pin.url], 'only review downloaded it')

    def test_a_machine_the_runtime_is_not_built_for_is_incomplete(self):
        with Runtime() as runtime:
            pin = runtime.publish('26.1.1')
            with mock.patch('platform.machine', return_value='aarch64'):
                self.assertEqual([x.kind for x in runtime.inspect(pin)], [Kind.INCOMPLETE])

    def test_no_pin_means_no_runtime(self):
        with Runtime() as runtime:
            self.assertEqual(runtime.inspect(None), ())


class PinTest(unittest.TestCase):
    PIN = rp.RuntimePin('26.1025.11240', f'{rp.ORIGIN}26.1025.11240/codex-primary-runtime-linux-x64-26.1025.11240.tar.xz',
                        'sha256:' + 'a' * 64, 387299468, 'sha256:' + 'b' * 64, 1836123456,
                        ('documents', 'pdf', 'presentations', 'spreadsheets', 'template-creator'))

    def read(self, text):
        with homes.Home() as home:
            path = home.write(home.roots.home / rp.PIN, text)
            errors = []
            return rp.RuntimePin.read(path, errors), [error.removeprefix(f'{path}:') for error in errors]

    def test_the_pin_round_trips(self):
        self.assertEqual(self.read(self.PIN.render()), (self.PIN, []))

    def test_the_pin_rejects_a_foreign_origin_a_bad_digest_or_size_and_unknown_keys(self):
        text = self.PIN.render()
        cases = (
            ('url', text.replace(rp.ORIGIN, 'https://example.com/')),
            ('url', text.replace('/26.1025.11240/', '/26.1025.11239/')),
            ('archive', text.replace('a' * 64, 'a' * 63)),
            ('tree_bytes', text.replace('1836123456', '0')),
            ('public', text.replace('public = true\n', '')),
            ('surprise', text + 'surprise = 1\n'),
            ('plugins', text.replace('"documents", "pdf"', '"pdf", "documents"')),
        )
        for key, edited in cases:
            with self.subTest(edited):
                pin, errors = self.read(edited)
                self.assertIsNone(pin)
                self.assertEqual([error.split(':')[0] for error in errors], [key])


class ReviewTest(unittest.TestCase):
    def test_review_reports_whether_upstream_moved_without_writing(self):
        with Runtime() as runtime:
            runtime.publish('26.1.1')
            path = runtime.setup.public / rp.PIN
            before = path.read_bytes()
            runtime.server.calls.clear()
            self.assertEqual(cr.review(runtime.setup, write=False, fetch=runtime.server, say=quiet), 0)
            runtime.server.publish('26.2.1', bundle('26.2.1'))
            self.assertEqual(cr.review(runtime.setup, write=False, fetch=runtime.server, say=quiet), 1)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(runtime.server.calls, [cr.LATEST, cr.LATEST], 'review only reads LATEST.json')

    def test_review_refuses_a_plugin_at_another_version_than_the_bundle(self):
        with Runtime() as runtime:
            runtime.server.publish('26.1.1', bundle('26.1.1', plugin_version='26.1.0'))
            self.assertEqual(cr.review(runtime.setup, write=True, fetch=runtime.server, say=quiet), 3)
            self.assertFalse((runtime.setup.public / rp.PIN).exists())

    def test_review_writes_only_into_a_linked_worktree(self):
        with Runtime() as runtime:
            runtime.server.publish('26.1.1', bundle('26.1.1'))
            for case, public in (('a main checkout', runtime.checkout / 'setup'),
                                 ('no repository', runtime.roots.home / 'loose/setup')):
                with self.subTest(case):
                    runtime.write(public / 'setup.toml', 'schema = 1\npublic = true\n')
                    roots = d.Roots(public, runtime.setup.private)
                    self.assertEqual(cr.review(roots, write=True, fetch=runtime.server, say=quiet), 3)
                    self.assertFalse((public / rp.PIN).exists())
            self.assertEqual(runtime.server.downloads(), [])

    def test_review_writes_only_on_linux_and_compares_anywhere(self):
        with Runtime() as runtime:
            runtime.publish('26.1.1')
            path = runtime.setup.public / rp.PIN
            before = path.read_bytes()
            runtime.server.publish('26.2.1', bundle('26.2.1'))
            downloads = runtime.server.downloads()
            for name in ('darwin', 'win32'):
                with self.subTest(name), mock.patch.object(sys, 'platform', name):
                    said = []
                    self.assertEqual(cr.review(runtime.setup, write=True, fetch=runtime.server, say=said.append), 3)
                    self.assertIn('WSL', said[-1])
                    self.assertEqual(cr.review(runtime.setup, write=False, fetch=runtime.server, say=quiet), 1,
                                     'review without --write still reports that upstream moved')
            self.assertEqual((path.read_bytes(), runtime.server.downloads()), (before, downloads))

    def test_latest_json_without_a_string_archive_url_fails_its_check(self):
        with Runtime() as runtime:
            runtime.server.publish('26.1.1', bundle('26.1.1'))
            latest = json.loads(runtime.server.files[cr.LATEST])
            del latest['archiveUrl']
            for case, field in (('missing', {}), ('null', {'archiveUrl': None}), ('not a string', {'archiveUrl': 7})):
                with self.subTest(case):
                    runtime.server.files[cr.LATEST] = json.dumps(latest | field).encode()
                    self.assertEqual(cr.review(runtime.setup, write=False, fetch=runtime.server, say=quiet), 3)

    def test_review_keeps_the_pin_when_an_enabled_package_would_lose_its_plugin(self):
        with Runtime() as runtime:
            runtime.write(runtime.setup.private / 'packages/documents.toml',
                          '[codex]\ninstall = [{ via = "openai-primary-runtime", enabled = true }]\n')
            runtime.publish('26.1.1')
            path = runtime.setup.public / rp.PIN
            before = path.read_bytes()
            runtime.server.publish('26.2.1', bundle('26.2.1', plugins=('pdf',)))
            self.assertEqual(cr.review(runtime.setup, write=True, fetch=runtime.server, say=quiet), 3)
            self.assertEqual(path.read_bytes(), before)


FAKE_CODEX = r'''import json, pathlib, shutil, sys, tomllib
here = pathlib.Path(__file__).parent
state = json.loads((here / 'codex-state.json').read_text())
args = sys.argv[1:]
with (here / 'codex-calls.log').open('a') as log:
    log.write(' '.join(args) + '\n')
home = pathlib.Path(state['home'])


def listing(root):
    return json.loads((pathlib.Path(root) / '.agents/plugins/marketplace.json').read_text())


if args == ['--version']:
    print('codex-cli 0.0.0')
elif args[:3] == ['plugin', 'marketplace', 'add']:
    state['catalogs'][listing(args[3])['name']] = args[3]
elif args[:2] == ['plugin', 'add']:
    name, catalog = args[2].split('@')
    root = pathlib.Path(state['catalogs'][catalog])
    source = root / next(e for e in listing(root)['plugins'] if e['name'] == name)['source']['path']
    version = json.loads((source / '.codex-plugin/plugin.json').read_text())['version']
    shutil.rmtree(home / 'plugins/cache' / catalog / name, ignore_errors=True)
    shutil.copytree(source, home / 'plugins/cache' / catalog / name / version)
    state['installed'][args[2]] = version
elif args == ['plugin', 'list', '--available', '--json']:
    config = home / 'config.toml'
    flags = tomllib.loads(config.read_text()).get('plugins', {}) if config.is_file() else {}
    installed, available = [], []
    for catalog, root in state['catalogs'].items():
        for entry in listing(root)['plugins'] if pathlib.Path(root).is_dir() else []:
            identity = entry['name'] + '@' + catalog
            plugin = {'pluginId': identity, 'name': entry['name'], 'marketplaceName': catalog,
                      'installed': identity in state['installed']}
            if identity in state['installed']:
                installed.append(plugin | {'version': state['installed'][identity],
                                           'enabled': flags.get(identity, {}).get('enabled', True),
                                           'source': {'source': 'local', 'path': str(pathlib.Path(root) / entry['source']['path'])}})
            else:
                available.append(plugin)
    print(json.dumps({'installed': installed, 'available': available}))
else:
    sys.exit('unexpected: ' + ' '.join(args))
(here / 'codex-state.json').write_text(json.dumps(state))
'''


class ApplyTest(unittest.TestCase):
    def test_one_apply_installs_the_runtime_registers_its_catalog_and_adds_its_plugins(self):
        from codex_host import CodexState
        from device import Device
        from device_sync import apply, plan
        with Runtime() as runtime:
            script = runtime.bin / 'codex.py'
            script.write_text(FAKE_CODEX)
            (runtime.bin / 'codex-state.json').write_text(json.dumps(
                {'home': str(runtime.roots.codex_home), 'catalogs': {}, 'installed': {}}))
            installing = functools.partial(cr.install, fetch=runtime.server, say=quiet)
            runtime.patches.append(mock.patch.object(cr, 'install', installing))
            runtime.patches[-1].start()
            device = Device(hs.Platform.LINUX, runtime.roots, (CodexState(runtime.roots, [sys.executable, str(script)]),), ())
            install = d.CodexPlacement((d.MarketplaceInstall(hs.PRIMARY_RUNTIME, True),))
            packages = {name: d.Package(name, d.Visibility.PRIVATE, '', None, modes, d.ToolNotes.AUDIT, install, None)
                        for name, modes in (('documents', {'documents': hs.SkillMode.ON}), ('pdf', {}))}
            setup = d.Setup(runtime.setup, packages, {}, {}, {}, None, (), frozenset(), {},
                            runtime=runtime.publish('26.1.1'))

            def writes():
                calls = (runtime.bin / 'codex-calls.log').read_text().splitlines()
                return [call for call in calls if call.startswith(('plugin add', 'plugin marketplace add'))]

            before = plan(setup, device, configurations=False)
            self.assertEqual(sorted((x.kind, x.key.subject) for x in before.changes), [
                (Kind.MISSING, 'documents:documents'), (Kind.MISSING, 'documents@openai-primary-runtime'),
                (Kind.MISSING, 'openai-primary-runtime'), (Kind.MISSING, 'pdf@openai-primary-runtime')])
            after, _ = apply(setup, device, before)
            self.assertEqual(writes(), [f'plugin marketplace add {runtime.bundle / cr.CATALOG} --json',
                                        'plugin add documents@openai-primary-runtime --json',
                                        'plugin add pdf@openai-primary-runtime --json'])
            config = tomllib.loads((runtime.roots.codex_home / 'config.toml').read_text())
            self.assertEqual(config['plugins'], {'documents@openai-primary-runtime': {'enabled': True},
                                                 'pdf@openai-primary-runtime': {'enabled': True}})
            self.assertEqual(config['skills']['config'], [{'name': 'documents:documents', 'enabled': True}])
            self.assertEqual((after.exit_code, after.result.differences), (0, ()), 'one apply converges')

            downloads = runtime.server.downloads()
            again, _ = apply(setup, device, plan(setup, device, configurations=False))
            self.assertEqual((again.exit_code, len(writes()), runtime.server.downloads()), (0, 3, downloads))

            setup = replace(setup, runtime=runtime.publish('26.2.1'))
            bump = plan(setup, device, configurations=False)
            self.assertEqual(sorted((x.kind, x.key.subject) for x in bump.changes), [
                (Kind.CHANGED, 'documents@openai-primary-runtime'), (Kind.CHANGED, 'openai-primary-runtime'),
                (Kind.CHANGED, 'pdf@openai-primary-runtime')])
            bumped, _ = apply(setup, device, bump)
            self.assertEqual(writes()[3:], [f'plugin marketplace add {runtime.bundle / cr.CATALOG} --json',
                                            'plugin add documents@openai-primary-runtime --json',
                                            'plugin add pdf@openai-primary-runtime --json'])
            self.assertEqual((bumped.exit_code, os.listdir(runtime.bundle.parent)), (0, [cr.ROOT]))


if __name__ == '__main__':
    unittest.main()
