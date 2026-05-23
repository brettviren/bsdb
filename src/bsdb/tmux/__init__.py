"""Primitive functions for launching and managing tmux sessions locally or via SSH."""

from __future__ import annotations

import shlex
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass
class SessionInfo:
    """All identifiers needed to reattach to or track a launched tmux session.

    Fields with tmux IDs (session_id, window_id, pane_id) are stable across
    renames and are safe to use with ``-t`` even if the session is renamed.
    """

    session_name: str      # human-readable name set at launch
    session_id: str        # tmux-internal unique ID, e.g. "$0"
    window_id: str         # first window ID, e.g. "@0"
    pane_id: str           # first pane ID, e.g. "%0"
    pane_pid: int          # PID of the process running in the first pane
    remote: Optional[str]  # SSH target used at launch; None means local
    created_at: datetime   # session creation time (UTC)
    cmd: str               # normalised command string passed to tmux
    cwd: Optional[str]     # working directory used, or None
    attached: int          # number of clients currently attached
    activity_at: datetime  # time of last pane activity (UTC)
    last_attached_at: Optional[datetime]  # time a client last attached (UTC); None if never attached
    many_attached: bool    # True when more than one client is attached


def _resolve_remote(remote: Optional[str]) -> Optional[str]:
    """Canonicalize a remote specifier; returns ``None`` for local execution.

    ``None``, empty string, ``"localhost"``, and any of these with a trailing
    colon all map to ``None``.  Everything else is returned unchanged.
    """
    if remote:
        remote = remote.rstrip(":")
    return None if not remote or remote == "localhost" else remote


def _resolve_session_spec(
    session: "SessionInfo | str",
    remote: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Resolve a session specifier to ``(tmux_target, canonicalized_remote)``.

    Accepted forms for *session*:

    * :class:`SessionInfo` — uses ``session_id``; inherits stored ``remote``
    * ``"host:target"`` — splits on the first colon
    * ``"target"`` — plain tmux name or ``$N`` ID; remote stays as given

    An explicit *remote* argument always takes precedence over any remote
    embedded in *session*.  The resolved remote is passed through
    :func:`_resolve_remote`.
    """
    if isinstance(session, SessionInfo):
        target = session.session_id
        resolved = remote if remote is not None else session.remote
    elif ":" in session:
        embedded, target = session.split(":", 1)
        resolved = remote if remote is not None else embedded
    else:
        target = session
        resolved = remote
    return target, _resolve_remote(resolved)


def _run(args: list[str], remote: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run *args* locally, or via ``ssh remote`` if *remote* is given.

    Remote commands are piped to ``$SHELL -l`` on the remote so the user's
    actual login shell (bash, zsh, fish …) sources its profile and the full
    PATH — including Homebrew, nix, conda, etc. — is available.  Using stdin
    also avoids the nested-quoting problem of ``ssh host 'cmd -l -c "…"'``.
    """
    if remote:
        return subprocess.run(
            ["ssh", remote, "$SHELL -l"],
            input=shlex.join(args) + "\n",
            capture_output=True, text=True, check=True,
        )
    return subprocess.run(args, capture_output=True, text=True, check=True)


def connection(
    session: "SessionInfo | str",
    remote: Optional[str] = None,
) -> list[str]:
    """Return the command list needed to attach to an existing tmux session.

    Accepts the same arguments as :func:`attach`.  The returned list can be
    passed directly to :func:`subprocess.run` or formatted with
    :func:`shlex.join` for copy-paste execution.

    Args:
        session: A :class:`SessionInfo`, a tmux session ID (e.g. ``$0``), a
                 session name, or a ``"host:name"`` string as printed by
                 :func:`launch`.
        remote:  SSH target; ``None`` = local.

    Returns:
        ``["tmux", "attach-session", "-t", target]`` for local sessions, or
        ``["ssh", "-t", remote, "$SHELL -l -c <cmd>"]`` for remote ones.
    """
    target, remote = _resolve_session_spec(session, remote)
    args = ["tmux", "attach-session", "-t", target]
    if remote:
        # ssh -t allocates a PTY so the tmux client can drive the terminal.
        # $SHELL -l ensures the remote PATH is set up.
        return ["ssh", "-t", remote, f"$SHELL -l -c {shlex.quote(shlex.join(args))}"]
    return args


def attach(
    session: "SessionInfo | str",
    remote: Optional[str] = None,
) -> int:
    """Attach to an existing tmux session in the current terminal.

    Args:
        session: A :class:`SessionInfo`, a tmux session ID (e.g. ``$0``), a
                 session name, or a ``"host:name"`` string as printed by
                 :func:`launch`.  When a :class:`SessionInfo` is given its
                 stored ``remote`` is used unless *remote* overrides it.
        remote:  SSH target; ``None`` = local.

    Returns:
        Exit code of ``tmux attach-session`` (0 = normal detach).
    """
    return subprocess.run(connection(session, remote)).returncode


def list(remote: Optional[str] = None) -> "list[SessionInfo]":
    """Return all active tmux sessions visible from *remote*.

    Args:
        remote: SSH target, ``None``, or ``"localhost"`` for local sessions.
                ``"host:"`` form is also accepted.  Multiple calls may be made
                to query several hosts.

    Returns:
        One :class:`SessionInfo` per session, in the order tmux reports them.
        ``cmd`` and ``cwd`` reflect the pane's *current* process and working
        directory rather than the original :func:`launch` values.

    An empty list is returned (no exception) when there is no tmux server or
    when SSH fails.
    """
    effective_remote = _resolve_remote(remote)

    fmt = "\t".join([
        "#{window_index}",
        "#{pane_index}",
        "#{session_id}",
        "#{session_name}",
        "#{session_created}",
        "#{window_id}",
        "#{pane_id}",
        "#{pane_pid}",
        "#{pane_current_command}",
        "#{pane_current_path}",
        "#{session_attached}",
        "#{session_activity}",
        "#{session_last_attached}",
        "#{session_many_attached}",
    ])

    try:
        result = _run(["tmux", "list-panes", "-a", "-F", fmt], effective_remote)
    except subprocess.CalledProcessError:
        return []

    seen: dict[str, SessionInfo] = {}
    for line in result.stdout.splitlines():
        if not line:
            continue
        parts = line.split("\t", 13)
        if len(parts) < 14:
            continue
        (win_idx, pane_idx, session_id, session_name,
         created_ts, window_id, pane_id, pane_pid_str, cmd, cwd,
         attached_str, activity_ts, last_attached_ts, many_attached_str) = parts
        if int(win_idx) != 0 or int(pane_idx) != 0:
            continue
        if session_id in seen:
            continue
        seen[session_id] = SessionInfo(
            session_name=session_name,
            session_id=session_id,
            window_id=window_id,
            pane_id=pane_id,
            pane_pid=int(pane_pid_str),
            remote=effective_remote,
            created_at=datetime.fromtimestamp(int(created_ts), tz=timezone.utc),
            cmd=cmd,
            cwd=cwd.strip() or None,
            attached=int(attached_str),
            activity_at=datetime.fromtimestamp(int(activity_ts), tz=timezone.utc),
            last_attached_at=datetime.fromtimestamp(int(last_attached_ts), tz=timezone.utc) if last_attached_ts.strip() else None,
            many_attached=bool(int(many_attached_str)),
        )

    return [*seen.values()]


def launch(
    cmd: str | list[str],
    name: Optional[str] = None,
    cwd: Optional[str] = None,
    remote: Optional[str] = None,
) -> SessionInfo:
    """Start *cmd* in a new detached tmux session.

    Args:
        cmd:    Command to run.  A list is joined into a shell command string.
        name:   tmux session name.  Auto-generated (``bsdb-<hex8>``) if None.
        cwd:    Working directory for *cmd* (remote path when *remote* is set).
        remote: SSH target such as ``"user@host"`` or ``"host"``.
                ``None`` runs tmux on the local machine.

    Returns:
        :class:`SessionInfo` carrying every identifier needed to reattach via
        :func:`attach` or to populate a tracking database row.

    Raises:
        subprocess.CalledProcessError: if tmux or ssh exits non-zero.
    """
    cmd_str = shlex.join(cmd) if not isinstance(cmd, str) else cmd

    if name is None:
        name = f"bsdb-{uuid.uuid4().hex[:8]}"

    new_args: list[str] = ["tmux", "new-session", "-d", "-s", name]
    if cwd:
        new_args += ["-c", cwd]
    new_args.append(cmd_str)

    _run(new_args, remote)

    fmt = "|".join([
        "#{session_id}", "#{window_id}", "#{pane_id}", "#{pane_pid}",
        "#{session_created}", "#{session_attached}",
        "#{session_activity}", "#{session_last_attached}", "#{session_many_attached}",
    ])
    result = _run(["tmux", "display-message", "-t", name, "-p", fmt], remote)

    # Take the last line in case login scripts wrote anything to stdout before
    # the tmux output.
    (session_id, window_id, pane_id, pane_pid_str,
     created_ts, attached_str, activity_ts, last_attached_ts, many_attached_str) = (
        result.stdout.strip().split("\n")[-1].split("|")
    )

    return SessionInfo(
        session_name=name,
        session_id=session_id,
        window_id=window_id,
        pane_id=pane_id,
        pane_pid=int(pane_pid_str),
        remote=remote,
        created_at=datetime.fromtimestamp(int(created_ts), tz=timezone.utc),
        cmd=cmd_str,
        cwd=cwd,
        attached=int(attached_str),
        activity_at=datetime.fromtimestamp(int(activity_ts), tz=timezone.utc),
        last_attached_at=datetime.fromtimestamp(int(last_attached_ts), tz=timezone.utc) if last_attached_ts.strip() else None,
        many_attached=bool(int(many_attached_str)),
    )
