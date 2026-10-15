from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import tomllib

import portable
MODEL_FILE = ".config/pstack/models.md"
CONFIG_FILE = ".codex/config.toml"
OVERRIDE_KEY = "expose_spawn_agent_model_overrides"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def patch_config(data, enabled):
    text = data.decode("utf-8")
    before = tomllib.loads(text.lstrip("\ufeff"))
    section = re.search(r"(?m)^\[features\.multi_agent_v2\][ \t]*(?:#[^\r\n]*)?\r?$", text)
    if section is None:
        raise ValueError("Codex config has no explicit multi_agent_v2 section; review it before synchronization")
    start = section.end()
    next_section = re.search(r"(?m)^\[", text[start:])
    end = start + next_section.start() if next_section else len(text)
    body = text[start:end]
    pattern = r"(?m)^([ \t]*expose_spawn_agent_model_overrides[ \t]*=[ \t]*)(true|false)([ \t]*(?:#[^\r\n]*)?)(\r?)$"
    matches = list(re.finditer(pattern, body))
    if len(matches) != 1:
        raise ValueError("Codex config needs one explicit spawn override flag; review it before synchronization")
    replacement = "true" if enabled else "false"
    body = re.sub(pattern, lambda m: m[1] + replacement + m[3] + m[4], body)
    result = (text[:start] + body + text[end:]).encode("utf-8")
    before["features"]["multi_agent_v2"][OVERRIDE_KEY] = enabled
    if tomllib.loads(result.decode("utf-8-sig")) != before:
        raise ValueError("Scoped update changed an unrelated Codex setting")
    return result


def model_entries(data):
    section = None
    seen = set()
    entries = []
    for line in data.decode("utf-8-sig").splitlines():
        if line.startswith("## "):
            section = line[3:]
            if section not in {"codex", "claude-code"}:
                raise ValueError(f"Unknown pstack harness: {section}")
        elif section and line.strip() and not line.startswith("#"):
            role, separator, values = line.partition(":")
            if not separator or (section, role) in seen:
                raise ValueError(f"Invalid or duplicate pstack role: {section}/{role}")
            seen.add((section, role))
            for entry in values.strip().split(", "):
                model, separator, effort = entry.rpartition("@")
                if not separator or effort not in {"low", "medium", "high", "xhigh", "max"}:
                    raise ValueError(f"Invalid pstack model value: {entry}")
                entries.append((section, role, model, effort))
    if {entry[0] for entry in entries} != {"codex", "claude-code"}:
        raise ValueError("Both pstack harness sections must have roles")
    return entries


def cli_command(name, *args):
    executable = portable.codex_cli() if name == 'codex' else portable.claude_cli()
    command = [executable, *args]
    if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", subprocess.list2cmdline(command)]
    return command


def validate_models(home, data, *, codex_home=None):
    catalog = json.loads(((codex_home or home / '.codex') / 'models_cache.json').read_text(encoding='utf-8-sig'))
    models = {m["slug"]: {e["effort"] for e in m.get("supported_reasoning_levels", [])}
              for m in catalog["models"] if m.get("visibility") == "list"}
    help_result = subprocess.run(cli_command("claude", "--help"), capture_output=True, text=True, encoding='utf-8', timeout=20)
    if help_result.returncode:
        raise RuntimeError("Claude CLI model discovery failed")
    aliases = set(re.findall(r"['\"](opus|sonnet|haiku|fable)['\"]", help_result.stdout))
    entries = model_entries(data)
    for section, role, model, effort in entries:
        if model in {"inherit-parent", "auto"}:
            continue
        harness = "codex" if model.startswith("codex:") else "claude-code" if model.startswith("claude:") else section
        slug = model.split(":", 1)[-1]
        cli_command("codex" if harness == "codex" else "claude")
        if harness == "codex" and (slug not in models or effort not in models[slug]):
            raise ValueError(f"Unavailable Codex model or effort for {section}/{role}: {model}@{effort}")
        if harness == "claude-code" and slug not in aliases:
            raise ValueError(f"Unconfirmed Claude alias for {section}/{role}: {slug}")
    return len(entries)


def validate_codex(enabled, *, proposed=False):
    request = {"id": 1, "method": "initialize", "params": {
        "clientInfo": {"name": "pstack-runtime-validation", "version": "1.0"}}}
    command = cli_command("codex", "app-server")
    if proposed:
        command += ['-c', f"features.multi_agent_v2.{OVERRIDE_KEY}={'true' if enabled else 'false'}"]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8')
    reader = ThreadPoolExecutor(max_workers=1)
    try:
        def send(value):
            process.stdin.write(json.dumps(value) + "\n")
            process.stdin.flush()

        def receive(ident):
            while True:
                line = reader.submit(process.stdout.readline).result(timeout=30)
                if not line:
                    raise RuntimeError("Codex exited before accepting the requested configuration")
                response = json.loads(line)
                if response.get("id") == ident:
                    if "error" in response:
                        raise RuntimeError(f"Codex configuration request {ident} was rejected")
                    return response["result"]

        send(request)
        receive(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "config/read", "params": {"includeLayers": False}})
        config = receive(2)["config"]
        if config.get("features", {}).get("multi_agent_v2", {}).get(OVERRIDE_KEY) != enabled:
            raise RuntimeError("Codex did not resolve the requested spawn override setting")
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        reader.shutdown(wait=True, cancel_futures=True)


def replace_checked(path, before, after):
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.pstack-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(after)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        if path.read_bytes() != before:
            raise RuntimeError(f"Concurrent edit detected; left file unchanged: {path}")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
