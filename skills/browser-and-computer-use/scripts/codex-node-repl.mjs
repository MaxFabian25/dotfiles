// Starts Codex's Windows computer-use server (node_repl) for Claude Code.
// The Codex app rewrites [mcp_servers.node_repl] in ~/.codex/config.toml on every launch with a fresh
// native-pipe name, so the settings are read at spawn time instead of being copied into Claude's config.
import { readFileSync } from "node:fs";
import { spawn } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

const toml = readFileSync(join(homedir(), ".codex", "config.toml"), "utf8");

function table(name) {
  const header = `[${name}]`;
  const start = toml.split(/\r?\n/).findIndex((line) => line.trim() === header);
  if (start < 0) return {};
  const values = {};
  for (const line of toml.split(/\r?\n/).slice(start + 1)) {
    if (line.startsWith("[")) break;
    const m = line.match(/^([A-Za-z0-9_]+)\s*=\s*(?:'([^']*)'|"((?:[^"\\]|\\.)*)")\s*$/);
    if (m) values[m[1]] = m[2] ?? JSON.parse(`"${m[3]}"`);
  }
  return values;
}

const { command } = table("mcp_servers.node_repl");
if (!command) {
  console.error("No [mcp_servers.node_repl] command in ~/.codex/config.toml. Open the Codex app once so it writes one.");
  process.exit(1);
}

const env = table("mcp_servers.node_repl.env");
for (const key of Object.keys(env)) {
  if (/^BROWSER_USE_|^NODE_REPL_INSTRUCTIONS_USE_CASE_(BROWSER|CHROME)$/.test(key)) delete env[key];
}
env.NODE_REPL_TRUSTED_SERVICES = JSON.stringify({ sky: "@oai/sky/service" });

const child = spawn(command, [], { stdio: "inherit", env: { ...process.env, ...env } });
child.on("exit", (code, signal) => process.exit(code ?? (signal ? 1 : 0)));
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => child.kill(signal));
