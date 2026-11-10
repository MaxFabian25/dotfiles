"""Cross-platform primitives shared by the engine on macOS, Linux and Windows.

They cover where Codex and Claude keep their files, how to call their CLIs, byte-exact UTF-8 text,
file replacement that tolerates Windows file locks, an exclusive run lock, directory pointers, and
archive extraction without symlinks. Codex honours CODEX_HOME and Claude honours CLAUDE_CONFIG_DIR,
as the apps do; an explicit home other than the real one (tests, sandboxes) ignores both.
"""
import contextlib
from datetime import datetime
import functools
import io
import json
import os
from pathlib import Path, PurePath, PurePosixPath
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

WINDOWS = os.name == 'nt'
# Git as the engine runs it: no line-ending conversion, so archives and blobs keep upstream's bytes.
GIT = ('git', '-c', 'core.autocrlf=false', '-c', 'core.eol=lf')
# The Codex CLI shipped inside the desktop app; preferring it keeps plugin state in step with the app.
CODEX_APP_CLI = {'darwin': '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'}


def native_cli_environment(environment=None):
    environment = dict(os.environ if environment is None else environment)
    # Windows processes launched from WSL can lack PATHEXT, breaking mise's executable lookup.
    if WINDOWS and not environment.get('PATHEXT'):
        environment['PATHEXT'] = '.COM;.EXE;.BAT;.CMD'
    count = int(environment.get('GIT_CONFIG_COUNT') or '0')
    if count < 0:
        raise ValueError('GIT_CONFIG_COUNT cannot be negative')
    for key, value in (('core.autocrlf', 'false'), ('core.eol', 'lf')):
        environment[f'GIT_CONFIG_KEY_{count}'] = key
        environment[f'GIT_CONFIG_VALUE_{count}'] = value
        count += 1
    environment['GIT_CONFIG_COUNT'] = str(count)
    return environment


def _home(home):
    return Path.home() if home is None else Path(home)


def _configured(variable, home):
    value = os.environ.get(variable)
    if value and (home is None or Path(home) == Path.home()):
        return Path(value).expanduser()
    return None


def codex_home(home=None):
    return _configured('CODEX_HOME', home) or _home(home) / '.codex'


def claude_home(home=None):
    return _configured('CLAUDE_CONFIG_DIR', home) or _home(home) / '.claude'


def agents_home(home=None):
    return _home(home) / '.agents'


def _executable(variable, name, candidates):
    override = os.environ.get(variable)
    if override:
        if not Path(override).is_file():
            raise FileNotFoundError(f'{variable} does not name a file: {override}')
        return override
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f'{name} CLI not found; install it or set {variable}')


def codex_cli():
    if os.environ.get('CODEX_CLI'):
        return _executable('CODEX_CLI', 'codex', [])
    app = CODEX_APP_CLI.get(sys.platform)
    if WINDOWS:
        command = "$p = Get-AppxPackage -Name OpenAI.Codex | Sort-Object Version -Descending | Select-Object -First 1; if ($p) { Join-Path $p.InstallLocation 'app/resources/codex.exe' }"
        try:
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                    capture_output=True, text=True, encoding='utf-8', timeout=15, check=True)
            app = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            app = None
    return _executable('CODEX_CLI', 'codex', [app])


def claude_cli():
    local = Path.home() / '.local/bin' / ('claude.exe' if WINDOWS else 'claude')
    return _executable('CLAUDE_CLI', 'claude', [shutil.which('claude'), local])


def read_utf8(path):
    # Bytes in, bytes out: no locale encoding and no newline translation on any platform.
    return Path(path).read_bytes().decode('utf-8')


def write_utf8(path, text):
    Path(path).write_bytes(text.encode('utf-8'))


def posix(path):
    """A relative path as recorded in receipts and manifests: forward slashes on every platform."""
    return PurePath(path).as_posix()


def replace(source, target, attempts=20, delay=0.25):
    # Windows refuses to replace a file another process holds open; Codex or Claude may be reading it.
    for attempt in range(attempts):
        try:
            return os.replace(source, target)
        except PermissionError:
            if not WINDOWS or attempt == attempts - 1:
                raise
            time.sleep(delay)


def staging_folder(prefix, dir):
    """Make a new folder under dir for building a tree that is then renamed into place, and return its path.

    On Windows the folder takes dir's own access rules. Python's tempfile gives its folders an owner-only rule
    there, a tree renamed out of one keeps that rule, and a tree an elevated session built is owned by the
    Administrators group, so a later non-elevated run could not read the release."""
    if not WINDOWS:
        return tempfile.mkdtemp(prefix=prefix, dir=dir)
    for _ in range(100):
        folder = Path(dir) / (prefix + secrets.token_hex(4))
        try:
            folder.mkdir()
        except FileExistsError:
            continue
        return str(folder)
    raise FileExistsError(f'No free staging folder name under {dir}')


def _remove_read_only(function, path, error):
    os.chmod(path, stat.S_IWRITE)
    function(path)


@contextlib.contextmanager
def staging_directory(prefix, dir):
    """staging_folder as a context manager that removes the folder on exit, as tempfile.TemporaryDirectory does."""
    if not WINDOWS:
        with tempfile.TemporaryDirectory(prefix=prefix, dir=dir) as folder:
            yield folder
        return
    folder = staging_folder(prefix, dir)
    try:
        yield folder
    finally:
        # Git marks a checkout's objects read-only, and Windows refuses to delete a read-only file.
        shutil.rmtree(folder, onexc=_remove_read_only)


def require_readable(root):
    """Raise PermissionError when a folder under root may not be listed. pathlib's rglob skips such a folder
    silently, so a tree read through it looks empty or smaller than it is. A missing root stays what rglob
    makes of it, an empty tree."""
    def refuse(error):
        if isinstance(error, PermissionError):
            raise PermissionError(
                f'Cannot read {error.filename}: {error.strerror}. On Windows a non-elevated run cannot read a '
                'folder that an elevated session built before staging folders took their parent\'s access rules; '
                'run the same command elevated, or reset the folder with icacls /reset /T.') from error
    for _ in os.walk(root, onerror=refuse):
        pass


@functools.cache
def _proc_shows_host():
    """Whether /proc lists this machine's processes. A sandbox with its own PID namespace mounts a /proc whose
    PID 1 is its own launcher, and that /proc hides every process outside it."""
    try:
        init = Path('/proc/1/cmdline').read_bytes().split(b'\0', 1)[0].decode('utf-8', 'replace')
    except OSError:
        return False
    return PurePosixPath(init).name in {'init', 'systemd'}


def process_start():
    """This process's start time in clock ticks, as field 22 of Linux /proc/self/stat gives it, or None."""
    try:
        return Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def process_running(pid, start=None):
    """Whether the process with this PID still runs, and on Linux is the one that started at start, a clock-tick
    count from process_start. A process that later reuses the PID has another start time. Where this cannot
    tell, as on Windows, it counts as running."""
    if WINDOWS:
        return True  # os.kill would end the process there
    if sys.platform.startswith('linux') and not _proc_shows_host():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    except (OSError, IndexError):
        return True  # no /proc, as on macOS, so a reused process ID cannot be told apart
    return start is None or not start.isdigit() or len(fields) < 20 or fields[19] == start


def _holder_note(path):
    return path.with_name(path.name + '.holder')


def lock_holder(path):
    """The process a recording exclusive_lock names for path, as its pid, procStart, command and since fields,
    while that process still runs, or None."""
    try:
        holder = json.loads(_holder_note(Path(path)).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(holder, dict) or type(holder.get('pid')) is not int:
        return None
    return holder if process_running(holder['pid'], holder.get('procStart')) else None


@contextlib.contextmanager
def exclusive_lock(path, *, record=False):
    """Hold a non-blocking exclusive lock; raise BlockingIOError while another run holds it. With record, name
    this process in a file beside the lock while it holds it, which lock_holder reads."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if WINDOWS:
            import msvcrt
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise BlockingIOError(f'Another run holds {path}') from error
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        note = _holder_note(path)
        try:
            if record:
                note.write_text(json.dumps({'pid': os.getpid(), 'procStart': process_start(),
                                            'command': ' '.join(sys.argv)[:300],
                                            'since': datetime.now().astimezone().isoformat(timespec='seconds')}),
                                encoding='utf-8')
            yield
        finally:
            if record:
                note.unlink(missing_ok=True)
            if WINDOWS:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def _is_junction(path):
    return bool(getattr(Path(path), 'is_junction', lambda: False)())


def read_pointer(link):
    """The directory name a pointer names, or None when link is not a pointer."""
    link = Path(link)
    if not (link.is_symlink() or _is_junction(link)):
        return None
    return Path(os.readlink(link)).name


def remove_pointer(link):
    link = Path(link)
    if read_pointer(link) is None:
        raise ValueError(f'Not a directory pointer: {link}')
    (os.rmdir if WINDOWS else os.unlink)(link)


def set_pointer(link, target_name):
    """Point link at its sibling directory target_name: a relative symlink, or a junction on Windows."""
    link = Path(link)
    temporary = link.with_name(f'.{link.name}-{os.getpid()}')
    try:
        temporary.symlink_to(target_name, target_is_directory=True)
    except OSError:
        if not WINDOWS:
            raise
        import _winapi
        _winapi.CreateJunction(str(link.parent / target_name), str(temporary))
    try:
        os.replace(temporary, link)
    except OSError:
        # Windows cannot rename over an existing directory link; swap it in two steps.
        if not WINDOWS:
            raise
        remove_pointer(link)
        os.replace(temporary, link)


def extract_tar(archive, destination):
    """Extract a tar archive's bytes with symlinks materialised as copies of their in-archive targets.

    Codex's package copier skips symlinks, and Windows cannot create them without privileges.
    A link that leaves the archive or names nothing raises ValueError.
    """
    destination = Path(destination)
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        members = bundle.getmembers()
        links = {member.name: member.linkname for member in members if member.issym()}
        bundle.extractall(destination, members=[m for m in members if not m.issym()], filter='data')

    def resolve(name, depth=0):
        if depth > 40:
            raise ValueError(f'Symlink loop in archive: {name}')
        target = PurePosixPath(name).parent / links[name]
        parts = []
        for part in target.parts:
            if part == '..':
                if not parts:
                    raise ValueError(f'Symlink leaves the archive: {name}')
                parts.pop()
            elif part not in ('', '.'):
                parts.append(part)
        resolved = '/'.join(parts)
        return resolve(resolved, depth + 1) if resolved in links else resolved

    done = set()

    def materialise(name):
        if name in done:
            return
        resolved = resolve(name)
        # Links inside a linked directory are copied with it, so materialise them first.
        for other in sorted(links):
            if other != name and other.startswith(resolved + '/'):
                materialise(other)
        source, target = destination / resolved, destination / name
        if source.is_file():
            shutil.copyfile(source, target)
        elif source.is_dir():
            shutil.copytree(source, target)
        else:
            raise ValueError(f'Unsupported symlink in archive: {name} -> {links[name]}')
        done.add(name)

    for name in sorted(links):
        materialise(name)
