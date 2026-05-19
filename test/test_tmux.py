"""Tests for bsdb.tmux.launch() and bsdb.tmux.attach()."""

import subprocess
from datetime import timezone
from unittest.mock import MagicMock, patch

import pytest

from bsdb.tmux import SessionInfo, attach, launch


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _kill_session(info: SessionInfo) -> None:
    """Best-effort cleanup: kill a tmux session, ignoring errors."""
    try:
        if info.remote:
            subprocess.run(
                ["ssh", info.remote, f"tmux kill-session -t {shlex_quote(info.session_id)}"],
                capture_output=True,
            )
        else:
            subprocess.run(
                ["tmux", "kill-session", "-t", info.session_id],
                capture_output=True,
            )
    except Exception:
        pass


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


def _session_exists_local(session_id: str) -> bool:
    r = subprocess.run(
        ["tmux", "has-session", "-t", session_id],
        capture_output=True,
    )
    return r.returncode == 0


def _session_exists_remote(session_id: str, remote: str) -> bool:
    import shlex
    r = subprocess.run(
        ["ssh", remote, f"tmux has-session -t {shlex.quote(session_id)}"],
        capture_output=True,
    )
    return r.returncode == 0


@pytest.fixture
def launched_sessions():
    """Collect SessionInfo objects and kill them all on teardown."""
    sessions: list[SessionInfo] = []
    yield sessions
    for info in sessions:
        _kill_session(info)


@pytest.fixture(scope="session")
def ssh_localhost():
    """Skip the test if passwordless SSH to localhost is unavailable."""
    r = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "localhost", "true"],
        capture_output=True,
    )
    if r.returncode != 0:
        pytest.skip("passwordless SSH to localhost not available")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLaunchLocal:
    def test_returns_session_info(self, launched_sessions):
        info = launch("sleep 300", name="bsdb-test-local")
        launched_sessions.append(info)

        assert isinstance(info, SessionInfo)

    def test_session_name_preserved(self, launched_sessions):
        info = launch("sleep 300", name="bsdb-test-local-name")
        launched_sessions.append(info)

        assert info.session_name == "bsdb-test-local-name"

    def test_remote_is_none(self, launched_sessions):
        info = launch("sleep 300", name="bsdb-test-local-remote")
        launched_sessions.append(info)

        assert info.remote is None

    def test_tmux_ids_populated(self, launched_sessions):
        info = launch("sleep 300", name="bsdb-test-local-ids")
        launched_sessions.append(info)

        assert info.session_id.startswith("$")
        assert info.window_id.startswith("@")
        assert info.pane_id.startswith("%")

    def test_pane_pid_positive(self, launched_sessions):
        info = launch("sleep 300", name="bsdb-test-local-pid")
        launched_sessions.append(info)

        assert info.pane_pid > 0

    def test_created_at_is_utc(self, launched_sessions):
        info = launch("sleep 300", name="bsdb-test-local-ts")
        launched_sessions.append(info)

        assert info.created_at.tzinfo is timezone.utc

    def test_session_actually_exists_in_tmux(self, launched_sessions):
        info = launch("sleep 300", name="bsdb-test-local-exists")
        launched_sessions.append(info)

        assert _session_exists_local(info.session_id)

    def test_auto_generated_name(self, launched_sessions):
        info = launch("sleep 300")
        launched_sessions.append(info)

        assert info.session_name.startswith("bsdb-")
        assert len(info.session_name) == len("bsdb-") + 8

    def test_cmd_normalised_from_list(self, launched_sessions):
        info = launch(["sleep", "300"], name="bsdb-test-local-list")
        launched_sessions.append(info)

        assert info.cmd == "sleep 300"

    def test_cwd_stored(self, launched_sessions, tmp_path):
        info = launch("sleep 300", name="bsdb-test-local-cwd", cwd=str(tmp_path))
        launched_sessions.append(info)

        assert info.cwd == str(tmp_path)


class TestLaunchRemoteLocalhost:
    """Same tests repeated over SSH to localhost."""

    def test_returns_session_info(self, launched_sessions, ssh_localhost):
        info = launch("sleep 300", name="bsdb-test-rem", remote="localhost")
        launched_sessions.append(info)

        assert isinstance(info, SessionInfo)

    def test_remote_set_correctly(self, launched_sessions, ssh_localhost):
        info = launch("sleep 300", name="bsdb-test-rem-remote", remote="localhost")
        launched_sessions.append(info)

        assert info.remote == "localhost"

    def test_tmux_ids_populated(self, launched_sessions, ssh_localhost):
        info = launch("sleep 300", name="bsdb-test-rem-ids", remote="localhost")
        launched_sessions.append(info)

        assert info.session_id.startswith("$")
        assert info.window_id.startswith("@")
        assert info.pane_id.startswith("%")

    def test_pane_pid_positive(self, launched_sessions, ssh_localhost):
        info = launch("sleep 300", name="bsdb-test-rem-pid", remote="localhost")
        launched_sessions.append(info)

        assert info.pane_pid > 0

    def test_session_actually_exists_via_ssh(self, launched_sessions, ssh_localhost):
        info = launch("sleep 300", name="bsdb-test-rem-exists", remote="localhost")
        launched_sessions.append(info)

        assert _session_exists_remote(info.session_id, "localhost")


# ---------------------------------------------------------------------------
# attach() target resolution (no real tmux interaction needed)
# ---------------------------------------------------------------------------


class TestAttachTargetResolution:
    """Verify that attach() resolves the session/remote correctly before
    calling subprocess.  The actual attach is mocked so no TTY is needed."""

    def _run_attach(self, *args, **kwargs):
        """Call attach() with subprocess.run mocked out; return the call args."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("bsdb.tmux.subprocess.run", return_value=mock_result) as m:
            attach(*args, **kwargs)
        return m.call_args

    def test_session_name_local(self):
        call = self._run_attach("my-session")
        cmd = call.args[0]
        assert cmd == ["tmux", "attach-session", "-t", "my-session"]

    def test_session_id_local(self):
        call = self._run_attach("$3")
        cmd = call.args[0]
        assert cmd == ["tmux", "attach-session", "-t", "$3"]

    def test_remote_colon_name(self):
        """'host:name' form sets remote and strips the prefix from target."""
        call = self._run_attach("haiku:my-session")
        # ["ssh", "-t", remote, shell-cmd]
        cmd = call.args[0]
        assert cmd[0] == "ssh"
        assert cmd[1] == "-t"
        assert cmd[2] == "haiku"
        assert "my-session" in cmd[3]
        assert "haiku" not in cmd[3].replace("$SHELL", "")  # remote not in tmux cmd

    def test_remote_colon_name_explicit_remote_overrides(self):
        """An explicit remote= overrides the one embedded in the string."""
        call = self._run_attach("ignored-host:my-session", remote="real-host")
        cmd = call.args[0]
        assert cmd[2] == "real-host"

    def test_session_info_uses_stored_remote(self):
        info = SessionInfo(
            session_name="s", session_id="$0", window_id="@0", pane_id="%0",
            pane_pid=1, remote="stored-host", created_at=__import__("datetime").datetime.now(),
            cmd="sleep 1", cwd=None,
        )
        call = self._run_attach(info)
        cmd = call.args[0]
        assert cmd[2] == "stored-host"
        assert "'$0'" in cmd[3]  # session_id single-quoted to suppress shell expansion

    def test_session_info_remote_overridden(self):
        info = SessionInfo(
            session_name="s", session_id="$0", window_id="@0", pane_id="%0",
            pane_pid=1, remote="original", created_at=__import__("datetime").datetime.now(),
            cmd="sleep 1", cwd=None,
        )
        call = self._run_attach(info, remote="override")
        cmd = call.args[0]
        assert cmd[2] == "override"
