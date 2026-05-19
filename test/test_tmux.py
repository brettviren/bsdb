"""Tests for bsdb.tmux: launch(), attach(), list(), and helpers."""

import subprocess
from datetime import timezone
from unittest.mock import ANY, MagicMock, patch

import pytest

from bsdb.tmux import SessionInfo, _resolve_remote, attach, connection, launch
from bsdb.tmux import list as tmux_list


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


# ---------------------------------------------------------------------------
# _resolve_remote
# ---------------------------------------------------------------------------


class TestResolveRemote:
    def test_none_returns_none(self):
        assert _resolve_remote(None) is None

    def test_empty_string_returns_none(self):
        assert _resolve_remote("") is None

    def test_localhost_returns_none(self):
        assert _resolve_remote("localhost") is None

    def test_trailing_colon_stripped(self):
        assert _resolve_remote("haiku:") == "haiku"

    def test_localhost_colon_returns_none(self):
        assert _resolve_remote("localhost:") is None

    def test_regular_host_unchanged(self):
        assert _resolve_remote("haiku") == "haiku"

    def test_user_at_host_unchanged(self):
        assert _resolve_remote("user@haiku") == "user@haiku"


# ---------------------------------------------------------------------------
# list()
# ---------------------------------------------------------------------------

_SAMPLE_ROW = "\t".join([
    "0",            # window_index
    "0",            # pane_index
    "$2",           # session_id
    "my-session",   # session_name
    "1700000000",   # session_created (Unix timestamp)
    "@1",           # window_id
    "%3",           # pane_id
    "42",           # pane_pid
    "emacs",        # pane_current_command
    "/home/user",   # pane_current_path
])


def _mock_result(stdout: str) -> MagicMock:
    m = MagicMock()
    m.stdout = stdout
    return m


class TestList:
    def test_returns_session_info(self):
        with patch("bsdb.tmux._run", return_value=_mock_result(_SAMPLE_ROW + "\n")):
            sessions = tmux_list()
        assert len(sessions) == 1
        s = sessions[0]
        assert s.session_name == "my-session"
        assert s.session_id == "$2"
        assert s.window_id == "@1"
        assert s.pane_id == "%3"
        assert s.pane_pid == 42
        assert s.cmd == "emacs"
        assert s.cwd == "/home/user"
        assert s.remote is None

    def test_created_at_utc(self):
        with patch("bsdb.tmux._run", return_value=_mock_result(_SAMPLE_ROW + "\n")):
            sessions = tmux_list()
        assert sessions[0].created_at.tzinfo is timezone.utc

    def test_localhost_queries_locally(self):
        with patch("bsdb.tmux._run", return_value=_mock_result(_SAMPLE_ROW + "\n")) as m:
            sessions = tmux_list("localhost")
        m.assert_called_once_with(["tmux", "list-panes", "-a", "-F", ANY], None)
        assert sessions[0].remote is None

    def test_remote_host_passed_through(self):
        with patch("bsdb.tmux._run", return_value=_mock_result(_SAMPLE_ROW + "\n")) as m:
            tmux_list("haiku")
        m.assert_called_once_with(["tmux", "list-panes", "-a", "-F", ANY], "haiku")

    def test_empty_on_no_server(self):
        with patch("bsdb.tmux._run", side_effect=subprocess.CalledProcessError(1, "tmux")):
            assert tmux_list() == []

    def test_skips_non_first_pane(self):
        second_pane = "\t".join([
            "0", "1", "$2", "my-session", "1700000000", "@1", "%4", "99", "bash", "/tmp",
        ])
        output = _SAMPLE_ROW + "\n" + second_pane + "\n"
        with patch("bsdb.tmux._run", return_value=_mock_result(output)):
            sessions = tmux_list()
        assert len(sessions) == 1

    def test_skips_non_first_window(self):
        second_window = "\t".join([
            "1", "0", "$2", "my-session", "1700000000", "@2", "%5", "99", "vim", "/tmp",
        ])
        output = _SAMPLE_ROW + "\n" + second_window + "\n"
        with patch("bsdb.tmux._run", return_value=_mock_result(output)):
            sessions = tmux_list()
        assert len(sessions) == 1

    def test_multiple_sessions(self):
        row2 = "\t".join([
            "0", "0", "$3", "other-session", "1700000001", "@2", "%4", "99", "vim", "/tmp",
        ])
        output = _SAMPLE_ROW + "\n" + row2 + "\n"
        with patch("bsdb.tmux._run", return_value=_mock_result(output)):
            sessions = tmux_list()
        assert len(sessions) == 2
        names = {s.session_name for s in sessions}
        assert names == {"my-session", "other-session"}

    def test_empty_cwd_becomes_none(self):
        row = "\t".join(["0", "0", "$2", "s", "1700000000", "@0", "%0", "1", "sh", ""])
        with patch("bsdb.tmux._run", return_value=_mock_result(row + "\n")):
            sessions = tmux_list()
        assert sessions[0].cwd is None


# ---------------------------------------------------------------------------
# connection()
# ---------------------------------------------------------------------------


class TestConnection:
    def test_local_session_name(self):
        cmd = connection("my-session")
        assert cmd == ["tmux", "attach-session", "-t", "my-session"]

    def test_local_session_id(self):
        cmd = connection("$3")
        assert cmd == ["tmux", "attach-session", "-t", "$3"]

    def test_remote_via_colon_string(self):
        cmd = connection("haiku:my-session")
        assert cmd[0] == "ssh"
        assert cmd[1] == "-t"
        assert cmd[2] == "haiku"
        assert "my-session" in cmd[3]
        assert "haiku" not in cmd[3].replace("$SHELL", "")

    def test_remote_via_kwarg(self):
        cmd = connection("my-session", remote="haiku")
        assert cmd[0] == "ssh"
        assert cmd[1] == "-t"
        assert cmd[2] == "haiku"
        assert "my-session" in cmd[3]

    def test_session_info_local(self):
        info = SessionInfo(
            session_name="s", session_id="$5", window_id="@0", pane_id="%0",
            pane_pid=1, remote=None, created_at=__import__("datetime").datetime.now(),
            cmd="sleep 1", cwd=None,
        )
        cmd = connection(info)
        assert cmd == ["tmux", "attach-session", "-t", "$5"]

    def test_session_info_remote(self):
        info = SessionInfo(
            session_name="s", session_id="$5", window_id="@0", pane_id="%0",
            pane_pid=1, remote="haiku", created_at=__import__("datetime").datetime.now(),
            cmd="sleep 1", cwd=None,
        )
        cmd = connection(info)
        assert cmd[0] == "ssh"
        assert cmd[2] == "haiku"
        assert "'$5'" in cmd[3]

    def test_explicit_remote_overrides_session_info(self):
        info = SessionInfo(
            session_name="s", session_id="$5", window_id="@0", pane_id="%0",
            pane_pid=1, remote="original", created_at=__import__("datetime").datetime.now(),
            cmd="sleep 1", cwd=None,
        )
        cmd = connection(info, remote="override")
        assert cmd[2] == "override"

    def test_localhost_treated_as_local(self):
        cmd = connection("my-session", remote="localhost")
        assert cmd == ["tmux", "attach-session", "-t", "my-session"]

    def test_returns_list(self):
        assert isinstance(connection("s"), list)
