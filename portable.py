"""Cross-platform primitives shared by the engine on macOS, Linux and Windows.

They cover where Codex and Claude keep their files, how to call their CLIs, byte-exact UTF-8 text,
file replacement that tolerates Windows file locks, an exclusive run lock, and
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
import signal
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


def while_held(move, attempts=20, delay=0.25, before_retry=None):
    """Run move, and run it again while Windows refuses it. Windows refuses to replace a file or move a folder that
    another process holds open: Codex or Claude may be reading it, or a scanner may have opened a folder that has
    just appeared. The last refusal is raised. before_retry runs before each further attempt, for a caller whose
    check before the move has to hold at the moment of the move, not seconds earlier. Whatever it raises ends the
    attempts, a refused read included, because a check that could not be made has not passed."""
    for attempt in range(attempts):
        try:
            return move()
        except PermissionError:
            if not WINDOWS or attempt == attempts - 1:
                raise
            time.sleep(delay)
            if before_retry:
                before_retry()


def replace(source, target, attempts=20, delay=0.25):
    """os.replace, again while Windows refuses it. Windows refuses a move onto a folder that already exists the
    same way as a move of a held one, so that move fails only after the last attempt."""
    return while_held(lambda: os.replace(source, target), attempts, delay)


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


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@functools.cache
def _kernel32():
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
                                                   ctypes.POINTER(wintypes.DWORD))
    return kernel32


def _windows_start(pid):
    import ctypes
    from ctypes import wintypes
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: no process has this PID
            raise ProcessLookupError(pid)
        raise PermissionError(pid)
    try:
        code, times = wintypes.DWORD(), [wintypes.FILETIME() for _ in range(4)]
        if not (kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                and kernel32.GetProcessTimes(handle, *map(ctypes.byref, times))):
            raise PermissionError(pid)
        if code.value != 259:  # STILL_ACTIVE: an open handle keeps an ended process's record
            raise ProcessLookupError(pid)
        return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
    finally:
        kernel32.CloseHandle(handle)


def process_start():
    """This process's start time, or None: on Linux in clock ticks, as field 22 of /proc/self/stat gives it, and on
    Windows in GetProcessTimes' 100-nanosecond steps."""
    if WINDOWS:
        return str(_windows_start(os.getpid()))
    try:
        return Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def process_running(pid, start=None):
    """Whether the process with this PID still runs, and on Linux and Windows is the one that started at start, as
    process_start on this machine gives it. A process that later reuses the PID has another start time. Where this
    cannot tell, as on macOS or for a process Windows will not open, it counts as running."""
    if WINDOWS:
        try:
            started = str(_windows_start(pid))  # not os.kill, which would end the process there
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return start is None or not start.isdigit() or started == start
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


def run_tree(command, *, timeout, stopped_by=subprocess.TimeoutExpired, **options):
    """subprocess.run without check, input or capture_output, except that the time limit stops every process the
    command started, and not only the command, as does an error of the kind stopped_by names, such as
    KeyboardInterrupt, raised while it runs. After that stop it raises the error. On POSIX the command leads a session
    of its own, so a Ctrl+C in its terminal reaches only the caller, and a command whose caller does not stop it on that
    interrupt runs on to its end. The stop there reaches only the command's process group, and a process that moved to
    a group or session of its own outlives it."""
    # taskkill /T follows Windows parents, and Git's MSYS shell starts a program by fork and then exec, so that
    # program's parent has exited and only a job still holds it.
    job = _kernel32().CreateJobObjectW(None, None) if WINDOWS else None
    try:
        process = subprocess.Popen(command, start_new_session=not WINDOWS, **options)
        # The command runs for a moment before it joins the job, and only taskkill /T reaches what it starts then.
        if job and not _kernel32().AssignProcessToJobObject(job, process._handle):
            _kernel32().CloseHandle(job)
            job = None
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except (subprocess.TimeoutExpired, stopped_by):
            _stop_tree(process, job)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.communicate(timeout=5)
            raise
    finally:
        if job:
            _kernel32().CloseHandle(job)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _stop_tree(process, job):
    if not WINDOWS:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        return
    try:
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=30)
    finally:
        if job:
            _kernel32().TerminateJobObject(job, 1)
            # A process can take a moment to end after TerminateJobObject, and until then it holds its working
            # folder, which its caller may be about to remove.
            ending = time.monotonic() + 5
            while _job_processes(job) and time.monotonic() < ending:
                time.sleep(0.05)


def _job_processes(job):
    import ctypes
    from ctypes import wintypes

    class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [('times', ctypes.c_int64 * 4), ('page_faults', wintypes.DWORD), ('total', wintypes.DWORD),
                    ('active', wintypes.DWORD), ('terminated', wintypes.DWORD)]

    JobObjectBasicAccountingInformation = 1
    accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    if not _kernel32().QueryInformationJobObject(job, JobObjectBasicAccountingInformation, ctypes.byref(accounting),
                                                 ctypes.sizeof(accounting), None):
        return 0
    return accounting.active


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
