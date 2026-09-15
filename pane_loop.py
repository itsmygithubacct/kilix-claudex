#!/usr/bin/env python3
"""Claudex: a local Kilix collaboration CLI and live-configurable prompt timer."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid

import dashboard
from idleness import codex_queue_hint, composer_is_empty, composer_text
from limits import CodexHistory, assess_limits, limit_report, pane_agent
from providers import CODEX_ID, COMPANION_ID, LIMIT_PROVIDERS, SUPPORTED


ALIASES = {"orchestrator": "claudex-orchestrator", "companion": "claudex-companion"}
# Guards against a runaway loop (`loop every 1e-6`); tests use short intervals.
MIN_INTERVAL_MINUTES = 0.01
DEFAULT_DASHBOARD_PORT = 8797
DEFAULT_TARGETS = ["orchestrator", "companion"]


def default_config():
    configured = os.environ.get("CLAUDEX_CONFIG")
    if configured:
        return Path(configured).expanduser()
    data_home = Path(os.environ.get("GPU_TERMINAL_HOME") or Path.home() / ".local/gpu_terminal")
    return data_home.expanduser() / "kilix-apps" / "kilix-claudex" / "loop.json"


class LoopError(Exception):
    pass


class Busy(LoopError):
    pass


class TargetChanged(LoopError):
    pass


class DeliveryError(LoopError):
    def __init__(self, reason, evidence, *, fatal=False):
        super().__init__(reason)
        self.evidence = evidence
        self.fatal = fatal or evidence["text_attempted"]


def save_delivery(runtime, evidence):
    with exclusive(runtime / "last-send.lock", blocking=True):
        write_json(runtime / "last-send.json", evidence)


@contextmanager
def record_attempt(runtime, evidence):
    try:
        yield
    except Exception as error:  # any failure after typing must keep its evidence
        evidence.update(status="uncertain" if evidence["text_attempted"] else "error",
                        reason=str(error), checked_at=time.time())
        if evidence["text_attempted"]:
            try:
                save_delivery(runtime, evidence)
            except OSError as persistence_error:
                evidence["persistence_error"] = str(persistence_error)
        raise DeliveryError(str(error), evidence, fatal=isinstance(error, TargetChanged)) from error


def atomic_write(path, value, *, overwrite=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(value)
        if overwrite:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                return False
        return True
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path, value):
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def read_utf8(path):
    # read_text() translates CRLF/CR, which can change literal instructions.
    with path.open(encoding="utf-8", newline="") as stream:
        return stream.read()


def read_stdin():
    buffer = getattr(sys.stdin, "buffer", None)
    return buffer.read().decode("utf-8") if buffer is not None else sys.stdin.read()


def load_config(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise LoopError("loop.json must be a JSON object")
    minutes = value.get("interval_minutes")
    if (isinstance(minutes, bool) or not isinstance(minutes, (int, float))
            or not math.isfinite(minutes) or minutes < MIN_INTERVAL_MINUTES):
        raise LoopError(f"interval_minutes must be a finite number of at least {MIN_INTERVAL_MINUTES}")
    if not isinstance(value.get("enabled"), bool):
        raise LoopError("enabled must be true or false")
    floor = value.get("min_remaining_percent", 0)
    if (isinstance(floor, bool) or not isinstance(floor, (int, float))
            or not math.isfinite(floor) or not 0 <= floor <= 100):
        raise LoopError("min_remaining_percent must be a number from 0 to 100")
    for key in ("target", "prompt_file"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise LoopError(f"{key} must be a nonempty string")
    return value


def prompt_path(config_path, config):
    return (config_path.parent / config["prompt_file"]).resolve()


@contextmanager
def exclusive(path, *, blocking=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise Busy(f"lock is held: {path}") from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def initialize(config_path, runtime):
    examples = Path(__file__).resolve().parent / "examples"
    with exclusive(runtime / "config.lock"):
        config_created = atomic_write(config_path, read_utf8(examples / "loop.json"), overwrite=False)
        config = load_config(config_path)
        path = prompt_path(config_path, config)
        prompt_created = atomic_write(path, read_utf8(examples / "prompt.txt"), overwrite=False)
        if not path.is_file():
            raise LoopError(f"prompt path is not a file: {path}")
    return {"config_file": str(config_path), "prompt_file": str(path), "runtime_dir": str(runtime),
            "config_created": config_created, "prompt_created": prompt_created,
            "enabled": config["enabled"]}


class Kilix:
    def __init__(self):
        executable = os.environ.get("CLAUDEX_KILIX_PANES") or shutil.which("kilix-panes")
        self.command = [executable] if executable else ["kilix", "panes"]

    def call(self, *args, text=None):
        try:
            result = subprocess.run(
                [*self.command, *args], input=text if text is not None else "",
                capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise LoopError(f"Kilix call failed: {error}") from error
        if result.returncode:
            raise LoopError(result.stderr.strip() or result.stdout.strip() or "Kilix call failed")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise LoopError("Kilix did not return JSON") from error
        if not isinstance(value, dict):
            raise LoopError("Kilix did not return a JSON object")
        return value

    def dump(self, target, lines=80, ansi=False):
        snapshot = self.call("dump", ALIASES.get(str(target), str(target)),
                             "--lines", str(lines), "--screen", "--json")
        if not isinstance(snapshot.get("pane"), dict) or not isinstance(snapshot.get("text"), str):
            raise TargetChanged("Kilix did not return a live pane and screen")
        if ansi:
            # Pane Center's dump is plain. Preserve styling for the detector:
            # Codex renders its empty-input hint faint, unlike an actual draft.
            try:
                screen = subprocess.run(
                    ["kilix", "@", "get-text", "--match", f"id:{int(snapshot['pane']['pane_id'])}",
                     "--extent", "screen", "--ansi"],
                    input="", capture_output=True, text=True, encoding="utf-8", timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise LoopError(f"ANSI screen capture failed: {error}") from error
            if screen.returncode:
                raise LoopError(screen.stderr.strip() or "ANSI screen capture failed")
            snapshot["ansi_text"] = screen.stdout
        return snapshot


def identity(pane):
    if not isinstance(pane, dict) or not isinstance(pane.get("pane_id"), int):
        raise TargetChanged("target pane is missing or malformed")
    coding = pane.get("coding_session") or {}
    broker = pane.get("broker") or {}
    if not isinstance(coding, dict) or not isinstance(broker, dict):
        raise TargetChanged("target session metadata is malformed")
    return (pane["pane_id"], broker.get("session_id"), coding.get("provider"),
            coding.get("session_id"))


def require_agent(pane, expected=None):
    current = identity(pane)
    if expected is not None and current != expected:
        raise TargetChanged("target pane or coding session changed; restart the loop to bind again")
    coding = pane.get("coding_session") or {}
    if (not current[1] or not current[3] or current[2] not in SUPPORTED
            or coding.get("state") != "live"):
        raise TargetChanged(f"target must still contain a live {CODEX_ID} or {COMPANION_ID} session")
    return current


def assess(pane, text, allow_busy=False):
    try:
        from idleness import assess_idle
    except ImportError as error:
        raise LoopError("idleness.py is not available yet") from error
    result = assess_idle(pane, text, allow_busy=allow_busy)
    if not isinstance(result, dict) or not isinstance(result.get("ready"), bool):
        raise LoopError("idleness detector must return ready (bool) and reason")
    return result


def compact(value):
    return "".join(value.split())


def message_visible(message, screen, agent="codex"):
    content = composer_text(agent, screen)
    return content is not None and compact(message) == compact(content)


def has_terminal_controls(text):
    for char in text:
        code = ord(char)
        if ((code < 32 and char not in "\n\r\t") or 127 <= code <= 159
                or code in {0x061C, 0x200E, 0x200F, 0x2028, 0x2029}
                or 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069):
            return True
    return False


def make_message(prompt, runtime, token):
    if not prompt.strip():
        raise LoopError("prompt is empty; no message sent")
    if has_terminal_controls(prompt):
        raise LoopError("prompt contains terminal control characters")
    # Raw send-text is not bracketed paste. Keep multiline/large text in a file
    # so newlines cannot become extra submissions or keys in the target TUI.
    if (any(char in prompt for char in "\n\r\t") or prompt != prompt.strip()
            or prompt.startswith(("/", "!", "@", "?", "#"))
            or any("\u2800" <= char <= "\u28ff" for char in prompt)
            or len(prompt.encode("utf-8")) > 750):
        snapshot = runtime / "prompts" / f"{token}.txt"
        if has_terminal_controls(str(snapshot)) or any(char in str(snapshot) for char in "\n\r\t"):
            raise LoopError("prompt file path contains terminal control characters")
        atomic_write(snapshot, prompt)
        prompt = f"Read and follow the instructions in this UTF-8 prompt file: {snapshot}"
    message = f"{prompt} [{token}]"
    if len(message.encode("utf-8")) > 900:
        raise LoopError("message path is too long for a bounded Kilix message")
    return message


def matching_turn(pane, evidence):
    recorded = (pane.get("coding_session") or {}).get("last_user_message")
    if not isinstance(recorded, str):
        return False
    # New evidence includes the exact wire message. Older records can only be
    # checked by their trailing marker, never by a token quoted mid-message.
    message = evidence.get("message")
    return (recorded.strip() == message if message is not None
            else recorded.rstrip().endswith(f"[{evidence['token']}]"))


def withdraw(client, bound, message, evidence):
    """Delete verified, unsubmitted text after the target turned busy.

    Only called when the composer held exactly ``message`` in the last
    readback, so deleting that many characters removes nothing else.
    """
    evidence["withdraw_attempted"] = True
    deleted = client.call("send", bound[1], "--allow-self", "--json", text="\x7f" * len(message))
    if deleted.get("accepted") is not True:
        raise LoopError("target became busy before submission and Kilix did not accept the "
                        "withdrawal; text may remain in the composer")
    cleared = client.dump(str(bound[0]))
    require_agent(cleared["pane"], bound)
    if not composer_is_empty(bound[2], cleared["text"]):
        raise LoopError("target became busy before submission and the withdrawal could not be "
                        "verified; text may remain in the composer")
    evidence.update(withdrawn=True, text_may_remain=False)
    return {**evidence, "status": "skipped",
            "reason": "target became busy before submission; the placed text was withdrawn"}


def deliver(client, target, prompt, runtime, expected=None, allow_busy=False, on_phase=None,
            min_remaining_percent=0, allow_limited=False):
    before = client.dump(target)
    bound = require_agent(before["pane"], expected)
    evidence = {"pane_id": bound[0], "broker_session": bound[1], "provider": bound[2],
                "coding_session": bound[3], "token": None, "text_attempted": False,
                "text_accepted": False, "placement_verified": False, "submission_key": None,
                "key_attempted": False, "key_accepted": False, "text_may_remain": False,
                "turn_observed": False, "queue_observed": False}
    with exclusive(runtime / f"send-{bound[0]}.lock"), record_attempt(runtime, evidence):
        # Refresh after locking: another cooperating sender may have just sent.
        before = client.dump(str(bound[0]), ansi=True)
        require_agent(before["pane"], bound)
        verdict = assess(before["pane"], before.get("ansi_text", before["text"]), allow_busy=allow_busy)
        if not verdict["ready"]:
            return {"status": "skipped", "reason": verdict.get("reason", "not idle")}
        if not allow_limited:
            # An exhausted target can't act on the message; unknown never blocks.
            limit = assess_limits(before["pane"], before.get("ansi_text", before["text"]),
                                  low_percent=min_remaining_percent)
            if limit["state"] == "exhausted" or (min_remaining_percent > 0 and limit["state"] == "low"):
                return {"status": "skipped", "reason": f"token limit {limit['state']}: {limit['reason']}",
                        "limits": limit, "text_attempted": False, "text_may_remain": False}
        token = "cx-" + uuid.uuid4().hex[:12]
        message = make_message(prompt, runtime, token)
        evidence.update(token=token, message=message)
        if on_phase:
            on_phase("sending", token)
        # Target the full broker session as well as checking the pane identity.
        # No shell evaluation, title matching, or environment spoofing is used.
        evidence.update(text_attempted=True, text_may_remain=True)
        sent = client.call("send", bound[1], "--allow-self", "--json", text=message)
        if sent.get("accepted") is not True:
            raise LoopError("Kilix did not accept the text; inspect the pane before retrying")
        evidence["text_accepted"] = True
        placed = client.dump(str(bound[0]))
        require_agent(placed["pane"], bound)
        if not message_visible(message, placed["text"], bound[2]):
            raise LoopError("text placement is unverified; text may remain in the composer; "
                            "inspect the pane before retrying")
        evidence["placement_verified"] = True
        # A working Codex composer can advertise Tab as its queue action.
        # Use the same broker-bound transport for the key as for the text.
        activity = placed["pane"].get("activity")
        queue_hint = bound[2] == "codex" and codex_queue_hint(placed["text"])
        working = (activity == "working" or queue_hint
                   or verdict.get("screen", {}).get("state") == "working")
        if activity == "waiting":
            # A dialog may own the keyboard now: send no further keys at all.
            raise LoopError("target became waiting before submission; inspect the pane before retrying")
        if working and not allow_busy:
            return withdraw(client, bound, message, evidence)
        queue_with_tab = queue_hint
        submission_key = "Tab" if queue_with_tab else "Enter"
        evidence.update(submission_key=submission_key, key_attempted=True)
        if queue_with_tab:
            entered = client.call("send", bound[1], "--allow-self", "--json", text="\t")
        else:
            entered = client.call("send", bound[1], "--allow-self", "--enter", "--json", text="")
        if entered.get("accepted") is not True:
            raise LoopError(f"Kilix did not accept {submission_key}; inspect the pane before retrying")
        evidence["key_accepted"] = True
        # Styled capture: a faint placeholder must not read as a leftover draft.
        after = client.dump(str(bound[0]), ansi=True)
        require_agent(after["pane"], bound)
        observed = matching_turn(after["pane"], evidence)
        if bound[2] == "codex" and not observed:
            time.sleep(0.5)
            after = client.dump(str(bound[0]), ansi=True)
            require_agent(after["pane"], bound)
            observed = matching_turn(after["pane"], evidence)
        after_verdict = assess(after["pane"], after.get("ansi_text", after["text"]), allow_busy=True)
        queued = after_verdict.get("screen_state") == "queued"
        result = {**evidence, "status": "sent" if observed else "pending", "pane_id": bound[0], "token": token,
                  "broker_session": bound[1], "provider": bound[2], "coding_session": bound[3],
                  "placement_verified": True, "turn_observed": observed,
                  "submission_key": submission_key, "queue_observed": queued,
                  "text_may_remain": not (observed or queued or after_verdict.get("screen_state") in {"idle", "working"}),
                  "screen_changed_after_enter": (after["text"] != placed["text"]) if not queue_with_tab else None,
                  "screen_changed_after_submit": after["text"] != placed["text"],
                  "sent_at": time.time()}
        if observed:
            result["observed_message"] = (after["pane"].get("coding_session") or {}).get("last_user_message")
            result["observed_at"] = time.time()
        evidence.update(result)
        if not observed and (after_verdict.get("screen_state") == "occupied"
                             or (queue_with_tab and not queued)):
            raise LoopError(f"{submission_key} did not visibly submit or queue the message; "
                            "inspect the pane before retrying")
        if bound[2] == "codex" and not observed and not allow_busy:
            raise LoopError("Enter was sent but no matching Codex turn was observed; inspect the pane before retrying")
        save_delivery(runtime, result)
        return result


def read_delivery(path):
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise LoopError("no delivery has been recorded yet") from error
    except (OSError, ValueError) as error:
        raise LoopError(f"cannot read {path.name}: {error}") from error
    if (not isinstance(record, dict) or type(record.get("pane_id")) is not int
            or not isinstance(record.get("token"), str) or not record["token"]):
        raise LoopError(f"{path.name} is malformed; inspect it before verifying")
    return record


def verify_last_send(client, runtime):
    previous = read_delivery(runtime / "last-send.json")
    snapshot = client.dump(str(previous["pane_id"]))
    expected = (previous["pane_id"], previous["broker_session"], previous["provider"],
                previous["coding_session"]) if "broker_session" in previous else None
    require_agent(snapshot["pane"], expected)
    observed = matching_turn(snapshot["pane"], previous)
    previous["turn_observed"] = previous.get("turn_observed", False) or observed
    previous["status"] = "sent" if previous["turn_observed"] else "pending"
    previous["checked_at"] = time.time()
    if observed:
        previous.update(observed_message=(snapshot["pane"].get("coding_session") or {}).get("last_user_message"),
                        observed_at=time.time(), text_may_remain=False)
    # Screen reads are slow. Another pane can receive a newer send while this
    # verification is in flight; never replace its evidence with this record.
    with exclusive(runtime / "last-send.lock", blocking=True):
        current = read_delivery(runtime / "last-send.json")
        previous["is_latest"] = current["token"] == previous["token"]
        if previous["is_latest"]:
            previous["turn_observed"] = previous["turn_observed"] or current.get("turn_observed", False)
            previous["status"] = "sent" if previous["turn_observed"] else "pending"
            if current.get("turn_observed"):
                for key in ("observed_message", "observed_at"):
                    if key in current and key not in previous:
                        previous[key] = current[key]
                previous["text_may_remain"] = False
            write_json(runtime / "last-send.json", previous)
    return previous


def process_start(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def read_state(runtime):
    try:
        state = json.loads((runtime / "state.json").read_text())
    except (OSError, ValueError):
        return {"running": False, "phase": "stopped"}
    if not isinstance(state, dict):
        return {"running": False, "phase": "stopped"}
    pid = state.get("pid")
    marker = state.get("process_start")
    # Missing markers must never compare equal merely because /proc is gone.
    # Only a positive PID and a recorded, nonempty start marker identify a run.
    state["running"] = bool(state.get("running") is True
                            and type(pid) is int and pid > 0
                            and isinstance(marker, str) and marker
                            and process_start(pid) == marker)
    return state


def run_loop(config_path, client, runtime, once=False, allow_busy=False):
    stop = threading.Event()
    if not once:
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())
    with exclusive(runtime / "loop.lock"):
        config = load_config(config_path)
        initial = client.dump(config["target"])
        bound = require_agent(initial["pane"])
        if bound[2] != "codex":
            raise LoopError("the self-prompt loop requires a Codex target")
        if once:
            if not config["enabled"]:
                return {"status": "skipped", "reason": "loop is paused"}
            return deliver(client, str(bound[0]), read_utf8(prompt_path(config_path, config)),
                           runtime, bound, allow_busy,
                           min_remaining_percent=config.get("min_remaining_percent", 0))
        state = {"running": True, "pid": os.getpid(), "process_start": process_start(os.getpid()),
                 "target": config["target"], "pane_id": bound[0], "broker_session": bound[1],
                 "coding_session": bound[3], "started_at": time.time(), "phase": "waiting",
                 "last_attempt_at": None, "last_fire_at": None, "next_fire_at": None}
        anchor = time.monotonic()
        last_error = None

        def save(**changes):
            state.update(changes, updated_at=time.time())
            write_json(runtime / "state.json", state)

        def event(message):
            print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message, flush=True)

        save()
        event(f"started; bound to pane {bound[0]}; config {config_path}")
        try:
            while not stop.is_set():
                try:
                    config = load_config(config_path)
                    if config["target"] != state["target"]:
                        raise LoopError("target changed in config; stop and restart to bind the new target")
                    interval = config["interval_minutes"] * 60
                except (OSError, ValueError, LoopError) as error:
                    if str(error) != last_error:
                        event(f"config error: {error}")
                    last_error = str(error)
                    save(phase="config_error", last_error=last_error, next_fire_at=None)
                    stop.wait(1)
                    continue
                last_error = None
                if not config["enabled"]:
                    anchor = time.monotonic()
                    save(phase="paused", next_fire_at=None, last_error=None)
                    stop.wait(1)
                    continue
                remaining = anchor + interval - time.monotonic()
                save(phase="waiting", next_fire_at=time.time() + max(0, remaining),
                     interval_minutes=config["interval_minutes"], last_error=None)
                if remaining > 0:
                    stop.wait(min(1, remaining))
                    continue
                # Advance even on a skipped tick: no backlog of queued prompts.
                anchor = time.monotonic()
                save(phase="checking", last_attempt_at=time.time(), next_fire_at=time.time() + interval)
                try:
                    prompt = read_utf8(prompt_path(config_path, config))
                    if not prompt.strip():
                        raise ValueError("prompt is empty")
                except (OSError, ValueError) as error:
                    save(phase="skipped", last_result={"status": "skipped", "reason": str(error)})
                    event(f"skipped: {error}")
                    continue
                try:
                    result = deliver(client, str(bound[0]), prompt, runtime, bound,
                                     on_phase=lambda phase, token: save(phase=phase, token=token),
                                     min_remaining_percent=config.get("min_remaining_percent", 0))
                except Busy:
                    result = {"status": "skipped", "reason": "another sender holds the pane lock"}
                except (LoopError, OSError, ValueError) as error:
                    if isinstance(error, TargetChanged) or (isinstance(error, DeliveryError) and error.fatal):
                        raise
                    result = {"status": "skipped", "reason": str(error)}
                save(phase=result["status"], last_result=result)
                if result["status"] == "sent":
                    save(last_fire_at=result["sent_at"])
                event(json.dumps(result))
                # CLI reads can take seconds. Never catch up missed intervals.
                if time.monotonic() >= anchor + interval:
                    anchor = time.monotonic()
        except Exception as error:
            save(phase="error", last_error=str(error))
            event(f"stopped on error: {error}")
            raise
        finally:
            save(running=False, next_fire_at=None,
                 phase="error" if state["phase"] == "error" else "stopped")
        return state


def start(config_path, client, runtime):
    load_config(config_path)
    if read_state(runtime)["running"]:
        raise LoopError("loop is already running")
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "loop.log").open("a") as log:
        child = subprocess.Popen(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--config", str(config_path), "loop", "run"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            cwd=config_path.parent,
        )
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise LoopError(f"loop failed to start; read {runtime / 'loop.log'}")
        state = read_state(runtime)
        if state.get("pid") == child.pid and state["running"]:
            return state
        time.sleep(0.1)
    child.terminate()
    raise LoopError(f"startup timed out; read {runtime / 'loop.log'}")


def stop_loop(runtime):
    state = read_state(runtime)
    if not state["running"]:
        return {"running": False, "phase": "stopped"}
    # pidfd avoids signalling an unrelated process if the recorded PID exits
    # and is reused between the identity check and signal delivery.
    try:
        fd = os.pidfd_open(state["pid"])
    except ProcessLookupError:
        return {"running": False, "phase": "stopped"}
    try:
        if process_start(state["pid"]) != state.get("process_start"):
            raise LoopError("loop PID changed; refusing to signal it")
        signal.pidfd_send_signal(fd, signal.SIGTERM)
    finally:
        os.close(fd)
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        state = read_state(runtime)
        if not state["running"]:
            return state
        time.sleep(0.1)
    raise LoopError("stop requested; a Kilix transaction is still finishing (see loop status)")


def dashboard_report_builder(client, targets, low_percent):
    history = CodexHistory()

    def build():
        now = time.time()
        history.refresh(now)
        panes = []
        for target in targets:
            try:
                snapshot = client.dump(target)
                panes.append({"target": target, "pane_id": snapshot["pane"].get("pane_id"),
                              "title": snapshot["pane"].get("title"),
                              **limit_report(snapshot["pane"], snapshot["text"], now=now,
                                             low_percent=low_percent, history=history)})
            except Exception as error:  # one missing pane shouldn't blank the page
                panes.append({"target": target, "state": "unknown", "windows": {},
                              "reason": f"{type(error).__name__}: {error}", "error": True})
        return {"panes": panes, "providers": dashboard.provider_links()}

    return build


def dashboard_state(runtime):
    try:
        state = json.loads((runtime / "dashboard.json").read_text())
    except (OSError, ValueError):
        return {"running": False}
    if not isinstance(state, dict):
        return {"running": False}
    pid, marker, port = state.get("pid"), state.get("process_start"), state.get("port")
    state["running"] = bool(type(pid) is int and pid > 0 and isinstance(marker, str) and marker
                            and type(port) is int and process_start(pid) == marker)
    if state["running"]:
        state["url"] = f"http://127.0.0.1:{port}/"
    return state


def dashboard_healthy(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


def serve_dashboard(runtime, client, port, targets, low_percent):
    server = dashboard.make_server(dashboard_report_builder(client, targets, low_percent), port)
    actual = server.server_address[1]
    write_json(runtime / "dashboard.json", {"pid": os.getpid(), "process_start": process_start(os.getpid()),
                                            "port": actual, "targets": targets, "started_at": time.time()})

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)
    print(json.dumps({"serving": f"http://127.0.0.1:{actual}/"}), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        if dashboard_state(runtime).get("pid") == os.getpid():
            (runtime / "dashboard.json").unlink(missing_ok=True)
    return {"running": False, "port": actual}


def start_dashboard(config_path, runtime, port, targets, low_percent):
    state = dashboard_state(runtime)
    if state["running"] and dashboard_healthy(state["port"]):
        return {**state, "already_running": True}
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / "dashboard.log").open("a") as log:
        child = subprocess.Popen(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--config", str(config_path),
             "dashboard", "serve", "--port", str(port), "--low", str(low_percent), *targets],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, cwd=runtime,
        )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise LoopError(f"dashboard failed to start; read {runtime / 'dashboard.log'}")
        state = dashboard_state(runtime)
        if state["running"] and state.get("pid") == child.pid and dashboard_healthy(state["port"]):
            return state
        time.sleep(0.1)
    child.terminate()
    raise LoopError(f"dashboard startup timed out; read {runtime / 'dashboard.log'}")


def stop_dashboard(runtime):
    state = dashboard_state(runtime)
    if not state["running"]:
        return {"running": False}
    try:
        fd = os.pidfd_open(state["pid"])
    except ProcessLookupError:
        return {"running": False}
    try:
        if process_start(state["pid"]) != state.get("process_start"):
            raise LoopError("dashboard PID changed; refusing to signal it")
        signal.pidfd_send_signal(fd, signal.SIGTERM)
    finally:
        os.close(fd)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if not dashboard_state(runtime)["running"]:
            return {"running": False, "stopped_port": state["port"]}
        time.sleep(0.1)
    raise LoopError("dashboard stop requested but the process is still running")


# `kilix open-url` prefers these desktop browsers, in this order (config/browser.sh).
REAL_BROWSERS = ("google-chrome", "chromium-browser", "firefox-esr")


def launch_browser(url, title):
    """Open ``url`` the way `kilix open-url` does.

    With a desktop browser installed, kilix opens an ordinary browser window
    with its usual profile, so existing logins apply. It runs as a background
    Kilix process, inheriting Kilix's display environment, and creates no tab.
    Only without a desktop browser does kilix render the page inside a pane;
    a titled tab is opened for that.
    """
    desktop = next((path for path in map(shutil.which, REAL_BROWSERS) if path), None)
    placement = ["--type=background"] if desktop else ["--type=tab", f"--title={title}"]
    try:
        done = subprocess.run(["kilix", "@", "launch", *placement, "kilix", "open-url", url],
                              capture_output=True, text=True, encoding="utf-8", timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LoopError(f"could not open {url}: {error}") from error
    if done.returncode:
        raise LoopError(done.stderr.strip() or done.stdout.strip() or "kilix launch failed")
    return {"url": url, "mode": "desktop browser" if desktop else "kilix pane", "browser": desktop,
            "title": None if desktop else title, "window_id": done.stdout.strip() or None}


def parser():
    result = argparse.ArgumentParser(prog="claudex", description=__doc__)
    result.add_argument("--config", type=Path, default=default_config(),
                        help="configuration file (CLAUDEX_CONFIG or the private Kilix app directory)")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create private configuration and prompt files without replacing existing data")
    loop = commands.add_parser("loop", help="control the editable self-prompt timer")
    actions = loop.add_subparsers(dest="action", required=True)
    for name in ("start", "run", "stop", "status", "pause", "resume"):
        actions.add_parser(name)
    every = actions.add_parser("every", help="change interval without restarting")
    every.add_argument("minutes", type=float)
    prompt = actions.add_parser("prompt", help="show or replace the live prompt")
    prompt.add_argument("text", nargs="?")
    prompt.add_argument("--file", type=Path, help="copy prompt text from a UTF-8 file; - reads stdin")
    once = actions.add_parser("once", help="perform one tick while the background loop is stopped")
    once.add_argument("--allow-busy", action="store_true", help="explicitly permit a working agent (test/manual use)")
    panes = commands.add_parser("panes", help="list the Claudex panes")
    panes.add_argument("--all", action="store_true", help="include the other Kilix panes")
    panes.add_argument("--json", action="store_true", help="include full Pane Center records")
    read = commands.add_parser("read", aliases=["monitor"], help="read a pane; add --watch for repeated snapshots")
    read.add_argument("target", nargs="?", default="companion")
    read.add_argument("--lines", type=int, default=40)
    read.add_argument("--watch", type=float, metavar="SECONDS")
    idle = commands.add_parser("idle", help="explain whether a pane is ready for another prompt")
    idle.add_argument("target", nargs="?", default="orchestrator")
    send = commands.add_parser("send", help="send and submit a message using the shared pane lock")
    send.add_argument("target")
    send.add_argument("text", nargs="?", help="message; read stdin if omitted")
    send.add_argument("--allow-busy", action="store_true", help="permit a queued FYI to a working agent")
    send.add_argument("--allow-limited", action="store_true",
                      help="send even when the target's token limit is exhausted")
    commands.add_parser("verify", help="recheck recorded-turn evidence for the most recent send")
    limit = commands.add_parser("limits", help="report weekly, five-hour and monthly token limits for agent panes")
    limit.add_argument("targets", nargs="*", default=["orchestrator", "companion"])
    limit.add_argument("--low", type=float, default=10.0, metavar="PERCENT",
                       help="remaining percentage reported as low (default 10)")
    limit.add_argument("--history", action="store_true",
                       help="add usage history, burn rate and projected exhaustion")
    board = commands.add_parser("dashboard", help="local token-limit dashboard and official usage pages")
    board_actions = board.add_subparsers(dest="action", required=True)
    for name, text in (("serve", "run the loopback dashboard in the foreground"),
                       ("start", "run the dashboard in the background"),
                       ("open", "start the dashboard if needed and open it in a kilix browse tab")):
        action = board_actions.add_parser(name, help=text)
        action.add_argument("targets", nargs="*", default=list(DEFAULT_TARGETS))
        action.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT)
        action.add_argument("--low", type=float, default=10.0, metavar="PERCENT")
    board_actions.add_parser("stop", help="stop the background dashboard")
    board_actions.add_parser("status", help="report whether the dashboard is running")
    provider = board_actions.add_parser("provider", help="open official usage pages in kilix browse tabs")
    provider.add_argument("which", nargs="?", choices=(*LIMIT_PROVIDERS, "all"), default="all")
    return result


def utf8_streams():
    # JSON is printed with ensure_ascii=False; never fail on a non-UTF-8 locale.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def main(argv=None):
    utf8_streams()
    args = parser().parse_args(argv)
    os.umask(0o077)
    config_path = args.config.expanduser().resolve()
    runtime = config_path.parent / ".loop"
    client = Kilix()
    try:
        if args.command == "init":
            value = initialize(config_path, runtime)
        elif args.command == "loop":
            if args.action == "start":
                value = start(config_path, client, runtime)
            elif args.action == "stop":
                value = stop_loop(runtime)
            elif args.action == "status":
                value = read_state(runtime)
                value["config_file"] = str(config_path)
            elif args.action in {"run", "once"}:
                value = run_loop(config_path, client, runtime, args.action == "once", getattr(args, "allow_busy", False))
            else:
                with exclusive(runtime / "config.lock"):
                    config = load_config(config_path)
                    if args.action in {"pause", "resume"}:
                        config["enabled"] = args.action == "resume"
                    elif args.action == "every":
                        if not math.isfinite(args.minutes) or args.minutes < MIN_INTERVAL_MINUTES:
                            raise LoopError(f"minutes must be finite and at least {MIN_INTERVAL_MINUTES}")
                        config["interval_minutes"] = args.minutes
                    elif args.action == "prompt":
                        if args.file and args.text is not None:
                            raise LoopError("use prompt TEXT or prompt --file, not both")
                        if args.file:
                            content = read_stdin() if str(args.file) == "-" else read_utf8(args.file)
                        else:
                            content = args.text
                        path = prompt_path(config_path, config)
                        if content is None:
                            print(read_utf8(path), end="")
                            return 0
                        atomic_write(path, content)
                        print(f"Updated {path}")
                        return 0
                    write_json(config_path, config)
                    value = config
        elif args.command == "panes":
            value = client.call("list", "--json")
            if not args.all:
                value["panes"] = [pane for pane in value["panes"]
                                  if pane.get("title") in ALIASES.values()
                                  or pane.get("cwd") == str(config_path.parent)]
            if not args.json:
                print(f"{'PANE':<7} {'TITLE':<26} {'ACTIVITY':<12} AGENT")
                for pane in value["panes"]:
                    provider = pane_agent(pane)[0] or "-"
                    print(f"{pane['pane_id']:<7} {pane.get('title', ''):<26} "
                          f"{pane.get('activity', ''):<12} {provider}")
                return 0
        elif args.command in {"read", "monitor"}:
            if args.watch is not None and (not math.isfinite(args.watch) or args.watch <= 0):
                raise LoopError("watch seconds must be finite and greater than zero")
            if args.lines <= 0:
                raise LoopError("lines must be greater than zero")
            while True:
                snapshot = client.dump(args.target, args.lines)
                print(snapshot["text"], flush=True)
                if args.watch is None:
                    return 0
                time.sleep(args.watch)
        elif args.command == "idle":
            snapshot = client.dump(args.target, ansi=True)
            value = {"pane_id": snapshot["pane"]["pane_id"],
                     **assess(snapshot["pane"], snapshot["ansi_text"])}
        elif args.command == "send":
            value = deliver(client, args.target, args.text if args.text is not None else read_stdin(),
                            runtime, allow_busy=args.allow_busy, allow_limited=args.allow_limited)
        elif args.command == "verify":
            value = verify_last_send(client, runtime)
        elif args.command == "limits":
            if not math.isfinite(args.low) or not 0 <= args.low <= 100:
                raise LoopError("--low must be a number from 0 to 100")
            reports = []
            history = CodexHistory().refresh() if args.history else None
            for target in args.targets:
                snapshot = client.dump(target)
                assessed = (limit_report(snapshot["pane"], snapshot["text"], low_percent=args.low, history=history)
                            if args.history else
                            assess_limits(snapshot["pane"], snapshot["text"], low_percent=args.low))
                reports.append({"target": target, "pane_id": snapshot["pane"].get("pane_id"), **assessed})
            worst = next((state for state in ("exhausted", "low", "ok")
                          if any(r["state"] == state for r in reports)), "unknown")
            value = {"status": worst, "panes": reports}
        elif args.command == "dashboard":
            if args.action in ("serve", "start", "open"):
                if not 0 <= args.port <= 65535:
                    raise LoopError("--port must be from 0 to 65535")
                if not math.isfinite(args.low) or not 0 <= args.low <= 100:
                    raise LoopError("--low must be a number from 0 to 100")
            if args.action == "serve":
                value = serve_dashboard(runtime, client, args.port, args.targets, args.low)
            elif args.action == "start":
                value = start_dashboard(config_path, runtime, args.port, args.targets, args.low)
            elif args.action == "stop":
                value = stop_dashboard(runtime)
            elif args.action == "status":
                value = dashboard_state(runtime)
            elif args.action == "open":
                state = start_dashboard(config_path, runtime, args.port, args.targets, args.low)
                value = {"dashboard": state, "browser": launch_browser(state["url"], "claudex-limits")}
            else:
                names = list(LIMIT_PROVIDERS) if args.which == "all" else [args.which]
                value = {"opened": [launch_browser(dashboard.PROVIDER_PAGES[name], f"{name}-usage")
                                    for name in names]}
        print(json.dumps(value, indent=2, ensure_ascii=False))
        return (2 if value.get("status") in ("skipped", "exhausted") or value.get("ready") is False
                else 0)
    except Busy as error:
        print(json.dumps({"status": "skipped", "reason": str(error), "text_attempted": False,
                          "text_may_remain": False}))
        return 2
    except (LoopError, OSError, ValueError) as error:
        value = getattr(error, "evidence", None) or {"status": "error", "reason": str(error),
                                                    "text_attempted": False, "text_may_remain": False}
        print(json.dumps(value, ensure_ascii=False))
        print(f"claudex: {error}", file=sys.stderr)
        return 3 if value.get("text_attempted") else 1
    except Exception as error:  # a caller must always get one JSON record
        value = getattr(error, "evidence", None) or {
            "status": "error", "reason": f"{type(error).__name__}: {error}",
            "text_attempted": False, "text_may_remain": False}
        print(json.dumps(value, ensure_ascii=False))
        print(f"claudex: unexpected {type(error).__name__}: {error}", file=sys.stderr)
        return 3 if value.get("text_attempted") else 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
