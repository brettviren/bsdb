import shlex
import sys

import click

from bsdb.tmux import attach as tmux_attach
from bsdb.tmux import connection as tmux_connection
from bsdb.tmux import launch as tmux_launch
from bsdb.tmux import list as tmux_list


@click.group()
def cli():
    pass


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


def main():
    cli()


if __name__ == "__main__":
    main()
