#!/usr/bin/env node
// One long-lived DevTools connection to the user's Windows Chrome, shared by every agent command, so Chrome asks
// "Allow remote debugging?" once per start and no client of its own leaves that prompt behind. Run with --help for
// the commands.
//
// Commands reach only the pages and contexts the broker opened. Every reply and error hides every held value, raw
// and JSON-escaped, input values (as their length), hex runs of 32 characters or more, base64 runs of 24 or more
// that mix upper case, lower case and digits, 1Password Secret Key groups (A3-...) and every line that holds a "•".
// Clients talk to the broker over HTTP on a Unix socket with mode 0600, never TCP, because under WSL's mirrored
// networking a localhost port is open to every Windows program and to every page in Chrome. When Chrome drops the
// connection, the broker answers "disconnected" and never reconnects by itself, since each connection raises a new
// prompt on the user's screen; `start` reconnects.
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
const ELEMENT_MS = 10_000;
const START_MS = 120_000;
const USAGE = `usage: node chrome-broker.mjs start | stop | status [PAGE] | COMMAND ...
  start                          start the broker as a systemd user unit, or report the running one
  stop                           close the broker's pages and contexts, then its connection
  status [PAGE]                  the connection and the broker's pages, or one page's URL and visibility
  open URL [--new-context | --context CONTEXT]
                                 a page in a window of its own, in the user's profile, a new cookie-less context
                                 or one the broker opened; prints its page ID, context ID and "visible" or "hidden"
  nav PAGE URL | activate PAGE | close PAGE | dispose CONTEXT
                                 dispose closes a cookie-less context and every page in it
  read PAGE [--fresh] [--frame URL-PART]
                                 the text, and each input with its value's length only; --fresh, for a
                                 time-sensitive value such as a one-time code, is refused while the page is hidden,
                                 because Chrome stops rendering hidden pages
  eval PAGE EXPRESSION [--frame URL-PART | --hold NAME]
                                 the expression's value, awaited; --frame runs it in each frame whose URL holds
                                 URL-PART; --hold keeps the value as NAME and prints its length
  click PAGE SELECTOR | type PAGE SELECTOR TEXT [--enter] | key PAGE KEY
                                 a real mouse click at the element's centre, plain text typed, or one key
                                 (Enter, Tab, Escape, Backspace, Space or an arrow)
Held values carry a secret from one page to another without showing it:
  store NAME < value             hold a value given on standard input, never as an argument
  read-field PAGE SELECTOR NAME  hold a field's value; prints its length
  type-held PAGE SELECTOR NAME [--enter]
  eq-held PAGE SELECTOR NAME     "yes" when the field holds the value, otherwise "no"
  held | drop NAME               list names and lengths, or forget one
store, read-field and eval --hold refuse a mask, a value made only of •, ●, * or ·, as a password manager shows in
place of a password. SELECTOR is a CSS selector, or text=Words for a visible button, link, label or option whose
text is or holds Words; commands wait up to ${ELEMENT_MS / 1000} s for it to appear.`;
const MASK = /^[•●*·]+$/u;
const MASK_REFUSAL = 'this is a mask; read the item with `op read` or press Reveal first';

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

// A held value's raw form and its JSON-escaped form, longest first, so no shorter form splits a longer one.
function heldForms(held) {
	const forms = [];
	for (const [name, value] of held) {
		const escaped = JSON.stringify(value).slice(1, -1);
		forms.push([value, name], [escaped, name], [JSON.stringify(escaped).slice(1, -1), name]);
	}
	return forms.sort((a, b) => b[0].length - a[0].length);
}

function hideHeld(text, held) {
	let out = text;
	for (const [form, name] of heldForms(held)) out = out.split(form).join(`[hidden held ${name}]`);
	return out;
}

function hideText(text, held = new Map()) {
	let out = hideHeld(text, held)
		.split(/(\r?\n)/)
		.map((line) => (line.includes('•') ? '[hidden line]' : line))
		.join('');
	for (const [, pattern, replace] of HIDING_RULES) out = out.replace(pattern, replace);
	return out;
}

function hide(value, held) {
	if (typeof value === 'string') return hideText(value, held);
	if (Array.isArray(value)) return value.map((item) => hide(item, held));
	if (value && typeof value === 'object')
		return Object.fromEntries(Object.entries(value).map(([k, v]) => [hideText(k, held), hide(v, held)]));
	return value;
}

function refuseMask(value) {
	if (typeof value !== 'string' || value.trim() === '') throw new Failure('the value is empty; nothing was held');
	if (MASK.test(value.trim())) throw new Failure(MASK_REFUSAL);
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
	return { opened, call, once, listen: (listener) => listeners.add(listener), close: () => socket.close() };
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
		this.held = new Map();
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
		devtools.listen((message) => this.trackFrames(message));
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

	// Chrome attaches each out-of-process iframe of a broker page to that page's session, as setAutoAttach asks.
	trackFrames({ method, params, sessionId }) {
		if (method === 'Target.attachedToTarget' && params.targetInfo.type === 'iframe') {
			for (const page of this.pages.values()) if (page.sessionId === sessionId) page.frames.add(params.sessionId);
		}
		if (method === 'Target.detachedFromTarget') for (const page of this.pages.values()) page.frames.delete(params.sessionId);
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

	async evaluate(page, expression, { sessionId = page.sessionId, contextId } = {}) {
		const { result, exceptionDetails } = await this.call(
			'Runtime.evaluate',
			{ expression, returnByValue: true, awaitPromise: true, ...(contextId && { contextId }) },
			sessionId,
		);
		if (exceptionDetails) throw new Failure(`page error: ${exceptionDetails.exception?.description ?? exceptionDetails.text}`);
		return result.value;
	}

	// The page's frames whose URL contains MATCH: same-process frames in an isolated world, and out-of-process
	// iframes through their own sessions.
	async frames(id, match) {
		const page = this.page(id);
		const found = [];
		const { frameTree } = await this.call('Page.getFrameTree', {}, page.sessionId);
		const visit = async (node) => {
			if (node.frame.url.includes(match)) {
				const world = await this.call('Page.createIsolatedWorld', { frameId: node.frame.id, worldName: 'chrome-broker' }, page.sessionId).catch(() => null);
				if (world) found.push({ url: node.frame.url, sessionId: page.sessionId, contextId: world.executionContextId });
			}
			for (const child of node.childFrames ?? []) await visit(child);
		};
		await visit(frameTree);
		for (const sessionId of page.frames) {
			const url = await this.evaluate(page, 'location.href', { sessionId }).catch(() => null);
			if (url?.includes(match) && !found.some((frame) => frame.url === url)) found.push({ url, sessionId });
		}
		if (!found.length) throw new Failure(`no frame of ${id} has a URL containing ${match}`);
		return found;
	}

	async inFrames(id, match, expression) {
		const page = this.page(id);
		const results = [];
		for (const { url, ...where } of await this.frames(id, match)) {
			const value = await this.evaluate(page, expression, where).then(
				(found) => ({ value: found }),
				(error) => ({ error: error.message }),
			);
			results.push({ frame: url, ...value });
		}
		return results;
	}

	// Runs fn(element, ...args) on the element SELECTOR finds, waiting up to ELEMENT_MS for it to appear, and returns
	// fn's result by value. "text=Words" finds a visible button, link, label or option whose text is Words, or holds it.
	async onElement(page, selector, fn, ...args) {
		if (!selector) throw new Failure('a selector is needed');
		const expression = `(() => {
			const selector = ${JSON.stringify(selector)};
			const find = () => {
				if (!selector.startsWith('text=')) return document.querySelector(selector);
				const want = selector.slice(5).trim().toLowerCase();
				const shown = [...document.querySelectorAll('button, a, label, summary, [role=button], [role=link], [role=option], [role=menuitem], [role=tab], input[type=submit], input[type=button]')]
					.filter((e) => e.getClientRects().length)
					.map((e) => [e, (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').toLowerCase()]);
				return (shown.find(([, text]) => text === want) ?? shown.find(([, text]) => text.includes(want)))?.[0];
			};
			const element = find();
			if (!element) return { missing: true };
			return { value: (${fn})(element, ...${JSON.stringify(args)}) };
		})()`;
		const deadline = Date.now() + ELEMENT_MS;
		for (;;) {
			const reply = await this.evaluate(page, expression).catch((error) => {
				if (/context|navigat|Cannot find/i.test(error.message) && Date.now() < deadline) return { missing: true };
				throw error;
			});
			if (!reply.missing) return reply.value;
			if (Date.now() > deadline) throw new Failure(`nothing on the page matches ${selector}`);
			await new Promise((resolve) => setTimeout(resolve, 200));
		}
	}

	async click(id, selector) {
		const page = this.page(id);
		const [x, y] = await this.onElement(page, selector, (element) => {
			element.scrollIntoView({ block: 'center', inline: 'center' });
			const box = element.getBoundingClientRect();
			return [box.x + box.width / 2, box.y + box.height / 2];
		});
		for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased'])
			await this.call('Input.dispatchMouseEvent', { type, x, y, button: 'left', clickCount: type === 'mouseMoved' ? 0 : 1 }, page.sessionId);
		return { clicked: selector };
	}

	async type(id, selector, text, enter) {
		if (text === undefined) throw new Failure('type needs PAGE SELECTOR TEXT');
		const page = this.page(id);
		await this.onElement(page, selector, (element) => {
			element.focus();
			element.select?.();
		});
		await this.call('Input.insertText', { text }, page.sessionId);
		if (enter) await this.press(page, 'Enter');
		return { typed: text.length };
	}

	heldValue(name) {
		if (!this.held.has(name)) throw new Failure(`nothing is held as ${name ?? '(none)'}`);
		return this.held.get(name);
	}

	store(name, value) {
		if (!name) throw new Failure('store needs a NAME');
		this.held.set(name, refuseMask(value));
		return { held: name, length: value.length };
	}

	async readField(id, selector, name) {
		if (!name) throw new Failure('read-field needs PAGE SELECTOR NAME');
		const value = await this.onElement(this.page(id), selector, (element) => String(element.value ?? ''));
		return this.store(name, value);
	}

	async typeHeld(id, selector, name, enter) {
		const value = this.heldValue(name);
		const page = this.page(id);
		await this.onElement(page, selector, (element) => {
			element.focus();
			element.select?.();
		});
		await this.call('Input.insertText', { text: value }, page.sessionId);
		if (enter) await this.press(page, 'Enter');
		return { typed: name, length: value.length };
	}

	async eqHeld(id, selector, name) {
		const value = this.heldValue(name);
		const field = await this.onElement(this.page(id), selector, (element) => String(element.value ?? ''));
		return { held: name, same: field === value ? 'yes' : 'no' };
	}

	async press(page, key) {
		const keys = { Enter: [13, '\r'], Tab: [9], Escape: [27], Backspace: [8], ArrowDown: [40], ArrowUp: [38], ArrowLeft: [37], ArrowRight: [39], Space: [32, ' '] };
		if (!Object.hasOwn(keys, key)) throw new Failure(`unknown key ${key}; use one of ${Object.keys(keys).join(', ')}`);
		const [windowsVirtualKeyCode, text] = keys[key];
		const event = { key: key === 'Space' ? ' ' : key, code: key, windowsVirtualKeyCode };
		await this.call('Input.dispatchKeyEvent', { type: 'keyDown', ...event, ...(text && { text }) }, page.sessionId);
		await this.call('Input.dispatchKeyEvent', { type: 'keyUp', ...event }, page.sessionId);
	}

	async navigate(page, url) {
		const loaded = this.devtools.once(page.sessionId, 'Page.loadEventFired', LOAD_MS);
		const { errorText } = await this.call('Page.navigate', { url }, page.sessionId);
		if (errorText) throw new Failure(`navigation failed: ${errorText}`);
		return loaded;
	}

	async open(url, newContext, inContext) {
		if (!url) throw new Failure('open needs a URL');
		let context = 'default';
		let browserContextId;
		if (inContext !== undefined) {
			if (newContext) throw new Failure('open takes --new-context or --context, not both');
			browserContextId = this.contexts.get(inContext);
			if (!browserContextId) throw new Failure(`${inContext} is not a context this broker opened`);
			context = inContext;
		} else if (newContext) {
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
		const page = { targetId, sessionId, context, frames: new Set() };
		this.pages.set(id, page);
		await this.call('Page.enable', {}, sessionId);
		await this.call('Target.setAutoAttach', { autoAttach: true, waitForDebuggerOnStart: false, flatten: true }, sessionId);
		return { page: id, context, ...(await this.nav(id, url)) };
	}

	async nav(id, url) {
		if (!url) throw new Failure('nav needs PAGE URL');
		const page = this.page(id);
		const loaded = await this.navigate(page, url);
		const facts = await this.evaluate(page, '({ url: location.href, visibility: document.visibilityState })');
		return { ...facts, loaded };
	}

	async read(id, fresh, frame) {
		const facts = await this.evaluate(this.page(id), PAGE_FACTS);
		if (facts.visibility === 'hidden' && fresh)
			throw new Failure(
				`${id} is hidden, and Chrome stops rendering hidden pages, so a fresh read could show a stopped countdown or an unrendered page; bring its window to the front and read again`,
			);
		const note = facts.visibility === 'hidden' && { note: 'this page is hidden, and Chrome stops rendering hidden pages' };
		if (frame !== undefined) {
			const frames = (await this.inFrames(id, frame, PAGE_FACTS)).map(({ value, ...where }) => ({ ...where, ...value }));
			return { page: id, ...note, visibility: facts.visibility, frames };
		}
		return { page: id, ...note, ...facts };
	}

	async evalCommand(id, expression, frame, hold) {
		if (expression === undefined) throw new Failure('eval needs PAGE EXPRESSION');
		if (frame !== undefined && hold !== undefined) throw new Failure('eval takes --frame or --hold, not both');
		if (frame !== undefined) return { frames: await this.inFrames(id, frame, expression) };
		const value = await this.evaluate(this.page(id), expression);
		if (hold === undefined) return { value };
		return this.store(hold, typeof value === 'string' ? value : JSON.stringify(value) ?? '');
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

	async run(argv, stdin) {
		const [command, ...rest] = argv;
		const known = {
			open: ['--new-context', '--context='],
			read: ['--fresh', '--frame='],
			eval: ['--frame=', '--hold='],
			type: ['--enter'],
			'type-held': ['--enter'],
		}[command] ?? [];
		const flags = new Map();
		const args = [];
		for (let at = 0; at < rest.length; at++) {
			const arg = rest[at];
			if (!arg.startsWith('--')) args.push(arg);
			else if (known.includes(arg)) flags.set(arg, true);
			else if (known.includes(`${arg}=`) && at + 1 < rest.length) flags.set(arg, rest[++at]);
			else throw new Failure(`${command} does not take ${arg}${known.includes(`${arg}=`) ? ' without a value' : ''}`);
		}
		switch (command) {
			case 'status':
				return this.status(args[0]);
			case 'connect':
				this.connect();
				return this.summary();
			case 'open':
				return this.open(args[0], flags.has('--new-context'), flags.get('--context'));
			case 'nav':
				return { page: args[0], ...(await this.nav(args[0], args[1])) };
			case 'activate':
				await this.call('Target.activateTarget', { targetId: this.page(args[0]).targetId });
				return { activated: args[0] };
			case 'read':
				return this.read(args[0], flags.has('--fresh'), flags.get('--frame'));
			case 'eval':
				return this.evalCommand(args[0], args[1], flags.get('--frame'), flags.get('--hold'));
			case 'click':
				return this.click(args[0], args[1]);
			case 'type':
				return this.type(args[0], args[1], args[2], flags.has('--enter'));
			case 'key':
				await this.press(this.page(args[0]), args[1]);
				return { pressed: args[1] };
			case 'store':
				if (args.length !== 1 || stdin === undefined)
					throw new Failure('store takes NAME alone and its value on standard input only, as in: store NAME < file');
				return this.store(args[0], stdin);
			case 'read-field':
				return this.readField(...args.slice(0, 3));
			case 'type-held':
				return this.typeHeld(args[0], args[1], args[2], flags.has('--enter'));
			case 'eq-held':
				return this.eqHeld(...args.slice(0, 3));
			case 'held':
				return [...this.held].map(([name, value]) => ({ name, length: value.length }));
			case 'drop':
				if (!this.held.delete(args[0])) throw new Failure(`nothing is held as ${args[0] ?? '(none)'}`);
				return { dropped: args[0] };
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
				const { argv: commandArgv, stdin } = JSON.parse(body);
				reply = { ok: true, result: await broker.run(commandArgv, stdin) };
			} catch (error) {
				status = error instanceof Failure ? 400 : 500;
				reply = { ok: false, error: error.message };
			}
			res.on('error', () => {});
			res.writeHead(status, { 'content-type': 'application/json' });
			res.end(hideHeld(JSON.stringify(hide(reply, broker.held)), broker.held));
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

function request(socket, argv, stdin) {
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
		req.end(JSON.stringify({ argv, ...(stdin !== undefined && { stdin }) }));
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
	let stdin;
	if (command === 'store') {
		if (argv.length !== 2) throw new Failure('store takes NAME alone and its value on standard input only, as in: store NAME < file');
		if (process.stdin.isTTY) throw new Failure('store reads its value from standard input, as in: store NAME < file');
		stdin = readFileSync(0, 'utf8').replace(/\r?\n$/, '');
	}
	const reply = command === 'start' ? await start() : await request(socketPath(), argv, stdin);
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
