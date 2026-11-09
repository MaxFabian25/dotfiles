#!/usr/bin/env node
// Drive one agent-browser session's page over the Chrome DevTools Protocol, for what agent-browser's own
// commands cannot do: real pointer presses, forced :hover states, Emulation.* settings, awaited evaluations.
//
//   node cdp.mjs NAME < steps.json        run each [method, params] step on NAME's current page
//   node cdp.mjs --url WS NAME < steps.json
//
// A step is ["Domain.method", {params}] or ["sleep", ms]. Each result prints as one JSON line, and the first
// DevTools error stops the run with exit 1. Focus emulation is on before the first step, because a headless
// tab that thinks it is in the background throttles its timers and requestAnimationFrame.
//
// As a module: const page = await connect('NAME'); await page.send('Runtime.evaluate', {...}); page.close().
// The socket and page URL come through ab_session.py when NAME saved launch flags there, because a command
// without them restarts the session's browser.

import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

function agentBrowser(name, args) {
	const saved = join(homedir(), '.local/state/agent-browser-sessions', `${name}.json`);
	const [file, argv] = existsSync(saved)
		? ['python3', [join(dirname(fileURLToPath(import.meta.url)), 'ab_session.py'), 'run', name, ...args]]
		: ['agent-browser', ['--session', name, ...args]];
	return execFileSync(file, argv, { encoding: 'utf8' }).trim();
}

export async function connect(name, { url } = {}) {
	const socketUrl = url ?? agentBrowser(name, ['get', 'cdp-url']);
	const pageUrl = agentBrowser(name, ['get', 'url']);
	const socket = new WebSocket(socketUrl);
	await new Promise((resolve, reject) => {
		socket.addEventListener('open', resolve, { once: true });
		socket.addEventListener('error', () => reject(new Error(`cannot open ${socketUrl}`)), { once: true });
	});
	let nextId = 0;
	const pending = new Map();
	socket.addEventListener('message', (event) => {
		const message = JSON.parse(event.data);
		const waiter = pending.get(message.id);
		if (!waiter) return;
		pending.delete(message.id);
		if (message.error) waiter.reject(new Error(`${waiter.method}: ${message.error.message}`));
		else waiter.resolve(message.result);
	});
	const call = (method, params = {}, sessionId) =>
		new Promise((resolve, reject) => {
			const id = ++nextId;
			pending.set(id, { resolve, reject, method });
			socket.send(JSON.stringify({ id, method, params, ...(sessionId && { sessionId }) }));
		});

	const { targetInfos } = await call('Target.getTargets');
	const pages = targetInfos.filter((target) => target.type === 'page' && target.url === pageUrl);
	if (pages.length !== 1) {
		socket.close();
		throw new Error(`${pages.length} page targets show ${pageUrl}, so the session's page is ambiguous`);
	}
	const { sessionId } = await call('Target.attachToTarget', { targetId: pages[0].targetId, flatten: true });
	await call('Emulation.setFocusEmulationEnabled', { enabled: true }, sessionId);
	return {
		send: (method, params) => call(method, params, sessionId),
		close: async () => {
			await call('Target.detachFromTarget', { sessionId });
			socket.close();
		},
	};
}

if (import.meta.main) {
	const args = process.argv.slice(2);
	const urlAt = args.indexOf('--url');
	const url = urlAt === -1 ? undefined : args.splice(urlAt, 2)[1];
	const [name] = args;
	if (!name) {
		console.error('usage: node cdp.mjs [--url WS] NAME < steps.json');
		process.exit(2);
	}
	const steps = JSON.parse(readFileSync(0, 'utf8'));
	const page = await connect(name, { url });
	try {
		for (const [index, [method, params]] of steps.entries()) {
			if (method === 'sleep') {
				await new Promise((resolve) => setTimeout(resolve, params));
				continue;
			}
			console.log(JSON.stringify({ step: index, method, result: await page.send(method, params) }));
		}
	} catch (error) {
		console.error(error.message);
		process.exitCode = 1;
	} finally {
		await page.close();
	}
}
