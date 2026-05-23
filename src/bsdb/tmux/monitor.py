"""Async push-based monitoring of tmux sessions via hooks.

Each public function is an async context manager that yields an
``AsyncIterator[SessionInfo]``.  On entry it installs tmux hooks and whatever
transport (FIFO, unix socket, or reverse-forwarded TCP port) is needed to
receive events; on exit it tears everything down cleanly.

Usage::

    async with monitor.pipe(session, remote=remote, dwell=10) as events:
        async for info in events:
            print(info.session_name, info.activity_at)
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shlex
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from bsdb.tmux import SessionInfo, _resolve_session_spec

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared format string (mirrors the one in list())
# ---------------------------------------------------------------------------

_FMT = "\t".join([
    "#{session_name}",
    "#{session_id}",
    "#{window_id}",
    "#{pane_id}",
    "#{pane_pid}",
    "#{session_created}",
    "#{pane_current_command}",
    "#{pane_current_path}",
    "#{session_attached}",
    "#{session_activity}",
    "#{session_last_attached}",
    "#{session_many_attached}",
])

_HOOKS = ("alert-bell", "alert-silence", "alert-activity")

# Runtime directory for FIFOs, sockets, and hook scripts.
# $HOME is universally available in fish, bash, sh, and zsh; the
# ${VAR:-default} bash/sh syntax is invalid in fish, so we avoid it here.
_RUN_DIR = "$HOME/.cache/bsdb"


def _parse_line(line: str, remote: Optional[str]) -> Optional[SessionInfo]:
    """Parse one tab-separated event line into a SessionInfo, or None on error."""
    parts = line.rstrip("\n").split("\t", 11)
    if len(parts) < 12:
        log.debug("parse: only %d field(s) (need 12) in: %r", len(parts), line[:120])
        return None
    (session_name, session_id, window_id, pane_id, pane_pid_str,
     created_ts, cmd, cwd, attached_str, activity_ts,
     last_attached_ts, many_attached_str) = parts
    try:
        info = SessionInfo(
            session_name=session_name,
            session_id=session_id,
            window_id=window_id,
            pane_id=pane_id,
            pane_pid=int(pane_pid_str),
            remote=remote,
            created_at=datetime.fromtimestamp(int(created_ts), tz=timezone.utc),
            cmd=cmd,
            cwd=cwd.strip() or None,
            attached=int(attached_str),
            activity_at=datetime.fromtimestamp(int(activity_ts), tz=timezone.utc),
            last_attached_at=(
                datetime.fromtimestamp(int(last_attached_ts), tz=timezone.utc)
                if last_attached_ts.strip() else None
            ),
            many_attached=bool(int(many_attached_str)),
        )
        log.debug("parse: ok session=%s pid=%s", info.session_name, info.pane_pid)
        return info
    except (ValueError, IndexError) as exc:
        log.debug("parse: conversion error (%s) in: %r", exc, line[:120])
        return None


async def _drain_stream(
    stream: asyncio.StreamReader,
    queue: asyncio.Queue,
    remote: Optional[str],
    label: str = "stream",
) -> None:
    """Read lines from *stream*, parse each as SessionInfo, push to *queue*."""
    log.debug("%s: drain task started", label)
    while True:
        try:
            raw = await stream.readline()
        except asyncio.CancelledError:
            log.debug("%s: drain task cancelled", label)
            break
        except Exception as exc:
            log.debug("%s: read error: %s", label, exc)
            break
        if not raw:
            log.debug("%s: EOF", label)
            break
        line = raw.decode(errors="replace")
        log.debug("%s: raw line: %r", label, line.rstrip())
        info = _parse_line(line, remote)
        if info is not None:
            await queue.put(info)
    await queue.put(None)  # sentinel
    log.debug("%s: drain task done", label)


async def _log_stream(stream: asyncio.StreamReader, label: str) -> None:
    """Drain *stream*, logging each line at DEBUG."""
    while True:
        try:
            raw = await stream.readline()
        except (asyncio.CancelledError, Exception):
            break
        if not raw:
            break
        log.debug("%s: %s", label, raw.decode(errors="replace").rstrip())


async def _queue_iter(queue: asyncio.Queue) -> AsyncIterator[SessionInfo]:
    while True:
        item = await queue.get()
        if item is None:
            break
        yield item


# ---------------------------------------------------------------------------
# Hook script helpers
#
# The hook command bodies are written as a small #!/bin/sh script and
# installed on the remote via base64 to sidestep all nested-quoting issues.
# The set-hook commands reference the script by its expanded path.
# ---------------------------------------------------------------------------

def _hook_script_content(target: str, dest_cmd: str) -> str:
    """Return #!/bin/sh script content that captures session state and sends it
    to *dest_cmd* on each hook firing."""
    t = shlex.quote(target)
    f = shlex.quote(_FMT)
    return f"#!/bin/sh\ntmux list-panes -t {t} -F {f} | head -1 | {dest_cmd}\n"


def _install_hooks_script(target: str, dest_cmd: str, hook_file_var: str) -> str:
    """Return shell commands that write the hook script and register all hooks.

    *hook_file_var* is a shell variable reference (e.g. ``$_BSDB_HOOK``) that
    holds the path to the script on the remote; it is expanded by the outer
    shell before being passed to ``tmux set-hook``, so tmux stores a literal
    path with no further variable references.
    """
    content = _hook_script_content(target, dest_cmd)
    b64 = base64.b64encode(content.encode()).decode()
    t = shlex.quote(target)
    lines = [
        f'printf "%s" {shlex.quote(b64)} | base64 -d > {hook_file_var}',
        f"chmod +x {hook_file_var}",
    ]
    for hook in _HOOKS:
        # The outer shell expands hook_file_var before tmux sees the command, so
        # tmux stores a literal path — no variable references reach tmux.
        lines.append(f'tmux set-hook -t {t} {hook} "run-shell {hook_file_var}"')
    return "\n".join(lines)


def _remove_hooks_cmd(target: str) -> str:
    t = shlex.quote(target)
    return "\n".join(f"tmux set-hook -u -t {t} {hook}" for hook in _HOOKS)


def _verify_hooks_cmd(target: str, hook_file_expr: str) -> str:
    """Shell commands that emit hook installation state to stderr for debug logging."""
    t = shlex.quote(target)
    return "\n".join([
        f"echo bsdb-diag:hook-file >&2",
        f"ls -la {hook_file_expr} >&2 || echo bsdb-diag:hook-file-MISSING >&2",
        f"echo bsdb-diag:hook-content >&2",
        f"cat {hook_file_expr} >&2 || true",
        f"echo bsdb-diag:tmux-hooks >&2",
        f"tmux show-hooks -t {t} >&2 || true",
        f"echo bsdb-diag:tmux-opts >&2",
        f"tmux show-options -t {t} activity-action silence-action >&2 || true",
        f"tmux show-window-options -t {t} monitor-activity monitor-silence >&2 || true",
    ])


def _tmux_options_cmd(target: str, dwell: int) -> str:
    t = shlex.quote(target)
    return "\n".join([
        # monitor-activity and monitor-silence are window options (-w)
        f"tmux set-option -w -t {t} monitor-activity on",
        f"tmux set-option -w -t {t} monitor-silence {dwell}",
        # activity-action and silence-action are session options.
        # "any" fires the hook regardless of which window is currently active.
        f"tmux set-option -t {t} activity-action any",
        f"tmux set-option -t {t} silence-action any",
        # Replace the terminal bell (triggered by *-action any) with a
        # status-bar message.  Hooks fire either way; this just stops the
        # audio bell from ringing every time an alert fires.
        f"tmux set-option -t {t} visual-activity on",
        f"tmux set-option -t {t} visual-silence on",
    ])


def _shell_argv(remote: Optional[str]) -> list[str]:
    if remote:
        return ["ssh", remote, "$SHELL -l"]
    return [os.environ.get("SHELL", "/bin/sh"), "-l"]


async def _fetch_info(
    remote: Optional[str],
    command: str,
    label: str = "fetch",
) -> Optional[SessionInfo]:
    """Run *command* via $SHELL -l, parse the first stdout line as a SessionInfo."""
    argv = _shell_argv(remote)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await proc.communicate(input=(command + "\n").encode())
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    if stderr.strip():
        log.debug("%s: stderr: %s", label, stderr.decode(errors="replace").rstrip())
    lines = stdout.decode(errors="replace").splitlines()
    return _parse_line(lines[0], remote) if lines else None


async def _ssh_run(remote: Optional[str], script: str, label: str = "ssh_run") -> None:
    """Run *script* on *remote* (or locally) via $SHELL -l on stdin."""
    log.debug("%s: running script:\n%s", label, script)
    argv = _shell_argv(remote)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    proc.stdin.write((script + "\n").encode())
    await proc.stdin.drain()
    proc.stdin.close()
    stdout_task = asyncio.create_task(_log_stream(proc.stdout, f"{label} stdout"))
    stderr_task = asyncio.create_task(_log_stream(proc.stderr, f"{label} stderr"))
    await proc.wait()
    stdout_task.cancel()
    stderr_task.cancel()
    await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    log.debug("%s: exit code %s", label, proc.returncode)


# ---------------------------------------------------------------------------
# Method: pipe
# ---------------------------------------------------------------------------

@asynccontextmanager
async def pipe(
    session: "SessionInfo | str",
    remote: Optional[str] = None,
    dwell: int = 10,
) -> AsyncIterator[AsyncIterator[SessionInfo]]:
    """Monitor via a named pipe (FIFO) on the remote host.

    Hooks write one list-panes line per event to a FIFO in the user's run
    directory.  A persistent ``cat`` on the remote streams the lines back
    through SSH to bsdb.
    """
    target, remote = _resolve_session_spec(session, remote)
    log.debug("pipe: target=%r remote=%r dwell=%s", target, remote, dwell)
    queue: asyncio.Queue = asyncio.Queue()

    safe = target.lstrip("$").replace("%", "p")
    fifo_expr = f"{_RUN_DIR}/bsdb-pipe-{safe}.fifo"
    hook_expr = f"{_RUN_DIR}/bsdb-pipe-{safe}-hook.sh"
    dest_cmd = f"tee -a {fifo_expr} > /dev/null"

    setup_script = "\n".join([
        f"mkdir -p {_RUN_DIR}",
        f"rm -f {fifo_expr}",
        f"mkfifo {fifo_expr}",
        _tmux_options_cmd(target, dwell),
        _install_hooks_script(target, dest_cmd, hook_expr),
        _verify_hooks_cmd(target, hook_expr),
        # sh -c "cat <> ..." keeps the write end open so cat never sees EOF when
        # a hook script closes after writing.  Wrapped in sh -c because the <>
        # read-write redirect is POSIX/bash but not valid in fish.  The parent
        # shell (fish or bash) expands $HOME before passing the path to sh.
        f'sh -c "cat <> {fifo_expr}"',
    ])
    log.debug("pipe: setup script:\n%s", setup_script)

    argv = _shell_argv(remote)
    log.debug("pipe: launching stream process: %s", argv)
    stream_proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    log.debug("pipe: stream process pid=%s", stream_proc.pid)
    stream_proc.stdin.write((setup_script + "\n").encode())
    await stream_proc.stdin.drain()
    stream_proc.stdin.close()

    drain_task = asyncio.create_task(
        _drain_stream(stream_proc.stdout, queue, remote, label="pipe")
    )
    stderr_task = asyncio.create_task(_log_stream(stream_proc.stderr, "pipe stderr"))

    try:
        yield _queue_iter(queue)
    finally:
        log.debug("pipe: shutting down")
        drain_task.cancel()
        stderr_task.cancel()
        stream_proc.terminate()
        await asyncio.gather(stream_proc.wait(), drain_task, stderr_task, return_exceptions=True)
        cleanup = "\n".join([
            _remove_hooks_cmd(target),
            f"rm -f {fifo_expr} {hook_expr}",
        ])
        await _ssh_run(remote, cleanup, label="pipe-cleanup")
        log.debug("pipe: done")


# ---------------------------------------------------------------------------
# Method: socat
# ---------------------------------------------------------------------------

@asynccontextmanager
async def socat(
    session: "SessionInfo | str",
    remote: Optional[str] = None,
    dwell: int = 10,
) -> AsyncIterator[AsyncIterator[SessionInfo]]:
    """Monitor via a Unix domain socket managed by socat on the remote host.

    Hooks pipe list-panes output through ``socat`` to a UNIX socket.  A socat
    listener on the remote writes arriving data to stdout, which SSH streams
    back to bsdb.
    """
    target, remote = _resolve_session_spec(session, remote)
    log.debug("socat: target=%r remote=%r dwell=%s", target, remote, dwell)
    queue: asyncio.Queue = asyncio.Queue()

    safe = target.lstrip("$").replace("%", "p")
    sock_expr = f"{_RUN_DIR}/bsdb-socat-{safe}.sock"
    hook_expr = f"{_RUN_DIR}/bsdb-socat-{safe}-hook.sh"
    dest_cmd = f"socat - UNIX-CONNECT:{sock_expr}"

    setup_script = "\n".join([
        f"mkdir -p {_RUN_DIR}",
        f"rm -f {sock_expr}",
        _tmux_options_cmd(target, dwell),
        _install_hooks_script(target, dest_cmd, hook_expr),
        _verify_hooks_cmd(target, hook_expr),
        f"socat UNIX-LISTEN:{sock_expr},fork,reuseaddr STDOUT",
    ])
    log.debug("socat: setup script:\n%s", setup_script)

    argv = _shell_argv(remote)
    log.debug("socat: launching stream process: %s", argv)
    stream_proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    log.debug("socat: stream process pid=%s", stream_proc.pid)
    stream_proc.stdin.write((setup_script + "\n").encode())
    await stream_proc.stdin.drain()
    stream_proc.stdin.close()

    # Give socat a moment to create the socket before hooks can fire
    log.debug("socat: waiting 0.5s for socket to be created")
    await asyncio.sleep(0.5)

    drain_task = asyncio.create_task(
        _drain_stream(stream_proc.stdout, queue, remote, label="socat")
    )
    stderr_task = asyncio.create_task(_log_stream(stream_proc.stderr, "socat stderr"))

    try:
        yield _queue_iter(queue)
    finally:
        log.debug("socat: shutting down")
        drain_task.cancel()
        stderr_task.cancel()
        stream_proc.terminate()
        await asyncio.gather(stream_proc.wait(), drain_task, stderr_task, return_exceptions=True)
        cleanup = "\n".join([
            _remove_hooks_cmd(target),
            f"rm -f {sock_expr} {hook_expr}",
        ])
        await _ssh_run(remote, cleanup, label="socat-cleanup")
        log.debug("socat: done")


# ---------------------------------------------------------------------------
# Method: listen
# ---------------------------------------------------------------------------

@asynccontextmanager
async def listen(
    session: "SessionInfo | str",
    remote: Optional[str] = None,
    dwell: int = 10,
) -> AsyncIterator[AsyncIterator[SessionInfo]]:
    """Monitor via SSH reverse port forwarding.

    bsdb opens a local TCP server and instructs SSH to forward a remote port
    back to it (``ssh -R``).  Hooks on the remote use ``socat`` or ``nc`` to
    send list-panes output to that forwarded port.
    """
    target, remote = _resolve_session_spec(session, remote)
    log.debug("listen: target=%r remote=%r dwell=%s", target, remote, dwell)
    queue: asyncio.Queue = asyncio.Queue()

    # Pick a free local port by binding to :0 and reading the assigned port
    tmp_srv = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    local_port = tmp_srv.sockets[0].getsockname()[1]
    tmp_srv.close()
    await tmp_srv.wait_closed()
    log.debug("listen: local port=%s", local_port)

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("listen: TCP connection from %s", peer)
        while True:
            raw = await reader.readline()
            if not raw:
                log.debug("listen: connection closed by %s", peer)
                break
            line = raw.decode(errors="replace")
            log.debug("listen: raw line from %s: %r", peer, line.rstrip())
            info = _parse_line(line, remote)
            if info is not None:
                await queue.put(info)
            else:
                log.info("listen: parse failed for data from %s (len=%d)", peer, len(raw))
        writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", local_port)
    log.info("listen: local TCP server on port %s (SSH -R %s:127.0.0.1:%s → local)",
             local_port, local_port, local_port)

    safe = target.lstrip("$").replace("%", "p")
    hook_expr = f"{_RUN_DIR}/bsdb-listen-{safe}-hook.sh"
    dest_cmd = (
        f"socat - TCP:127.0.0.1:{local_port} 2>/dev/null"
        f" || nc -q1 127.0.0.1 {local_port} 2>/dev/null"
    )

    hook_script = "\n".join([
        f"mkdir -p {_RUN_DIR}",
        _tmux_options_cmd(target, dwell),
        _install_hooks_script(target, dest_cmd, hook_expr),
        _verify_hooks_cmd(target, hook_expr),
        # Test that the reverse port forward is actually usable before blocking.
        # socat /dev/null sends nothing and exits cleanly; a non-zero exit means
        # the forward isn't reachable (AllowTcpForwarding may be off in sshd_config).
        f"socat /dev/null TCP:127.0.0.1:{local_port},connect-timeout=3 >&2"
        f" && echo bsdb:port-fwd-ok >&2"
        f" || echo bsdb:port-fwd-FAILED-check-AllowTcpForwarding-in-sshd_config >&2",
        # Keep the shell alive so the SSH connection (and thus the -R port
        # forward) persists.  tail -f /dev/null blocks indefinitely without
        # reading from stdin, unlike `read` which depends on stdin staying open.
        "tail -f /dev/null",
    ])
    log.debug("listen: hook script:\n%s", hook_script)

    if remote:
        argv = [
            "ssh",
            "-R", f"{local_port}:127.0.0.1:{local_port}",
            "-o", "ExitOnForwardFailure=yes",
            remote,
            "$SHELL -l",
        ]
    else:
        argv = [os.environ.get("SHELL", "/bin/sh"), "-l"]

    log.debug("listen: launching SSH process: %s", argv)
    ssh_proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    log.debug("listen: SSH process pid=%s", ssh_proc.pid)
    ssh_proc.stdin.write((hook_script + "\n").encode())
    await ssh_proc.stdin.drain()
    ssh_proc.stdin.close()  # EOF lets the remote shell execute the script and reach tail

    stdout_task = asyncio.create_task(_log_stream(ssh_proc.stdout, "listen stdout"))
    stderr_task = asyncio.create_task(_log_stream(ssh_proc.stderr, "listen stderr"))

    # If SSH exits while we are still monitoring, the reverse port forward is gone
    # and no further events can arrive.  Surface this as a WARNING so it appears
    # at the default log level without needing -L debug.
    _stopping = asyncio.Event()

    async def _watch_ssh() -> None:
        await ssh_proc.wait()
        if not _stopping.is_set():
            log.warning(
                "listen: SSH process exited unexpectedly (code=%s); "
                "reverse port forward is gone — check that AllowTcpForwarding "
                "is enabled in the remote sshd_config",
                ssh_proc.returncode,
            )
            await queue.put(None)

    watch_task = asyncio.create_task(_watch_ssh())

    try:
        yield _queue_iter(queue)
    finally:
        log.debug("listen: shutting down")
        _stopping.set()
        watch_task.cancel()
        server.close()
        await server.wait_closed()
        stdout_task.cancel()
        stderr_task.cancel()
        ssh_proc.terminate()
        await asyncio.gather(
            ssh_proc.wait(), watch_task, stdout_task, stderr_task,
            return_exceptions=True,
        )
        cleanup = "\n".join([
            _remove_hooks_cmd(target),
            f"rm -f {hook_expr}",
        ])
        await _ssh_run(remote, cleanup, label="listen-cleanup")
        await queue.put(None)  # unblock any pending consumer
        log.debug("listen: done")


# ---------------------------------------------------------------------------
# Method: poll
# ---------------------------------------------------------------------------

@asynccontextmanager
async def poll(
    session: "SessionInfo | str",
    remote: Optional[str] = None,
    dwell: int = 10,
    interval: int = 5,
) -> AsyncIterator[AsyncIterator[SessionInfo]]:
    """Monitor by polling ``tmux list-panes`` every *interval* seconds.

    Unlike the hook-based methods, this works for sessions with no attached
    client.  An event is emitted on the first poll, whenever ``session_activity``
    changes between polls (activity detected), and every *dwell* seconds when
    no activity is seen (silence notification).

    *interval* is clamped to *dwell* so silence events are never overdue.
    Each poll opens a short-lived SSH connection; the default interval of 5 s
    is a reasonable balance between latency and connection overhead.
    """
    target, remote = _resolve_session_spec(session, remote)
    effective_interval = min(interval, dwell)
    log.debug("poll: target=%r remote=%r dwell=%s interval=%s",
              target, remote, dwell, effective_interval)
    queue: asyncio.Queue = asyncio.Queue()

    t = shlex.quote(target)
    f = shlex.quote(_FMT)
    poll_cmd = f"tmux list-panes -t {t} -F {f}"

    async def _poll_loop() -> None:
        last_activity_at: Optional[datetime] = None
        last_event_at: Optional[datetime] = None

        while True:
            try:
                info = await _fetch_info(remote, poll_cmd, label="poll")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("poll: fetch error: %s", exc)
                info = None

            if info is not None:
                now = datetime.now(tz=timezone.utc)
                first = last_event_at is None
                activity_changed = (
                    last_activity_at is not None
                    and info.activity_at != last_activity_at
                )
                silence_due = (
                    last_event_at is not None
                    and (now - last_event_at).total_seconds() >= dwell
                )
                if first or activity_changed or silence_due:
                    log.debug("poll: emit first=%s activity=%s silence=%s",
                              first, activity_changed, silence_due)
                    await queue.put(info)
                    last_event_at = now
                last_activity_at = info.activity_at

            await asyncio.sleep(effective_interval)

    poll_task = asyncio.create_task(_poll_loop())
    try:
        yield _queue_iter(queue)
    finally:
        log.debug("poll: shutting down")
        poll_task.cancel()
        await asyncio.gather(poll_task, return_exceptions=True)
        await queue.put(None)
        log.debug("poll: done")
