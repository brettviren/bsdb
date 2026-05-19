import sys

import click

from bsdb.tmux import attach as tmux_attach
from bsdb.tmux import launch as tmux_launch


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


@cli.group()
def session():
    """Manage existing tmux sessions."""
    pass


@session.command("attach")
@click.argument("target")
@click.option("-r", "--remote", default=None, help="SSH target, e.g. user@host.")
def session_attach(target, remote):
    """Attach to session TARGET.

    TARGET may be a session name, a tmux ID (e.g. $0), or the
    "remote:name" string printed by the launch command.
    """
    rc = tmux_attach(target, remote=remote)
    if rc:
        sys.exit(rc)


def main():
    cli()


if __name__ == "__main__":
    main()
