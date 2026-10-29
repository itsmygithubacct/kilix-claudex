"""Serialize status readers and bind their input to one dedicated tmux pane."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat

from idleness import assess_idle


class StatusSessionError(Exception):
    pass


@contextmanager
def reader_lock(session):
    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "claudex-status"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise StatusSessionError("status lock directory must be private and owned")
    fd = os.open(root / (session + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StatusSessionError(f"another reader is using {session}") from error
        yield


class Transport:
    """Check pane identity before every operation and drafts before placing text.

    The lock coordinates readers. An unrelated tmux writer can still race a
    check; these sessions must remain dedicated to account status.
    """

    def __init__(self, run, session, agent, runs_agent):
        self.run = run
        self.session = session
        self.agent = agent
        self.runs_agent = runs_agent
        self.bound = None

    def inspect(self, result=None):
        result = result if result is not None else self.run(["show", "--json", self.session])
        if result.returncode:
            raise StatusSessionError(f"cannot inspect {self.session}")
        try:
            data = json.loads(result.stdout)["data"]
            panes = data["panes"]
            if not isinstance(panes, list) or len(panes) != 1 or not isinstance(panes[0], dict):
                raise ValueError("expected one pane")
            pane = panes[0]
            identity = (pane["pane_id"], pane["pid"], str(pane["window"]), str(pane["pane"]))
            if (not isinstance(identity[0], str) or not re.fullmatch(r"%\d+", identity[0])
                    or type(identity[1]) is not int or identity[1] <= 0
                    or not all(re.fullmatch(r"\d+", value) for value in identity[2:])):
                raise ValueError("invalid pane identity")
            attached = (data.get("session") or {}).get("attached", 0)
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise StatusSessionError(f"{self.session} must contain exactly one identifiable pane") from error
        if self.bound is not None and identity != self.bound:
            raise StatusSessionError(f"{self.session} pane identity changed")
        self.bound = identity
        self.target = f"{self.session}:{identity[2]}.{identity[3]}"
        return pane, attached

    def __call__(self, args, **kwargs):
        operation = args[0]
        if operation == "show":
            result = self.run(args, **kwargs)
            self.inspect(result)
            return result
        if operation not in {"capture", "type", "send", "key"}:
            return self.run(args, **kwargs)
        pane, attached = self.inspect()
        if operation in {"type", "send", "key"}:
            if attached:
                raise StatusSessionError(f"detach {self.session} before reading usage")
            if operation != "type" and not self.runs_agent(pane):
                raise StatusSessionError(f"{self.session} no longer runs the expected agent")
            if operation in {"type", "send"}:
                captured = self.run(["capture", "--ansi", "-n", "80", self.target])
                if captured.returncode:
                    raise StatusSessionError(f"cannot inspect input in {self.session}")
                if operation == "type":
                    lines = [line for line in captured.stdout.splitlines() if line.strip()]
                    ready = (pane.get("command") in {"bash", "sh", "zsh", "fish", "dash", "nu", "login"}
                             and bool(lines and re.search(r"[$%#]\s*$", lines[-1])))
                else:
                    ready = assess_idle({"coding_session": {"provider": self.agent}, "activity": "idle"},
                                        captured.stdout)["ready"]
                if not ready:
                    raise StatusSessionError(f"{self.session} input is occupied or not ready")
        args = [self.target if arg == self.session else arg for arg in args]
        return self.run(args, **kwargs)
