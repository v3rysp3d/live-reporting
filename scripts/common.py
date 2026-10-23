"""Shared helpers. Standard library only: live.py runs on whatever Python Claude finds first."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path


def data_root() -> Path:
    """Where the venv and sessions live: never inside the repo."""
    env = os.environ.get("LIVE_REPORTING_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "live-reporting"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "live-reporting"


def sessions_root() -> Path:
    return data_root() / "sessions"


def venv_python(windowed: bool = False) -> Path:
    env = os.environ.get("LIVE_REPORTING_PY")
    if env:
        return Path(env)
    venv = data_root() / "venv"
    if os.name == "nt":
        return venv / "Scripts" / ("pythonw.exe" if windowed else "python.exe")
    return venv / "bin" / "python"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class EventLog:
    """Append-only JSONL with a sequence number per event; safe across threads of one process."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.seq = 0
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    self.seq = max(self.seq, int(json.loads(line).get("seq", 0)))
                except (json.JSONDecodeError, ValueError):
                    continue

    def write(self, event: dict) -> dict:
        with self.lock:
            self.seq += 1
            ev = {"seq": self.seq, "time": now_iso(), **event}
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            return ev


def read_events(path: Path, after: int = 0) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue  # a line still being written
        if ev.get("seq", 0) > after:
            out.append(ev)
    return out


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def capture_running(session: Path) -> bool:
    pid_file = session / "capture.pid"
    try:
        return pid_alive(int(pid_file.read_text().strip()))
    except (OSError, ValueError):
        return False


def wait_until(pred, timeout: float, step: float = 0.25) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()
