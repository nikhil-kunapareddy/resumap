"""Starting Resumap: the server itself, and the double-click app around it.

``serve()`` runs the app in this process until Ctrl-C or the page's Quit
button; ``run.py`` calls it. ``open_app()`` is what the packaged app
(``packaging/``) does on every double-click: open the Resumap that is already
running, or start one in the background, wait until it answers, open the
browser, and exit. The server outlives that short-lived launcher, so opening
the app again after closing the tab just brings the page back.

Like ``main``, this sits above the layers and may import anything.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Literal

from . import config

# Every /api response carries this header (main._log_requests), which is how a
# launcher tells a running Resumap from some other program on the port.
MARKER = "X-Resumap-Run"
# Ports tried, upward from config.PORT, when another program holds one.
PORT_SPAN = 10
# The first launch after a download is slow: macOS scans the whole bundle.
READY_TIMEOUT = 90.0
# Where claude, codex and pi usually live. An app opened from Finder starts
# with only /usr/bin:/bin:/usr/sbin:/sbin and would find none of them.
EXTRA_BIN_DIRS = (
    "~/.local/bin",
    "~/.claude/local",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.npm-global/bin",
    "~/.bun/bin",
    "~/.volta/bin",
)

Probe = Literal["resumap", "free", "other"]


# -- the server ---------------------------------------------------------------


def serve(*, port: int | None = None, open_browser: bool = False) -> None:
    """Run Resumap in this process until Ctrl-C or the page's Quit button."""
    if config.FROZEN:
        fix_path()

    import uvicorn

    from .main import app

    services = app.state.services
    services.start()

    port = port or config.PORT
    url = f"http://{config.HOST}:{port}"
    print(f"\n  Resumap  ->  {url}")
    why = services.gateway.status()
    name = services.gateway.registry.current_name()
    if why:
        print(f"  WARNING: the {name} backend is not ready.")
        print(f"           {why}")
    else:
        print(f"  Backend: {name}")
    print("  Ctrl-C (or Quit in the page) to stop.\n", flush=True)

    server = uvicorn.Server(uvicorn.Config(app, host=config.HOST, port=port, log_level="warning"))
    services.lifecycle.allow_quit(lambda: setattr(server, "should_exit", True))
    if open_browser:

        def open_soon() -> None:
            time.sleep(1.2)  # long enough for the server to be listening
            webbrowser.open(url)

        threading.Thread(target=open_soon, daemon=True).start()
    server.run()


# -- finding the CLIs ---------------------------------------------------------


def merged_path(*chunks: str) -> str:
    """Every directory in `chunks` (each a PATH-style string), first occurrence
    kept, in order; `~` expanded, and directories that do not exist dropped."""
    out: list[str] = []
    for chunk in chunks:
        for d in chunk.split(os.pathsep):
            d = os.path.expanduser(d.strip())
            if d and d not in out and os.path.isdir(d):
                out.append(d)
    return os.pathsep.join(out)


def login_shell_path() -> str:
    """The PATH a new terminal window would have, or "" if the shell will not say."""
    if sys.platform == "win32":
        return ""
    shell = os.environ.get("SHELL") or "/bin/zsh"
    tag = "__RESUMAP_PATH__"
    try:
        out = subprocess.run(
            [shell, "-ilc", f'printf "\\n{tag}%s" "$PATH"'],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    _, found, path = out.stdout.rpartition(tag)
    return path.strip() if found else ""


def fix_path() -> None:
    """Give this process (and so every CLI backend it runs) a terminal's PATH."""
    os.environ["PATH"] = merged_path(
        login_shell_path(), os.environ.get("PATH", ""), os.pathsep.join(EXTRA_BIN_DIRS)
    )


# -- the double-click app -----------------------------------------------------


def probe(host: str, port: int) -> Probe:
    """Whether `port` is free, held by a Resumap, or held by something else."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        if s.connect_ex((host, port)) != 0:
            return "free"
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/status", timeout=2) as resp:
            headers = resp.headers
    except urllib.error.HTTPError as exc:
        headers = exc.headers
    except Exception:  # noqa: BLE001 - anything that is not an HTTP server is "other"
        return "other"
    return "resumap" if headers.get(MARKER) else "other"


def find_port(host: str, start: int, span: int = PORT_SPAN) -> tuple[int | None, Probe]:
    """The first port from `start` that is a running Resumap or free to start
    one on, and which of the two it is; (None, "other") if all are taken."""
    for port in range(start, start + span):
        state = probe(host, port)
        if state != "other":
            return port, state
    return None, "other"


def alert(message: str) -> None:
    """Tell the user something went wrong, with no terminal to print to."""
    print(message, file=sys.stderr)
    if sys.platform == "darwin":
        text = message.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e", f'display alert "Resumap" message "{text}" as critical'],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )


# server.log takes the detached server's console, a line for every request,
# so it is started afresh past this size instead of growing for good.
SERVER_LOG_MAX = 1_000_000


def rotate_log(path: Path, limit: int = SERVER_LOG_MAX) -> None:
    """Move `path` aside to `<name>.1` (replacing the last one) once it outgrows `limit`."""
    try:
        if path.stat().st_size > limit:
            path.replace(path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass


def open_app() -> None:
    """Open the running Resumap in the browser, starting it first if needed."""
    host = config.HOST
    port, state = find_port(host, config.PORT)
    if port is None:
        alert(
            f"Every port from {config.PORT} to {config.PORT + PORT_SPAN - 1} is in use by "
            "another program, so Resumap has nowhere to start."
        )
        return
    url = f"http://{host}:{port}"
    if state == "resumap":
        webbrowser.open(url)
        return

    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = config.LOG_DIR / "server.log"
    rotate_log(log)
    # The app binary re-runs itself as the server; from a checkout, the entry
    # script that called us does.
    cmd = [sys.executable] if config.FROZEN else [sys.executable, os.path.abspath(sys.argv[0])]
    with open(log, "ab") as out:
        proc = subprocess.Popen(
            [*cmd, "--serve", "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # outlives this launcher
        )
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        # Checked before the child: if a second launcher won the race for this
        # port, ours exits but theirs answers, and that is fine.
        if probe(host, port) == "resumap":
            webbrowser.open(url)
            return
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    if probe(host, port) == "resumap":
        webbrowser.open(url)
        return
    alert(f"Resumap did not start. What it printed is in {log}")


def _port_arg(args: list[str]) -> int | None:
    if "--port" in args:
        i = args.index("--port")
        if i + 1 < len(args) and args[i + 1].isdigit():
            return int(args[i + 1])
    return None


def main(argv: list[str] | None = None) -> None:
    """The packaged app's entry point: `--serve [--port N]` runs the server
    (what the launcher starts), anything else is a double-click."""
    args = sys.argv[1:] if argv is None else argv
    if "--serve" in args:
        serve(port=_port_arg(args), open_browser=False)
    else:
        open_app()


if __name__ == "__main__":
    main()
