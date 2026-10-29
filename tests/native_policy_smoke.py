"""Exercise fresh Codex installs and cache rehydration from a policy-aware personal catalog."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from description import CodexPlacement, EngineInstall, LocalSource, Package, Roots, Setup, ToolNotes, Visibility
from host_state import DeviceRoots, SkillMode
from sync_sources import Builder


def discover(environment, home):
    process = subprocess.Popen(['codex', 'app-server', '--stdio'], env=environment, cwd=home,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    reader = ThreadPoolExecutor(max_workers=1)

    def call(identifier, method, params):
        process.stdin.write(json.dumps({'id': identifier, 'method': method, 'params': params}) + '\n')
        process.stdin.flush()
        while True:
            line = reader.submit(process.stdout.readline).result(timeout=30)
            if not line:
                raise RuntimeError('Native app-server exited')
            message = json.loads(line)
            if message.get('id') == identifier:
                if 'error' in message:
                    raise RuntimeError(message['error'])
                return message['result']

    try:
        call(1, 'initialize', {'clientInfo': {'name': 'dotfiles-policy-smoke', 'version': '1.0'}})
        process.stdin.write('{"method":"initialized"}\n')
        process.stdin.flush()
        return call(2, 'skills/list', {'cwds': [str(home)], 'forceReload': True})
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        reader.shutdown(wait=True, cancel_futures=True)


def main():
    with tempfile.TemporaryDirectory(prefix='dotfiles-policy-smoke-') as temporary:
        home = Path(temporary).resolve()
        roots = DeviceRoots(home, home / '.codex', home / '.claude', home / '.agents', home / 'engine')
        roots.codex_home.mkdir()
        source = home / 'source'
        (source / '.codex-plugin').mkdir(parents=True)
        name = 'dotfiles-policy-fixture'
        (source / '.codex-plugin/plugin.json').write_text(json.dumps({'name': name, 'version': '1.0.0', 'skills': './skills/'}))
        for skill in ('explicit-test', 'ordinary-test'):
            path = source / 'skills' / skill
            path.mkdir(parents=True)
            (path / 'SKILL.md').write_text(f'---\nname: {skill}\ndescription: A native loader fixture.\n---\nFixture only.\n')
        package = Package(name, Visibility.PUBLIC, '', LocalSource('source', roots.tree_digest(source)),
                          {'explicit-test': SkillMode.EXPLICIT}, ToolNotes.AUDIT,
                          CodexPlacement((EngineInstall(True),)), None)
        setup = Setup(Roots(home / 'setup', None), {name: package}, {}, {}, {}, None, (), frozenset(), {})
        built = Builder(setup, roots, roots.engine).codex(package, verify=False)
        catalog = roots.agents_home / 'plugins/marketplace.json'
        catalog.parent.mkdir(parents=True)
        catalog.write_text(json.dumps({'name': 'personal', 'interface': {'displayName': 'Personal'}, 'plugins': [
            {'name': name, 'source': {'source': 'local', 'path': './' + built.root.relative_to(home).as_posix()},
             'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_USE'}, 'category': 'Developer Tools'}]}))
        environment = dict(os.environ, CODEX_HOME=str(roots.codex_home))

        def cli(*args):
            result = subprocess.run(['codex', *args, '--json'], cwd=home, env=environment,
                                    capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout)[-2000:])

        cli('plugin', 'marketplace', 'add', str(home))
        identity = name + '@personal'
        installed = roots.codex_home / 'plugins/cache/personal' / name / built.version
        for attempt in range(2):
            cli('plugin', 'add', identity)
            listing = discover(environment, home)
            found = {s['name']: s for group in listing['data'] for s in group['skills'] if s.get('pluginId') == identity}
            assert len(found) == 2, found
            assert all(s['enabled'] for s in found.values())
            sidecar = installed / 'skills/explicit-test/agents/openai.yaml'
            assert sidecar.read_text() == 'policy:\n  allow_implicit_invocation: false\n'
            assert not (installed / 'skills/ordinary-test/agents/openai.yaml').exists()
            if attempt == 0:
                assert str(installed) and installed.resolve().is_relative_to(home) and installed.is_dir()
                shutil.rmtree(installed)
        print(json.dumps({'fresh_native_install': True, 'cache_rehydration': True, 'discovered_skills': 2,
                          'explicit_sidecar': False, 'live_settings_untouched': True}))


if __name__ == '__main__':
    main()
