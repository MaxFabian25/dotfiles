#!/usr/bin/env node
// One long-lived DevTools connection to the user's Windows Chrome, shared by every agent command, so Chrome asks
// "Allow remote debugging?" once per start and no client of its own leaves that prompt behind.
//
//   node chrome-broker.mjs start                  start the broker as a systemd user unit, or report the running one
//   node chrome-broker.mjs status [PAGE]          the connection and the broker's pages, or one page
//   node chrome-broker.mjs open URL [--new-context]
//                                                 a page in a window of its own, in the user's profile or in a new
//                                                 cookie-less context; prints its page ID, context ID and visibility
//   node chrome-broker.mjs read PAGE              the page's text, and each input with its value's length only
//   node chrome-broker.mjs close PAGE
//   node chrome-broker.mjs dispose CONTEXT        close a cookie-less context and every page in it
//   node chrome-broker.mjs stop                   close the broker's pages and contexts, then the connection
//
// Commands reach only the pages and contexts the broker opened. Every reply and error hides input values (as their
// length), hex runs of 32 characters or more, base64 runs of 24 or more that mix upper case, lower case and digits,
// 1Password Secret Key groups (A3-...) and every line that holds a "•". Clients talk to the broker over HTTP on a
// Unix socket with mode 0600, never TCP, because under WSL's mirrored networking a localhost port is open to every
// Windows program and to every page in Chrome. When Chrome drops the connection, the broker answers "disconnected"
// and never reconnects by itself, since each connection raises a new prompt on the user's screen; `start` reconnects.
//
// It finds Windows Chrome's DevToolsActivePort file under Windows' LOCALAPPDATA. CHROME_BROKER_PORT_FILE names
// another one, CHROME_BROKER_SOCKET another socket and CHROME_BROKER_UNIT another unit name, as the tests do.

import { execFileSync, spawnSync } from 'node:child_process';
import { chmodSync, existsSync, readFileSync, unlinkSync } from 'node:fs';
import http from 'node:http';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT = fileURLToPath(import.meta.url);
const CALL_MS = 30_000;
const LOAD_MS = 30_000;
const START_MS = 120_000;
const USAGE = 'usage: node chrome-broker.mjs start | status [PAGE] | open URL [--new-context] | read PAGE | close PAGE | dispose CONTEXT | stop';

const socketPath = () =>
	process.env.CHROME_BROKER_SOCKET ||
	join(process.env.XDG_RUNTIME_DIR || `/run/user/${process.getuid()}`, 'chrome-broker.sock');
const unitName = () => process.env.CHROME_BROKER_UNIT || 'chrome-broker';

function windowsPortFile() {
	const local = execFileSync('cmd.exe', ['/d', '/c', 'echo %LOCALAPPDATA%'], {
		encoding: 'utf8',
		cwd: '/mnt/c',
		stdio: ['ignore', 'pipe', 'ignore'],
	}).trim();
	const folder = execFileSync('wslpath', ['-u', local], { encoding: 'utf8' }).trim();
	return join(folder, 'Google', 'Chrome', 'User Data', 'DevToolsActivePort');
}

// ---- Hiding -------------------------------------------------------------------------------------------------------

const HIDING_RULES = [
	['hex', /[0-9A-Fa-f]{32,}/g, () => '[hidden hex]'],
	[
		'base64',
		/[A-Za-z0-9+/_-]{24,}={0,2}/g,
		(run) => (/[A-Z]/.test(run) && /[a-z]/.test(run) && /[0-9]/.test(run) ? '[hidden base64]' : run),
	],
	['secret-key', /\bA3-[A-Z0-9]+(?:-[A-Z0-9]+)*/g, () => '[hidden key]'],
];

function hideText(text) {
	let out = text
		.split(/(\r?\n)/)
		.map((line) => (line.includes('•') ? '[hidden line]' : line))
		.join('');
	for (const [, pattern, replace] of HIDING_RULES) out = out.replace(pattern, replace);
	return out;
}

function hide(value) {
	if (typeof value === 'string') return hideText(value);
	if (Array.isArray(value)) return value.map(hide);
	if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, hide(v)]));
	return value;
}

// ---- The DevTools connection --------------------------------------------------------------------------------------

class Failure extends Error {}

function connectDevTools(portFile, onClose) {
	const [port, path] = readFileSync(portFile, 'utf8').trim().split(/\r?\n/);
	if (!/^\d+$/.test(port) || !path?.startsWith('/devtools/browser/')) throw new Failure(`${portFile} holds no DevTools address`);
	const socket = new WebSocket(`ws://127.0.0.1:${port}${path}`);
	let nextId = 0;
	const pending = new Map();
	const listeners = new Set();
	const opened = new Promise((resolve, reject) => {
		socket.addEventListener('open', resolve, { once: true });
		socket.addEventListener('error', () => reject(new Failure('Chrome refused the DevTools connection')), { once: true });
	});
	socket.addEventListener('message', (event) => {
		const message = JSON.parse(event.data);
		if (message.id === undefined) {
			for (const listener of listeners) listener(message);
			return;
		}
		const waiter = pending.get(message.id);
		if (!waiter) return;
		pending.delete(message.id);
		clearTimeout(waiter.timer);
		if (message.error) waiter.reject(new Failure(`${waiter.method}: ${message.error.message}`));
		else waiter.resolve(message.result);
	});
	socket.addEventListener('close', () => {
		for (const waiter of pending.values()) waiter.reject(new Failure('disconnected: Chrome closed the DevTools connection'));
		pending.clear();
		onClose();
	});
	const call = (method, params = {}, sessionId) =>
		new Promise((resolve, reject) => {
			const id = ++nextId;
			const timer = setTimeout(() => {
				pending.delete(id);
				reject(new Failure(`${method}: Chrome did not answer within ${CALL_MS / 1000} s`));
			}, CALL_MS);
			pending.set(id, { resolve, reject, method, timer });
			socket.send(JSON.stringify({ id, method, params, ...(sessionId && { sessionId }) }));
		});
	const once = (sessionId, method, ms) => {
		let listener;
		const done = new Promise((resolve) => {
			const timer = setTimeout(() => (listeners.delete(listener), resolve(false)), ms);
			listener = (message) => {
				if (message.sessionId !== sessionId || message.method !== method) return;
				clearTimeout(timer);
				listeners.delete(listener);
				resolve(true);
			};
			listeners.add(listener);
		});
		return done;
	};
	return { opened, call, once, close: () => socket.close() };
}

// ---- The broker ---------------------------------------------------------------------------------------------------

const PAGE_FACTS = `(() => {
	const label = (e) => (e.labels?.[0]?.innerText || e.getAttribute('aria-label') || e.placeholder || '').trim().slice(0, 80);
	const inputs = [...document.querySelectorAll('input, textarea, select')]
		.filter((e) => e.type !== 'hidden')
		.map((e) => ({ tag: e.tagName.toLowerCase(), type: e.type || '', name: e.name || '', id: e.id || '', label: label(e), length: String(e.value ?? '').length }));
	return { url: location.href, title: document.title, visibility: document.visibilityState, text: document.body ? document.body.innerText : '', inputs };
})()`;

class Broker {
	constructor(portFile) {
		this.portFile = portFile;
		this.state = 'disconnected';
		this.error = null;
		this.devtools = null;
		this.pages = new Map();
		this.contexts = new Map();
		this.counter = 0;
	}

	connect() {
		if (this.state !== 'disconnected') return;
		this.state = 'connecting';
		this.error = null;
		let devtools;
		try {
			devtools = connectDevTools(this.portFile, () => {
				if (this.devtools !== devtools) return;
				this.devtools = null;
				this.state = 'disconnected';
				this.error ??= 'Chrome closed the DevTools connection; run start to connect again';
				this.pages.clear();
				this.contexts.clear();
			});
		} catch (error) {
			this.state = 'disconnected';
			this.error = error.code === 'ENOENT' ? `no DevToolsActivePort at ${this.portFile}` : error.message;
			return;
		}
		this.devtools = devtools;
		devtools.opened.then(
			() => {
				if (this.devtools === devtools) this.state = 'connected';
			},
			(error) => {
				if (this.devtools !== devtools) return;
				this.devtools = null;
				this.state = 'disconnected';
				this.error = error.message;
			},
		);
	}

	summary() {
		return {
			state: this.state,
			...(this.error && this.state !== 'connected' && { error: this.error }),
			pid: process.pid,
			pages: [...this.pages].map(([page, { context }]) => ({ page, context })),
			contexts: [...this.contexts.keys()],
		};
	}

	call(method, params, sessionId) {
		if (this.state !== 'connected') throw new Failure(`disconnected: ${this.error ?? 'no DevTools connection'}; run start`);
		return this.devtools.call(method, params, sessionId);
	}

	page(id) {
		const page = this.pages.get(id);
		if (!page) throw new Failure(`${id ?? '(none)'} is not a page this broker opened`);
		return page;
	}

	async evaluate(page, expression) {
		const { result, exceptionDetails } = await this.call(
			'Runtime.evaluate',
			{ expression, returnByValue: true, awaitPromise: true },
			page.sessionId,
		);
		if (exceptionDetails) throw new Failure(`page error: ${exceptionDetails.exception?.description ?? exceptionDetails.text}`);
		return result.value;
	}

	async navigate(page, url) {
		const loaded = this.devtools.once(page.sessionId, 'Page.loadEventFired', LOAD_MS);
		const { errorText } = await this.call('Page.navigate', { url }, page.sessionId);
		if (errorText) throw new Failure(`navigation failed: ${errorText}`);
		return loaded;
	}

	async open(url, newContext) {
		if (!url) throw new Failure('open needs a URL');
		let context = 'default';
		let browserContextId;
		if (newContext) {
			({ browserContextId } = await this.call('Target.createBrowserContext', { disposeOnDetach: true }));
			context = `c${++this.counter}`;
			this.contexts.set(context, browserContextId);
		}
		const { targetId } = await this.call('Target.createTarget', {
			url: 'about:blank',
			newWindow: true,
			...(browserContextId && { browserContextId }),
		});
		const { sessionId } = await this.call('Target.attachToTarget', { targetId, flatten: true });
		const id = `p${++this.counter}`;
		const page = { targetId, sessionId, context };
		this.pages.set(id, page);
		await this.call('Page.enable', {}, sessionId);
		const loaded = await this.navigate(page, url);
		const facts = await this.evaluate(page, '({ url: location.href, visibility: document.visibilityState })');
		return { page: id, context, ...facts, loaded };
	}

	async read(id) {
		return { page: id, ...(await this.evaluate(this.page(id), PAGE_FACTS)) };
	}

	async status(id) {
		if (id === undefined) return this.summary();
		const page = this.page(id);
		const facts = await this.evaluate(page, '({ url: location.href, visibility: document.visibilityState })');
		return { page: id, context: page.context, ...facts };
	}

	async close(id) {
		const page = this.page(id);
		this.pages.delete(id);
		await this.call('Target.closeTarget', { targetId: page.targetId });
		return { closed: id };
	}

	async dispose(context) {
		const browserContextId = this.contexts.get(context);
		if (!browserContextId) throw new Failure(`${context ?? '(none)'} is not a context this broker opened`);
		for (const [id, page] of this.pages) if (page.context === context) this.pages.delete(id);
		this.contexts.delete(context);
		await this.call('Target.disposeBrowserContext', { browserContextId });
		return { disposed: context };
	}

	async shutDown() {
		if (this.state === 'connected') {
			for (const id of [...this.pages.keys()]) await this.close(id).catch(() => {});
			for (const context of [...this.contexts.keys()]) await this.dispose(context).catch(() => {});
		}
		const devtools = this.devtools;
		this.devtools = null;
		this.state = 'disconnected';
		devtools?.close();
		return { stopped: true };
	}

	async run(argv) {
		const [command, ...rest] = argv;
		const flags = new Set(rest.filter((arg) => arg.startsWith('--')));
		const args = rest.filter((arg) => !arg.startsWith('--'));
		const known = { open: ['--new-context'] }[command] ?? [];
		const unknown = [...flags].filter((flag) => !known.includes(flag));
		if (unknown.length) throw new Failure(`${command} does not take ${unknown.join(', ')}`);
		switch (command) {
			case 'status':
				return this.status(args[0]);
			case 'connect':
				this.connect();
				return this.summary();
			case 'open':
				return this.open(args[0], flags.has('--new-context'));
			case 'read':
				return this.read(args[0]);
			case 'close':
				return this.close(args[0]);
			case 'dispose':
				return this.dispose(args[0]);
			case 'stop':
				return this.shutDown();
			default:
				throw new Failure(`unknown command ${command ?? '(none)'}\n${USAGE}`);
		}
	}
}

async function serve(argv) {
	const option = (name) => {
		const at = argv.indexOf(name);
		return at === -1 ? undefined : argv[at + 1];
	};
	const socket = option('--socket') ?? socketPath();
	const portFile = option('--port-file');
	if (!portFile) throw new Failure('serve needs --port-file');
	if (existsSync(socket)) {
		if (await request(socket, ['status']).then(() => true, () => false)) throw new Failure(`a broker already answers on ${socket}`);
		unlinkSync(socket);
	}
	const broker = new Broker(portFile);
	const server = http.createServer((req, res) => {
		let body = '';
		req.on('data', (chunk) => (body += chunk));
		req.on('end', async () => {
			let status = 200;
			let reply;
			try {
				const { argv: commandArgv } = JSON.parse(body);
				reply = { ok: true, result: await broker.run(commandArgv) };
			} catch (error) {
				status = error instanceof Failure ? 400 : 500;
				reply = { ok: false, error: error.message };
			}
			res.on('error', () => {});
			res.writeHead(status, { 'content-type': 'application/json' });
			res.end(JSON.stringify(hide(reply)));
			if (reply.result?.stopped) server.close(() => process.exit(0));
		});
	});
	const quit = () => broker.shutDown().finally(() => process.exit(0));
	process.on('SIGTERM', quit);
	process.on('SIGINT', quit);
	const umask = process.umask(0o177);
	await new Promise((resolve, reject) => server.listen(socket, resolve).once('error', reject));
	process.umask(umask);
	chmodSync(socket, 0o600);
	process.on('exit', () => {
		try {
			unlinkSync(socket);
		} catch {}
	});
	broker.connect();
}

// ---- The client ---------------------------------------------------------------------------------------------------

function request(socket, argv) {
	return new Promise((resolve, reject) => {
		const req = http.request({ socketPath: socket, path: '/', method: 'POST', headers: { 'content-type': 'application/json' } }, (res) => {
			let body = '';
			res.setEncoding('utf8');
			res.on('data', (chunk) => (body += chunk));
			res.on('end', () => {
				try {
					resolve(JSON.parse(body));
				} catch {
					reject(new Failure('the broker sent an unreadable reply'));
				}
			});
		});
		req.on('error', (error) =>
			reject(
				['ENOENT', 'ECONNREFUSED'].includes(error.code)
					? new Failure('no broker is running; start one with: node chrome-broker.mjs start')
					: error,
			),
		);
		req.end(JSON.stringify({ argv }));
	});
}

async function waitWhileConnecting(socket) {
	const deadline = Date.now() + START_MS;
	for (;;) {
		const reply = await request(socket, ['status']).catch(() => null);
		if (reply && reply.result.state !== 'connecting') return reply;
		if (Date.now() > deadline) return reply;
		await new Promise((resolve) => setTimeout(resolve, 250));
	}
}

async function start() {
	const socket = socketPath();
	const running = await request(socket, ['status']).catch(() => null);
	let reply;
	if (running) {
		if (running.result.state === 'disconnected') await request(socket, ['connect']);
		reply = await waitWhileConnecting(socket);
		if (reply && running.result.state !== 'disconnected') reply.result.note = 'already running';
	} else {
		const portFile = process.env.CHROME_BROKER_PORT_FILE || windowsPortFile();
		if (!existsSync(portFile))
			throw new Failure(`no DevToolsActivePort at ${portFile}; turn on remote debugging at chrome://inspect/#remote-debugging`);
		const launched = spawnSync(
			'systemd-run',
			['--user', `--unit=${unitName()}`, '--collect', '--quiet', process.execPath, SCRIPT, 'serve', '--socket', socket, '--port-file', portFile],
			{ encoding: 'utf8' },
		);
		if (launched.status !== 0) throw new Failure(`systemd-run could not start the broker: ${(launched.stderr || launched.error?.message || '').trim()}`);
		reply = await waitWhileConnecting(socket);
	}
	if (!reply) throw new Failure(`the broker did not answer; read: journalctl --user -u ${unitName()}`);
	if (reply.result.state === 'connecting')
		throw new Failure('still waiting for Chrome to accept the connection, such as an "Allow remote debugging?" prompt; check again with status');
	if (reply.result.state !== 'connected') throw new Failure(reply.result.error ?? 'the broker is not connected');
	return reply;
}

async function main(argv) {
	const [command] = argv;
	if (!command || command === '--help' || command === '-h') {
		console.log(USAGE);
		return command ? 0 : 2;
	}
	if (command === 'serve') {
		await serve(argv.slice(1));
		return null;
	}
	const reply = command === 'start' ? await start() : await request(socketPath(), argv);
	if (!reply.ok) {
		console.error(reply.error);
		return 1;
	}
	console.log(JSON.stringify(reply.result));
	return 0;
}

if (import.meta.main) {
	main(process.argv.slice(2)).then(
		(code) => {
			if (code !== null) process.exitCode = code;
		},
		(error) => {
			console.error(hideText(error.message));
			process.exitCode = 1;
		},
	);
}
