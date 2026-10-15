---
name: browser-and-computer-use
description: Use before browser automation, verification, web development testing, or native desktop control. Both Claude Code and Codex use Playwright CLI for browsers on macOS, Windows, and WSL. Native apps use Codex Computer Use on macOS and Windows, and cua-driver on Linux.
---

# Browser and computer use

Use only the tools named here. Do not reach for agent-browser, the Playwright library or Playwright MCP, Puppeteer, chrome-devtools-mcp, or AppleScript UI scripting.

## Browser

Use `playwright-cli` from the shell for browser automation, verification, and web development testing on every host, in both Claude Code and Codex. Run `playwright-cli --help` once per session and read the official installed skill at `~/.agents/skills/playwright-cli/SKILL.md` (Codex) or `~/.claude/skills/playwright-cli/SKILL.md` (Claude Code). This shared policy supplies host routing and session ownership; the official skill supplies commands. Keep existing repository test suites; run their tests through the repository's documented commands.

The CLI and official skill are installed through the pinned mise package and `playwright-cli install --skills=agents -g` / `playwright-cli install --skills=claude -g`. Codex discovers `~/.agents/skills`; a second copy in `~/.codex/skills` is unnecessary. Browser plugins and Claude in Chrome are disabled as automation defaults. Native desktop control follows the separate Computer use section below.

Playwright CLI drives the host's installed Google Chrome, headless by default. These rules come from running it on all three hosts:

- Give every browser a session name of your own: `playwright-cli -s=<name> open <url>`, then the same `-s=<name>` on every later command. Without a name, commands share one default browser with every other agent on the machine. Close only your sessions, with `playwright-cli -s=<name> close`. Never run `close-all` or `kill-all`, because they close other agents' browsers too.
- Set `PLAYWRIGHT_MCP_OUTPUT_DIR` to a scratch directory on the `open` command (in PowerShell, `$env:PLAYWRIGHT_MCP_OUTPUT_DIR = '<dir>'` first). The session then writes its snapshots, screenshots and downloads there. Without it they land in `.playwright-cli/` in the current directory, which litters a repository checkout.
- `open` starts the browser. Use `goto <url>` to navigate a session that is already open, because a second `open` does not navigate.
- If a command prints a snapshot link, read its relevant part for element refs such as `e15`. After input, if the next action depends on asynchronous changes, await the expected visible state and take a scoped snapshot for fresh discovery and refs. Use `find <text>` for discovery or `snapshot <known-container>` for a region; add `--depth=<n>` when needed. Expand to the full page when the task needs it. To target an element by role or label instead, pass a Playwright locator: `click "getByRole('button', { name: 'Gem', exact: true })"`.
- There is no wait command. Wait for text with `run-code "async page => { await page.getByText('Saved').filter({ visible: true }).first().waitFor() }"`. Keep the visible filter, because a hidden copy of the same text, such as one in a collapsed menu, matches first.
- A click that starts a download saves the file in the output directory and prints `Downloaded file <name> to "<path>"`. Copy it from there.
- Prefer `--raw` when only the result matters, for example `playwright-cli -s=<name> --raw eval "document.title"`. This trims returned text; it does not skip automatic snapshot work.
- After inspecting the page, batch a known sequence of related actions in one `playwright-cli -s=<name> --raw run-code` call. Await the expected visible result or URL, check the resulting state, and return compact JSON. Keep a snapshot checkpoint whenever the next action depends on what the page shows. This saves repeated CLI startup, settling, snapshots, and remote shell calls.
- Put longer callbacks in a scratch JavaScript file and use `run-code --filename=<file>`. Use this for multiline code on Windows, where a native launcher can misquote inline JavaScript.
- Sessions belong to the install. Do not upgrade or reinstall Playwright CLI while a session is open, because the new install cannot see the old sessions.

Host notes:

- For an existing sign-in, inspect `playwright-cli list` and known task profiles first. Session names from Claude tools do not identify Playwright sessions. Reuse only a session owned by this task; leave other agents' sessions alone. To use the user's running Chrome, try `playwright-cli -s=<name> attach --cdp=chrome` if remote debugging is already enabled, or `attach --extension=chrome` if the Playwright extension is installed. Detach when finished. If neither connection is available and sign-in is required, open a dedicated headed persistent profile for the human to sign in. Keep authentication state outside repositories and evidence, and never print cookies or tokens.
- A Chrome on another machine reaches that machine's `localhost`, not yours. The Windows Chrome reaches dev servers in WSL on `127.0.0.1`, because WSL uses mirrored networking.
- To open a dev server from another host, use an existing task-owned tailnet share or run `tailscale serve <port>` in the foreground. Record the process ID and printed URL. Stop only that process when finished and confirm its URL no longer loads; a foreground share never shows in `tailscale serve status`.
- Codex's in-app browser (`iab`) only exists inside the Codex desktop app. Do not try it elsewhere.
- Playwright CLI runs on the same host as you, so `localhost` reaches dev servers there.
- Windows over SSH: OpenSSH ends browser child processes when its connection exits. Keep the opening connection alive with `scripts/windows-session.ps1 -Session <name> -OutputDirectory <scratch> -Url <url>` (add `-Persistent` when needed), then send later CLI commands over separate SSH calls. Closing that named session ends the helper. Native Windows shells can use the CLI directly. WSL sessions survive separate SSH calls.
- To attach to an already running Chrome or Electron app over CDP, use `playwright-cli -s=<name> attach --cdp=http://localhost:<port>`, and `playwright-cli -s=<name> detach` when done so the app keeps running.

## Computer use

Prefer a CLI, an API, or the browser tools when they can do the job. Use desktop control only for native apps.

- macOS: OpenAI's Codex Computer Use, through the `cua_repl` server's `js` tool, for Codex and Claude Code alike. Claude Code never uses Claude's own computer use there: not `mcp__computer-use__*`, not the `anthropic-skills:computer-use` skill, and never ask the user to turn on Computer use in the Claude desktop app. If `cua_repl` is missing, say so and stop. Start with `await cua.getState();` and follow the documentation it returns. An app must be approved before use. If a call fails with "was not approved to use <app>", ask the user to approve that app once from a Codex thread, which shows the approval prompt, then retry.
- Windows: OpenAI's Codex Computer Use, through the `node_repl` server's `js` tool. First read Codex's skill at `~/.codex/plugins/cache/openai-bundled/computer-use/<newest version>/skills/computer-use/SKILL.md` and follow its Initialize section. It needs the Codex desktop app running. If a call fails with "Computer Use native pipe is unavailable", ask the user to open the Codex app, then retry.
- Linux (WSL): the open-source `cua-driver` server's MCP tools. Call them directly rather than the `cua-driver` CLI, which needs a background service that is not running. Its own `cua-driver` skill documents the tools. The server draws on a private virtual display (`:99`), so the user cannot watch, and apps you launch there are invisible to them.

## Maintenance

The durable source is `skills/browser-and-computer-use` in the dotfiles checkout. Maintain it there and refresh its reviewed source lock before device sync. Tool versions live in `config/mise/config.toml`; host-specific integrations stay in the private configuration. Before upgrading Playwright, finish or hand back all live sessions. Install the matching official CLI skill on each host and verify discovery. Preserve native computer-use integrations independently of browser updates.

To measure a CLI workflow change, run `python3 <skill-dir>/benchmarks/benchmark.py --output <new-scratch-directory>`. The [probe](benchmarks/benchmark.py) uses a synthetic local form, five interleaved trials per workflow, exact saved-state checks, and owned-session teardown. Its results measure that local flow and CLI bytes, not tokens or whole-agent website speed. Keep the CLI's global waits; tune a slower task from its own evidence.
