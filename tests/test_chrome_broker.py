"""chrome-broker.mjs holds one DevTools connection, opens pages in windows of their own and hides secrets in replies.

Each test class starts a throwaway headless Chrome with its own profile and debugging port, a local page server and a
broker unit of its own, so nothing here touches the user's Chrome or the machine's broker.
"""

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

BROKER = Path(__file__).resolve().parents[1] / 'skills/browser-and-computer-use/scripts/chrome-broker.mjs'
CHROME = shutil.which('google-chrome') or shutil.which('google-chrome-stable')

HEX64 = '9f2c4e1a7b3d5f60' * 4
HEX31 = '0123456789abcdef0123456789abcde'
B64_28 = 'Zm9vYmFyQmF6UXV4MTIzNDU2Nzg5'
B64_23 = 'aB3dE5fG7hJ9kL1mN3pQ5rS'
PLAIN_VALUE = 'correct horse battery staple'
KEY_GROUP = 'A3-ABC123-XYZ789'

PAGES = {
    '/planted': f"""<!doctype html><title>Planted</title>
<div role="dialog"><p>Webhook secret: {HEX64}</p><input id="webhook" value="{HEX64}"></div>
<p>A3-ABC123 • ••••••</p>
<p>Secret Key {KEY_GROUP} on its own line</p>
<p>Token {B64_28} in text</p>
<label>Password <input id="masked" type="password" value="••••••••••"></label>
<label>Phrase <input id="phrase" value="{PLAIN_VALUE}"></label>
<p id="short-hex">{HEX31}</p>
<p id="short-b64">{B64_23}</p>
<p>Plain text stays.</p>""",
    '/plain': '<!doctype html><title>Plain</title><p>Nothing secret here.</p>',
}


def skip_reason():
    if not shutil.which('node'):
        return 'node is not installed'
    if not CHROME:
        return 'Google Chrome is not installed'
    if not shutil.which('systemd-run'):
        return 'systemd-run is not installed'
    if subprocess.run(['systemctl', '--user', 'show-environment'], capture_output=True).returncode != 0:
        return 'no systemd user manager answers'
    if not shutil.which('ss'):
        return 'ss is not installed'
    return None


class Pages(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/slow':
            time.sleep(3)
            path = '/plain'
        body = PAGES.get(path)
        self.send_response(200 if body else 404)
        self.send_header('content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write((body or 'not found').encode())

    def log_message(self, format, *args):
        pass


class Chrome:
    """A headless Chrome on its own profile, started and stopped by its own process ID."""

    def __init__(self, profile):
        self.profile = profile
        self.process = None

    def start(self):
        port_file = self.profile / 'DevToolsActivePort'
        port_file.unlink(missing_ok=True)
        self.process = subprocess.Popen(
            [str(CHROME), '--headless=new', f'--user-data-dir={self.profile}', '--remote-debugging-port=0', '--no-first-run',
             '--no-default-browser-check', 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if port_file.exists() and port_file.read_text().count('\n') >= 1:
                self.port = int(port_file.read_text().split()[0])
                return
            time.sleep(0.2)
        raise RuntimeError('the test Chrome wrote no DevToolsActivePort')

    def stop(self):
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=20)

    def page_urls(self):
        with urlopen(f'http://127.0.0.1:{self.port}/json/list', timeout=10) as reply:
            return [(target['id'], target['url']) for target in json.load(reply) if target['type'] == 'page']


SKIP = skip_reason()


@unittest.skipIf(SKIP is not None, SKIP or '')
class BrokerRig(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = Path(tempfile.mkdtemp(prefix='cb.'))
        cls.chrome = Chrome(cls.scratch / 'profile')
        cls.chrome.start()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Pages)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.site = f'http://127.0.0.1:{cls.server.server_address[1]}'
        cls.unit = f'chrome-broker-test-{uuid.uuid4().hex[:8]}'
        cls.socket = cls.scratch / 'broker.sock'
        cls.env = {**os.environ, 'CHROME_BROKER_SOCKET': str(cls.socket), 'CHROME_BROKER_UNIT': cls.unit,
                   'CHROME_BROKER_PORT_FILE': str(cls.scratch / 'profile/DevToolsActivePort')}
        started = cls.broker('start')
        if started.returncode != 0:
            cls.tearDownClass()
            raise RuntimeError(f'the broker did not start: {started.stderr}')
        cls.pid = json.loads(started.stdout)['pid']

    @classmethod
    def tearDownClass(cls):
        cls.broker('stop')
        subprocess.run(['systemctl', '--user', 'stop', cls.unit], capture_output=True)
        cls.chrome.stop()
        cls.server.shutdown()
        shutil.rmtree(cls.scratch, ignore_errors=True)

    @classmethod
    def broker(cls, *args, timeout=60, stdin=None):
        return subprocess.run(['node', str(BROKER), *args], env=cls.env, capture_output=True, text=True,
                              timeout=timeout, input=stdin)

    def ok(self, *args, **kwargs):
        result = self.broker(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def connections(self):
        """The broker's open TCP connections to the test Chrome's debugging port."""
        out = subprocess.run(['ss', '-tnpH', 'state', 'established'], capture_output=True, text=True).stdout
        return [line for line in out.splitlines()
                if f'pid={self.pid},' in line and re.search(rf'127\.0\.0\.1:{self.chrome.port}\s', line + ' ')]


class ConnectionTest(BrokerRig):
    def test_a_second_start_reports_the_running_broker(self):
        again = self.ok('start')
        self.assertEqual(again['pid'], self.pid)
        self.assertEqual(again['note'], 'already running')
        active = subprocess.run(['systemctl', '--user', 'is-active', self.unit], capture_output=True, text=True)
        self.assertEqual(active.stdout.strip(), 'active')

    def test_nothing_listens_on_tcp_and_the_socket_is_private(self):
        listening = subprocess.run(['ss', '-ltnpH'], capture_output=True, text=True).stdout
        self.assertNotIn(f'pid={self.pid},', listening)
        self.assertEqual(stat.S_IMODE(self.socket.stat().st_mode), 0o600)

    def test_one_connection_and_a_killed_client_opens_no_second(self):
        self.assertEqual(len(self.connections()), 1)
        killed = subprocess.run(['timeout', '1', 'node', str(BROKER), 'open', f'{self.site}/slow'], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(killed.returncode, 124)
        time.sleep(3.5)
        self.assertEqual(len(self.connections()), 1)
        self.assertEqual(self.ok('status')['state'], 'connected')


class HidingTest(BrokerRig):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        opened = cls.broker('open', f'{cls.site}/planted')
        cls.page = json.loads(opened.stdout)['page']
        cls.read = cls.broker('read', cls.page)

    def test_open_gives_a_page_id_its_context_and_visibility(self):
        opened = self.ok('open', f'{self.site}/plain')
        self.assertRegex(opened['page'], r'^p\d+$')
        self.assertEqual(opened['context'], 'default')
        self.assertEqual(opened['visibility'], 'visible')
        self.ok('close', opened['page'])

    def test_each_input_appears_with_its_length_only(self):
        self.assertEqual(self.read.returncode, 0, self.read.stderr)
        inputs = {field['id']: field for field in json.loads(self.read.stdout)['inputs']}
        self.assertEqual(inputs['phrase']['length'], len(PLAIN_VALUE))
        self.assertEqual(inputs['webhook']['length'], 64)
        self.assertEqual(inputs['masked']['length'], 10)
        self.assertNotIn(PLAIN_VALUE, self.read.stdout)

    def test_hex_of_32_or_more_is_hidden(self):
        self.assertIn('Webhook secret', self.read.stdout)
        self.assertNotIn(HEX64[:32], self.read.stdout)

    def test_base64_of_24_or_more_is_hidden(self):
        self.assertIn('Token', self.read.stdout)
        self.assertNotIn(B64_28, self.read.stdout)

    def test_secret_key_groups_are_hidden(self):
        self.assertIn('on its own line', self.read.stdout)
        self.assertNotIn('A3-', self.read.stdout)

    def test_lines_with_a_bullet_are_hidden(self):
        self.assertNotIn('•', self.read.stdout)
        self.assertNotIn('\\u2022', self.read.stdout)

    def test_shorter_runs_pass_unmasked(self):
        self.assertIn(HEX31, self.read.stdout)
        self.assertIn(B64_23, self.read.stdout)
        self.assertIn('Plain text stays.', self.read.stdout)

    def test_errors_are_hidden_too(self):
        refused = self.broker('read', HEX64)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('not a page this broker opened', refused.stderr)
        self.assertNotIn(HEX64[:32], refused.stderr)

    def test_pages_and_contexts_it_did_not_create_are_refused(self):
        own_tab = next(target for target, url in self.chrome.page_urls() if url == 'about:blank')
        for args in (('read', own_tab), ('read', own_tab[:8]), ('close', 'p999'), ('status', 'p999'),
                     ('dispose', 'c999'), ('dispose', 'default')):
            with self.subTest(args=args):
                refused = self.broker(*args)
                self.assertNotEqual(refused.returncode, 0)
                self.assertRegex(refused.stderr, r'is not a (page|context) this broker opened')
        self.assertIn(own_tab, [target for target, _ in self.chrome.page_urls()])

    def test_a_new_context_is_disposed_with_its_pages(self):
        opened = self.ok('open', f'{self.site}/plain', '--new-context')
        self.assertRegex(opened['context'], r'^c\d+$')
        self.assertEqual(self.ok('dispose', opened['context']), {'disposed': opened['context']})
        refused = self.broker('read', opened['page'])
        self.assertNotEqual(refused.returncode, 0)


class DropTest(BrokerRig):
    def test_a_dropped_connection_stays_down_until_start(self):
        self.chrome.stop()
        deadline = time.monotonic() + 10
        while self.ok('status')['state'] != 'disconnected' and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertEqual(self.ok('status')['state'], 'disconnected')
        refused = self.broker('open', f'{self.site}/plain')
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('disconnected', refused.stderr)

        self.chrome.start()
        time.sleep(3)
        self.assertEqual(self.connections(), [])
        self.assertEqual(self.ok('status')['state'], 'disconnected')

        self.assertEqual(self.ok('start')['state'], 'connected')
        self.assertEqual(len(self.connections()), 1)

        opened = self.ok('open', f'{self.site}/plain', '--new-context')
        self.assertEqual(self.ok('stop'), {'stopped': True})
        deadline = time.monotonic() + 10
        state = 'active'
        while state == 'active' and time.monotonic() < deadline:
            time.sleep(0.2)
            state = subprocess.run(['systemctl', '--user', 'is-active', self.unit], capture_output=True,
                                   text=True).stdout.strip()
        self.assertNotEqual(state, 'active')
        self.assertNotIn(f'{self.site}/plain', [url for _, url in self.chrome.page_urls()])
        self.assertFalse(self.socket.exists())
        self.assertTrue(opened['page'])


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class UsageTest(unittest.TestCase):
    def test_help_and_unknown_commands(self):
        helped = subprocess.run(['node', str(BROKER), '--help'], capture_output=True, text=True, timeout=30)
        self.assertEqual(helped.returncode, 0)
        self.assertIn('usage: node chrome-broker.mjs start', helped.stdout)

    def test_no_broker_names_start(self):
        env = {**os.environ, 'CHROME_BROKER_SOCKET': '/nonexistent/broker.sock'}
        result = subprocess.run(['node', str(BROKER), 'status'], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertIn('no broker is running', result.stderr)


if __name__ == '__main__':
    unittest.main()
