import asyncio
import logging
import shlex
import signal
import sys

import click

from bsdb.tmux import attach as tmux_attach
from bsdb.tmux import connection as tmux_connection
from bsdb.tmux import launch as tmux_launch
from bsdb.tmux import list as tmux_list
import bsdb.tmux.monitor as tmux_monitor

log = logging.getLogger(__name__)


def _configure_logging(level: str, sink: str) -> None:
    numeric = getattr(logging, level.upper(), None)
    if numeric is None:
        raise click.BadParameter(f"unknown level {level!r}", param_hint="-L/--log-level")
    if sink == "stderr":
        handler = logging.StreamHandler(sys.stderr)
    elif sink == "stdout":
        handler = logging.StreamHandler(sys.stdout)
    else:
        handler = logging.FileHandler(sink)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    # force=True removes any handlers already installed (e.g. by pytest)
    logging.basicConfig(level=numeric, handlers=[handler], force=True)


@click.group()
@click.option(
    "-L", "--log-level",
    default="info",
    metavar="LEVEL",
    help="Logging level: debug, info, warning, error.  [default: info]",
)
@click.option(
    "-l", "--log-sink",
    default="stderr",
    metavar="DEST",
    help="Log destination: stderr, stdout, or a file path.  [default: stderr]",
)
def cli(log_level, log_sink):
    _configure_logging(log_level, log_sink)


@cli.command(context_settings={"allow_interspersed_args": False})
@click.argument("cmd", nargs=-1, required=True)
@click.option("-n", "--name", default=None, help="tmux session name.")
@click.option("-C", "--cwd", default=None, help="Working directory for the command.")
@click.option("-r", "--remote", default=None, help="SSH target, e.g. user@host.")
def launch(cmd, name, cwd, remote):
    """Launch CMD in a detached tmux session."""
    info = tmux_launch(list(cmd), name=name, cwd=cwd, remote=remote)
    where = f"{info.remote}:" if info.remote else ""
    click.echo(f"launched {where}{info.session_name}  (pid {info.pane_pid})")


@cli.command("attach")
@click.argument("target")
@click.option("-r", "--remote", default=None, help="SSH target, e.g. user@host.")
def attach(target, remote):
    """Attach to session TARGET.

    TARGET may be a session name, a tmux ID (e.g. $0), or the
    "remote:name" string printed by the launch command.
    """
    rc = tmux_attach(target, remote=remote)
    if rc:
        sys.exit(rc)


@cli.command("connection")
@click.argument("target")
@click.option("-r", "--remote", default=None, help="SSH target, e.g. user@host.")
def connection_cmd(target, remote):
    """Print the command to attach to session TARGET.

    TARGET may be a session name, a tmux ID (e.g. $0), or the
    "remote:name" string printed by the launch command.
    """
    click.echo(shlex.join(tmux_connection(target, remote=remote)))


@cli.command("list")
@click.argument("remotes", nargs=-1)
@click.pass_context
def list_cmd(ctx, remotes):
    """List tmux sessions on each REMOTE (alias: ls).

    REMOTE may be a hostname, "localhost" for local sessions, or the "host:"
    form.  Multiple REMOTEs may be given.  If no REMOTE is given, print help.
    """
    if not remotes:
        click.echo(ctx.get_help())
        return
    for remote_arg in remotes:
        for info in tmux_list(remote_arg):
            where = f"{info.remote}:" if info.remote else ""
            click.echo(f"{where}{info.session_name}  (pid {info.pane_pid})")


cli.add_command(list_cmd, name="ls")


_MONITOR_METHODS = {
    "pipe": tmux_monitor.pipe,
    "socat": tmux_monitor.socat,
    "listen": tmux_monitor.listen,
    "poll": tmux_monitor.poll,
}


@cli.command("monitor")
@click.argument("target")
@click.option("-r", "--remote", default=None, help="SSH target, e.g. user@host.")
@click.option(
    "-m", "--method",
    type=click.Choice(list(_MONITOR_METHODS)),
    default="pipe",
    show_default=True,
    help="Push-monitoring method.",
)
@click.option(
    "-d", "--dwell",
    type=int,
    default=10,
    show_default=True,
    help="Silence timeout in seconds passed to tmux monitor-silence.",
)
def monitor_cmd(target, remote, method, dwell):
    """Watch TARGET and print a line each time a tmux hook fires.

    TARGET may be a session name, a tmux ID (e.g. $0), or the
    "remote:name" string printed by the launch command.
    Interrupt with Ctrl-C to cleanly tear down hooks and exit.
    """

    async def _run():
        log.debug("monitor: method=%s target=%r remote=%r dwell=%s", method, target, remote, dwell)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _on_sigint():
            log.debug("monitor: SIGINT received")
            stop.set()

        loop.add_signal_handler(signal.SIGINT, _on_sigint)

        mon_fn = _MONITOR_METHODS[method]
        log.debug("monitor: entering %s context manager", method)
        async with mon_fn(target, remote=remote, dwell=dwell) as events:
            log.debug("monitor: context ready, starting consume loop")

            async def _consume():
                async for info in events:
                    where = f"{info.remote}:" if info.remote else ""
                    ts = info.activity_at.strftime("%H:%M:%S")
                    log.debug("monitor: event session=%s pid=%s", info.session_name, info.pane_pid)
                    click.echo(
                        f"{ts}  {where}{info.session_name}"
                        f"  pid={info.pane_pid}"
                        f"  attached={info.attached}"
                        f"  cmd={info.cmd}"
                    )
                log.debug("monitor: consume loop ended")

            consume_task = asyncio.create_task(_consume())
            log.debug("monitor: waiting for SIGINT")
            await stop.wait()
            log.debug("monitor: cancelling consume task")
            consume_task.cancel()
            await asyncio.gather(consume_task, return_exceptions=True)

        log.debug("monitor: cleanup complete, exiting")

    asyncio.run(_run())


def main():
    cli()


if __name__ == "__main__":
    main()
