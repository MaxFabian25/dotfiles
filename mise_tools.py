"""Install the tool versions the home mise config pins."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess


def state_folder(home: Path) -> Path:
    """The folder that holds the pull's lock, receipt and the record of the last installed mise config."""
    return home / '.local/state/dotfiles'


def install(home: Path, state: Path) -> None:
    """A release writes tool versions and the npm PATH entry into the mise config, and mise stops on a pinned
    version that isn't installed. Codex runs through launchers that ask mise for the pinned Node, so each pin has to
    be installed before the next Codex command after the config changes. Install once per config contents, and
    again after a failed install."""
    config = home / '.config/mise/config.toml'
    if not config.is_file():
        return
    record = state / 'mise-installed.sha256'
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    if record.is_file() and record.read_text(encoding='utf-8').strip() == digest:
        return
    mise = shutil.which('mise')
    if mise is None:
        raise RuntimeError(f'{config} pins tools, but mise is not on PATH to install them')
    # mise also reads version files in the folder it runs in, so it runs in the home folder, not the pull's checkout.
    result = subprocess.run([mise, '--cd', str(home), 'install', '--yes'], timeout=900)
    if result.returncode:
        raise RuntimeError(f'mise install exited {result.returncode}; the next pull retries it')
    state.mkdir(parents=True, exist_ok=True)
    record.write_text(digest + '\n', encoding='utf-8')
