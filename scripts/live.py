"""live-reporting control: start / watch / say / stop a live session.

  live.py doctor                      check the install, list microphones and screens
  live.py start [--topic T] [capture options...]   start capturing; prints the session folder
  live.py watch [--session S]         stream new events, one line each (made for Claude Code's Monitor)
  live.py say [--kind K] TEXT         show a message in the live window (question | heard | note | link)
  live.py stop                        end the session
  live.py status | latest | transcript

--session defaults to the newest session. Standard library only; the capture itself runs in the venv.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    capture_running, data_root, now_iso, read_events, sessions_root, venv_python, wait_until,
)

HERE = Path(__file__).resolve().parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def latest_session() -> Path | None:
    root = sessions_root()
    if not root.is_dir():
        return None
    dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name)
    return dirs[-1] if dirs else None


def resolve_session(arg: str | None) -> Path:
    s = Path(arg) if arg else latest_session()
    if s is None or not s.is_dir():
        sys.exit("no live-reporting session found; run `live.py start` first")
    return s


def fmt(ev: dict) -> str:
    """One line per event: what Claude reads in each Monitor notification."""
    head = f"[{ev.get('seq')} {ev.get('type')} {str(ev.get('time', ''))[11:19]}]"
    t = ev.get("type")
    if t == "speech":
        line = f'{head} "{ev.get("text", "")}"'
        if "screen" in ev:
            line += f" | screen: {ev['screen']}" + (" (NEW)" if ev.get("screen_new") else " (unchanged)")
        return line
    if t == "screen":
        return f"{head} {ev.get('screen')} ({ev.get('note', 'changed')})"
    if t == "end":
        return f"{head} session over: {ev.get('reason', '')}"
    return f"{head} {ev.get('text', '')}"


# ---------------------------------------------------------------- commands

def cmd_doctor(_a) -> int:
    py = venv_python()
    print(f"data folder : {data_root()}")
    print(f"venv python : {py} ({'found' if py.exists() else 'MISSING: run install.ps1 / install.sh'})")
    if not py.exists():
        return 1
    probe = (
        "import sounddevice as sd, mss, ctranslate2, faster_whisper\n"
        "print('whisper     :', faster_whisper.__version__, '| CUDA devices:', ctranslate2.get_cuda_device_count())\n"
        "d = sd.query_devices(kind='input'); print('default mic :', d['name'])\n"
        "print('microphones :')\n"
        "[print(f'  {i}: {x[\"name\"]}') for i, x in enumerate(sd.query_devices()) if x['max_input_channels'] > 0 and x['hostapi'] == 0]\n"
        "m = mss.mss().monitors\n"
        "print('screens     :'); [print(f'  {i}: {x[\"width\"]}x{x[\"height\"]} at {x[\"left\"]},{x[\"top\"]}' + (' (all)' if i == 0 else '')) for i, x in enumerate(m)]\n"
    )
    r = subprocess.run([str(py), "-c", probe], capture_output=True, text=True, encoding="utf-8", **no_window())
    print(r.stdout, end="")
    if r.returncode:
        print(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else f"probe failed ({r.returncode})")
    return r.returncode


def no_window() -> dict:
    return {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW


def cmd_start(a, extra: list[str]) -> int:
    running = latest_session()
    if running and capture_running(running):
        print(f"already running: {running}")
        return 0
    py = venv_python(windowed=True)
    if not py.exists():
        sys.exit(f"capture environment missing ({py}); run install.ps1 or install.sh from the repo")
    session = sessions_root() / datetime.now().strftime("%Y%m%d-%H%M%S")
    session.mkdir(parents=True)
    (session / "meta.json").write_text(json.dumps({"topic": a.topic, "started": now_iso()}, indent=2),
                                       encoding="utf-8")
    log = (session / "capture.log").open("w", encoding="utf-8")
    cmd = [str(py), str(HERE / "capture.py"), "--session", str(session), *extra]
    if os.name == "nt":
        flags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
    else:
        subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    print(f"session: {session}")

    def settled():
        evs = read_events(session / "events.jsonl")
        return any(e["type"] in ("end",) or (e["type"] == "status" and e["text"].startswith("ready")) for e in evs)

    ok = wait_until(settled, a.wait)
    for ev in read_events(session / "events.jsonl"):
        print(fmt(ev))
    if not ok:
        print(f"still starting after {a.wait:.0f} s (first run downloads the speech model); "
              f"see {session / 'capture.log'}")
    return 0


def cmd_watch(a) -> int:
    session = resolve_session(a.session)
    events = session / "events.jsonl"
    cursor_file = session / ".watch_cursor"
    try:
        cursor = 0 if a.from_start else int(cursor_file.read_text())
    except (OSError, ValueError):
        cursor = 0
    dead_since = None
    while True:
        for ev in read_events(events, cursor):
            print(fmt(ev), flush=True)
            cursor = ev["seq"]
            cursor_file.write_text(str(cursor))
            if ev["type"] == "end":
                return 0
        if capture_running(session):
            dead_since = None
        else:
            dead_since = dead_since or time.time()
            if time.time() - dead_since > 3:
                print(f"[capture stopped without an end event; see {session / 'capture.log'}]", flush=True)
                return 1
        time.sleep(0.3)


def cmd_say(a) -> int:
    session = resolve_session(a.session)
    text = " ".join(a.text).strip()
    if not text:
        sys.exit("nothing to say")
    with (session / "outbox.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"time": now_iso(), "kind": a.kind, "text": text}, ensure_ascii=False) + "\n")
    with (session / "transcript.md").open("a", encoding="utf-8") as f:
        f.write(f"> **Claude ({a.kind})** {text}\n\n")
    return 0


def cmd_stop(a) -> int:
    session = resolve_session(a.session)
    if not capture_running(session):
        print(f"not running: {session}")
        return 0
    (session / "stop").touch()
    stopped = wait_until(lambda: not capture_running(session), 90)
    print(f"{'stopped' if stopped else 'stop requested (still finishing)'}: {session}")
    return 0


def cmd_status(a) -> int:
    session = resolve_session(a.session)
    evs = read_events(session / "events.jsonl")
    speech = [e for e in evs if e["type"] == "speech"]
    shots = list((session / "screens").glob("*.jpg")) if (session / "screens").is_dir() else []
    print(f"session   : {session}")
    print(f"running   : {capture_running(session)}")
    print(f"utterances: {len(speech)}   screenshots: {len(shots)}")
    for e in evs[-5:]:
        print(fmt(e))
    return 0


def cmd_transcript(a) -> int:
    session = resolve_session(a.session)
    print((session / "transcript.md").read_text(encoding="utf-8"))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    s = sub.add_parser("start", help="extra options pass through to capture.py (--monitor, --mic, --prompt...)")
    s.add_argument("--topic", default="")
    s.add_argument("--wait", type=float, default=240.0, help="seconds to wait for the capture to be ready")
    for name in ("watch", "stop", "status", "transcript"):
        sp = sub.add_parser(name)
        sp.add_argument("--session")
        if name == "watch":
            sp.add_argument("--from-start", action="store_true")
    sub.add_parser("latest")
    sy = sub.add_parser("say")
    sy.add_argument("--session")
    sy.add_argument("--kind", default="note", choices=["question", "heard", "note", "link"])
    sy.add_argument("text", nargs="+")

    a, extra = p.parse_known_args(argv)
    if extra and a.cmd != "start":
        p.error(f"unrecognized arguments: {' '.join(extra)}")
    if a.cmd == "start":
        return cmd_start(a, extra)
    if a.cmd == "latest":
        s = latest_session()
        print(s or "")
        return 0 if s else 1
    return {"doctor": cmd_doctor, "watch": cmd_watch, "say": cmd_say, "stop": cmd_stop,
            "status": cmd_status, "transcript": cmd_transcript}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
