// Runs a cua-driver script from a file or stdin and prints only its console output and return value.
// `cua-driver call run_script` needs the script wrapped in JSON and answers with a per-call log as well.
// Usage: node cua-js.mjs [--session NAME] [--timeout-ms N] [--max-calls N] [--calls] [FILE]
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";

const args = process.argv.slice(2);
const request = {};
let file = null;
let calls = false;
for (let i = 0; i < args.length; i++) {
  const flag = args[i];
  if (flag === "--calls") calls = true;
  else if (flag === "--session") request.session = args[++i];
  else if (flag === "--timeout-ms") request.timeout_ms = Number(args[++i]);
  else if (flag === "--max-calls") request.max_calls = Number(args[++i]);
  else if (flag.startsWith("--") || file !== null) {
    console.error("Usage: node cua-js.mjs [--session NAME] [--timeout-ms N] [--max-calls N] [--calls] [FILE]");
    process.exit(2);
  } else file = flag;
}
request.script = readFileSync(file ?? 0, "utf8");

const run = spawnSync("cua-driver", ["call", "run_script"], { input: JSON.stringify(request), encoding: "utf8" });
if (run.error) {
  console.error(`cua-driver did not start: ${run.error.message}`);
  process.exit(2);
}
let result;
try {
  result = JSON.parse(run.stdout);
} catch {
  process.stderr.write(run.stdout + run.stderr);
  process.exit(run.status || 2);
}

for (const line of result.console ?? []) console.log(`console: ${line}`);
if (result.console_truncated) console.log("console: (truncated)");
const log = result.calls ?? [];
if (calls) {
  for (const c of log) console.log(`call ${c.call} line ${c.line} ${c.op} ${c.ms}ms ${c.ok ? "ok" : "failed"}: ${c.detail ?? ""}`);
}
if (result.ok) {
  const value = result.value;
  if (value !== undefined) console.log(typeof value === "string" ? value : JSON.stringify(value));
  process.exit(0);
}
const error = result.error ?? {};
const where = [error.line != null && `line ${error.line}`, error.op, error.call != null && `call ${error.call}`].filter(Boolean);
console.error(`error${where.length ? ` (${where.join(", ")})` : ""}: ${error.message ?? JSON.stringify(result)}`);
process.exit(1);
