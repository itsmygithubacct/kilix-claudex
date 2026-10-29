#!/usr/bin/env python3
"""Drive a dedicated Chrome profile through the DevTools protocol over a pipe.

``--remote-debugging-pipe`` opens no TCP port, so no other local process can
drive the logged-in browser. Chrome marks such a session as automated
(``navigator.webdriver``); nothing here hides that. Signing in happens in an
ordinary, non-automated window started by ``launch_login``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import time
from urllib.parse import urlsplit

BROWSERS = ("google-chrome", "google-chrome-stable", "chromium")
DISPLAYS = ("virtual", "screen", "headless")
BODY_LIMIT = 2 << 20
CHROME_FLAGS = ("--no-first-run", "--no-default-browser-check", "--window-size=1280,900")


class BrowserError(Exception):
    pass


class ProfileBusy(BrowserError):
    pass


def find_browser(preferred=None) -> str:
    for name in ([preferred] if preferred else []) + list(BROWSERS):
        path = shutil.which(name) if name else None
        if path:
            return path
    raise BrowserError(f"no Chrome browser found (tried {', '.join(BROWSERS)})")


def profile_owner(profile: Path) -> int | None:
    """PID of a live Chrome using ``profile``, from its SingletonLock (``host-pid``)."""
    try:
        target = os.readlink(Path(profile) / "SingletonLock")
    except OSError:
        return None
    host, _, pid = target.rpartition("-")
    if not pid.isdigit() or (host and host != os.uname().nodename):
        return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    return int(pid)


def private_profile(profile: Path) -> Path:
    profile = Path(profile)
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(profile, 0o700)
    owner = profile_owner(profile)
    if owner:
        raise ProfileBusy(f"the usage browser profile is open in Chrome (pid {owner}); close that window first")
    return profile


def launch_login(profile: Path, urls, binary=None) -> int:
    """Open an ordinary, visible Chrome on the dedicated profile; returns its PID."""
    profile = private_profile(profile)
    process = subprocess.Popen([find_browser(binary), f"--user-data-dir={profile}", *CHROME_FLAGS, *urls],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    return process.pid


class VirtualDisplay:
    """A private Xvfb server for the life of a ``with`` block."""

    def __enter__(self):
        if not shutil.which("Xvfb"):
            raise BrowserError("Xvfb is not installed; use --display screen or --display headless")
        read_fd, write_fd = os.pipe()
        self.process = subprocess.Popen(
            ["Xvfb", "-displayfd", str(write_fd), "-nolisten", "tcp", "-screen", "0", "1280x900x24"],
            pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(write_fd)
        try:
            ready, _, _ = select.select([read_fd], [], [], 15)
            number = os.read(read_fd, 32).decode().strip() if ready else ""
        finally:
            os.close(read_fd)
        if not number.isdigit():
            self.__exit__(None, None, None)
            raise BrowserError("Xvfb did not report a display")
        self.display = f":{number}"
        return self

    def __exit__(self, *exc):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


class ChromePipe:
    """One Chrome process on ``profile``, spoken to over fds 3 and 4."""

    def __init__(self, profile: Path, *, display: str = "virtual", binary=None, env=None):
        if display not in DISPLAYS:
            raise BrowserError(f"display must be one of {', '.join(DISPLAYS)}")
        argv = [find_browser(binary), f"--user-data-dir={private_profile(profile)}",
                "--remote-debugging-pipe", *CHROME_FLAGS]
        if display == "headless":
            argv.append("--headless=new")
        argv.append("about:blank")
        commands_read, self._write = os.pipe()
        self._read, replies_write = os.pipe()

        def wire():
            os.dup2(commands_read, 3)
            os.dup2(replies_write, 4)

        self.process = subprocess.Popen(argv, pass_fds=(3, 4), preexec_fn=wire, env=env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
        os.close(commands_read)
        os.close(replies_write)
        self._buffer, self._next, self.events = b"", 0, []

    # --- protocol ---------------------------------------------------------

    def pump(self, timeout: float) -> None:
        ready, _, _ = select.select([self._read], [], [], max(0.0, timeout))
        if not ready:
            return
        chunk = os.read(self._read, 1 << 20)
        if not chunk:
            raise BrowserError("Chrome closed the DevTools pipe")
        self._buffer += chunk
        while b"\0" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\0", 1)
            try:
                self.events.append(json.loads(raw))
            except ValueError:
                continue

    def call(self, method: str, params=None, session=None, timeout: float = 30.0) -> dict:
        self._next += 1
        message = {"id": self._next, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        os.write(self._write, json.dumps(message).encode() + b"\0")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for index, event in enumerate(self.events):
                if event.get("id") == message["id"]:
                    del self.events[index]
                    if "error" in event:
                        raise BrowserError(f"{method}: {event['error'].get('message', event['error'])}")
                    return event.get("result") or {}
            self.pump(min(0.25, deadline - time.monotonic()))
        raise BrowserError(f"{method} timed out")

    def close(self) -> None:
        try:
            self.call("Browser.close", timeout=5)
        except (BrowserError, OSError):
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        for fd in (self._read, self._write):
            try:
                os.close(fd)
            except OSError:
                pass

    # --- pages ------------------------------------------------------------

    def capture(self, url: str, *, domains=(), timeout: float = 45.0, settle: float = 4.0, done=None) -> dict:
        """Load ``url`` in a new tab and return its text plus JSON responses from ``domains``.

        ``done(page)`` may end the wait early once the wanted data has arrived.
        """
        target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        page = {"url": url, "responses": [], "loaded": False}
        try:
            self.call("Network.enable", session=session)
            self.call("Page.enable", session=session)
            self.call("Page.navigate", {"url": url}, session=session)
            pending: dict[str, dict] = {}
            start = last_activity = time.monotonic()
            while time.monotonic() - start < timeout:
                self.pump(0.25)
                events, self.events = ([e for e in self.events if e.get("sessionId") == session],
                                       [e for e in self.events if e.get("sessionId") != session])
                for event in events:
                    method, params = event.get("method"), event.get("params") or {}
                    if method == "Page.loadEventFired":
                        page["loaded"] = True
                    elif method == "Network.responseReceived":
                        last_activity = time.monotonic()
                        response = params.get("response") or {}
                        host = urlsplit(response.get("url", "")).hostname or ""
                        mime = str(response.get("mimeType"))
                        if ("json" in mime or "grpc-web" in mime) and _in_domains(host, domains):
                            pending[params.get("requestId")] = {"url": response.get("url"), "status": response.get("status"),
                                                                "mime": mime}
                    elif method == "Network.loadingFinished" and params.get("requestId") in pending:
                        last_activity = time.monotonic()
                        entry = pending.pop(params["requestId"])
                        entry.update(self._body(params["requestId"], session, params.get("encodedDataLength")))
                        page["responses"].append(entry)
                if done and page["responses"] and done(page):
                    break
                if page["loaded"] and time.monotonic() - last_activity > settle:
                    break
            state = self.call("Runtime.evaluate", {
                "expression": "JSON.stringify({href: location.href, title: document.title,"
                              " text: document.body ? document.body.innerText : ''})",
                "returnByValue": True}, session=session)
            summary = json.loads(((state.get("result") or {}).get("value")) or "{}")
            page.update(final_url=summary.get("href"), title=summary.get("title"),
                        text=(summary.get("text") or "")[:BODY_LIMIT])
            return page
        finally:
            try:
                self.call("Target.closeTarget", {"targetId": target}, timeout=5)
            except BrowserError:
                pass

    def _body(self, request_id, session, size) -> dict:
        """``{"body": parsed JSON}``, or ``{"raw": base64}`` for a binary body such as gRPC-web."""
        if isinstance(size, (int, float)) and size > BODY_LIMIT:
            return {"body": None}
        try:
            result = self.call("Network.getResponseBody", {"requestId": request_id}, session=session, timeout=10)
        except BrowserError:
            return {"body": None}
        if result.get("base64Encoded"):
            return {"body": None, "raw": result.get("body") or ""}
        try:
            return {"body": json.loads(result.get("body") or "")}
        except ValueError:
            return {"body": None}


def _in_domains(host: str, domains) -> bool:
    return not domains or any(host == d or host.endswith("." + d) for d in domains)
