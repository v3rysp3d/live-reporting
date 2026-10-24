"""Unit tests for the parts that don't need a microphone, a screen or the speech model."""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from capture import FRAME, SAMPLE_RATE, ScreenWatcher, Segmenter  # noqa: E402
from common import EventLog, read_events  # noqa: E402
import live  # noqa: E402


def frames(seconds: float, level: float, seed: int = 0):
    """Noise at `level`; levels >= 0.05 are 'speech': 210 ms syllables with 90 ms dips, like real talking."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SAMPLE_RATE / FRAME)
    for i in range(n):
        lv = level if level < 0.05 or i % 10 < 7 else 0.003
        yield (rng.standard_normal(FRAME) * lv).astype("float32")


def run(seg: Segmenter, *parts):
    out = []
    for seconds, level in parts:
        for f in frames(seconds, level):
            u = seg.feed(f)
            if u is not None:
                out.append(len(u) / SAMPLE_RATE)
    return out


def test_two_utterances_split_at_a_pause():
    got = run(Segmenter(pause_s=1.0), (1.0, 0.001), (1.5, 0.1), (1.3, 0.001), (2.0, 0.1), (1.3, 0.001))
    assert len(got) == 2
    assert 2.4 < got[0] < 3.0  # speech + the pause that ended it + pre-roll


def test_short_pause_keeps_one_utterance():
    got = run(Segmenter(pause_s=1.0), (1.0, 0.001), (1.5, 0.1), (0.5, 0.001), (1.5, 0.1), (1.3, 0.001))
    assert len(got) == 1


def test_click_is_not_speech():
    got = run(Segmenter(), (1.0, 0.001), (0.12, 0.2), (2.0, 0.001))
    assert got == []


def test_long_speech_is_cut_at_max():
    got = run(Segmenter(max_s=5.0), (0.5, 0.001), (12.0, 0.1))
    assert len(got) == 2 and all(4.5 < g <= 5.1 for g in got)


def test_noise_floor_adapts_to_a_loud_room():
    seg = Segmenter()
    run(seg, (3.0, 0.02))  # steady fan noise above the absolute minimum
    assert run(seg, (2.0, 0.02)) == []
    assert len(run(seg, (1.5, 0.3), (1.3, 0.02))) == 1


def img(color):
    return Image.new("RGB", (320, 180), color)


def test_screen_saves_first_frame_then_only_settled_changes(tmp_path):
    w = ScreenWatcher(tmp_path, monitor=1, settle=2.0)
    assert w.step(img("white"), 0.0)  # first frame always saved
    assert w.step(img("white"), 1.0) is None  # unchanged
    assert w.step(img("black"), 2.0) is None  # moving
    assert w.step(img("black"), 3.0) is None  # not settled yet
    saved = w.step(img("black"), 4.5)
    assert saved and Path(saved).name == "0002.jpg"
    assert w.step(img("black"), 9.0) is None  # nothing new


def test_screen_change_that_reverts_is_not_saved(tmp_path):
    w = ScreenWatcher(tmp_path, monitor=1, settle=2.0)
    w.step(img("white"), 0.0)
    w.step(img("black"), 1.0)
    w.step(img("white"), 2.0)
    assert w.step(img("white"), 5.0) is None


def test_screenshots_are_downscaled(tmp_path):
    w = ScreenWatcher(tmp_path, monitor=1, max_edge=800)
    path = w.step(Image.new("RGB", (3840, 2160), "white"), 0.0)
    assert Image.open(path).size == (800, 450)


def test_event_log_sequence_survives_reopen(tmp_path):
    p = tmp_path / "events.jsonl"
    EventLog(p).write({"type": "status", "text": "a"})
    log = EventLog(p)
    ev = log.write({"type": "speech", "text": "b"})
    assert ev["seq"] == 2
    assert [e["text"] for e in read_events(p, after=1)] == ["b"]


def test_partial_line_is_skipped(tmp_path):
    p = tmp_path / "events.jsonl"
    EventLog(p).write({"type": "status", "text": "a"})
    with p.open("a") as f:
        f.write('{"seq": 2, "type": "spe')
    assert len(read_events(p)) == 1


def test_format_speech_line():
    line = live.fmt({"seq": 3, "type": "speech", "time": "2026-01-01T14:03:22+00:00", "text": "hi",
                     "screen": "s/0001.jpg", "screen_new": True})
    assert line == '[3 speech 14:03:22] "hi" | screen: s/0001.jpg (NEW)'


def cli(*args):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "live.py"), *args],
                          capture_output=True, text=True, encoding="utf-8")


def test_watch_resumes_from_its_cursor_and_ends(tmp_path):
    s = tmp_path / "20260101-000000"
    s.mkdir()
    log = EventLog(s / "events.jsonl")
    log.write({"type": "speech", "text": "first"})
    log.write({"type": "end", "reason": "test"})
    r1 = cli("watch", "--session", str(s))
    assert r1.returncode == 0 and "first" in r1.stdout and "session over" in r1.stdout
    log.write({"type": "speech", "text": "second"})
    log.write({"type": "end", "reason": "again"})
    r2 = cli("watch", "--session", str(s))
    assert "first" not in r2.stdout and "second" in r2.stdout


def test_watch_reports_a_dead_capture(tmp_path):
    s = tmp_path / "20260101-000000"
    s.mkdir()
    EventLog(s / "events.jsonl").write({"type": "status", "text": "loading"})
    r = cli("watch", "--session", str(s))
    assert r.returncode == 1 and "without an end event" in r.stdout


def test_parse_fast_reply():
    from fast import parse_reply
    reply = "\n".join([
        "SAY question: In-process or sandboxed?",
        "NOTE piece: Core owns the document and undo history",
        "NOTE decision: plugins only send commands (so undo always works)",
        "NOTE bogus: ignored kind",
        "DROP #3: plugins may read directly",
        "ESCALATE: they asked for a recommendation on sandboxing",
        "some chatter the model should not have written",
        "END",
    ])
    assert parse_reply(reply) == [
        ("say", "question", "In-process or sandboxed?"),
        ("note", "piece", "Core owns the document and undo history"),
        ("note", "decision", "plugins only send commands (so undo always works)"),
        ("drop", 3, "plugins may read directly"),
        ("escalate", "they asked for a recommendation on sandboxing"),
        ("end",),
    ]
    assert parse_reply("-") == []


def fake_listener(tmp_path):
    from fast import FastListener
    (tmp_path / "transcript.md").write_text("", encoding="utf-8")
    ended = []
    f = FastListener(tmp_path, EventLog(tmp_path / "events.jsonl"), "t", on_end=lambda: ended.append(1))
    return f, ended


def test_end_needs_the_users_own_words(tmp_path):
    f, ended = fake_listener(tmp_path)
    f._apply([("end",)], 1.0, "What do you think, in-process or sandboxed?")
    assert ended == []
    f._apply([("end",)], 1.0, "Okay, that's it. We're done.")
    assert ended == [1]


def test_one_say_of_each_kind_per_turn(tmp_path):
    f, _ = fake_listener(tmp_path)
    f._apply([("say", "heard", "a"), ("say", "question", "q1"), ("say", "heard", "b"), ("say", "question", "q2")], 1.0)
    shown = [json.loads(l)["text"] for l in (tmp_path / "outbox.jsonl").read_text(encoding="utf-8").splitlines()]
    assert shown == ["a", "q1"]


def test_questions_too_close_together_become_open_notes(tmp_path):
    f, _ = fake_listener(tmp_path)
    f._apply([("say", "question", "first?")], 1.0)
    f._apply([("say", "question", "second?")], 1.0)
    shown = [json.loads(l)["text"] for l in (tmp_path / "outbox.jsonl").read_text(encoding="utf-8").splitlines()]
    assert shown == ["first?"] and f.notes.render().endswith("open: second?")


def test_notes_add_drop_and_file(tmp_path):
    from fast import Notes
    n = Notes(tmp_path / "notes.json", "topic")
    a = n.add("piece", "core")
    n.add("flow", "plugins send commands")
    assert n.drop(a) and not n.drop(99)
    data = json.loads((tmp_path / "notes.json").read_text(encoding="utf-8"))
    assert [i["id"] for i in data["items"]] == [2] and n.render() == "#2 flow: plugins send commands"


def test_watch_shows_only_main_events_when_fast_is_on(tmp_path):
    s = tmp_path / "20260101-000000"
    s.mkdir()
    log = EventLog(s / "events.jsonl")
    log.write({"type": "status", "fast": True, "text": "fast listener ready"})
    log.write({"type": "speech", "text": "handled by the fast model"})
    log.write({"type": "escalate", "text": "which design?", "recent": ["a", "b"], "notes": "n.json"})
    log.write({"type": "end", "reason": "done"})
    out = cli("watch", "--session", str(s)).stdout
    assert "handled by the fast model" not in out and "which design?" in out and "session over" in out
    full = cli("watch", "--session", str(s), "--from-start", "--all").stdout
    assert "handled by the fast model" in full


def test_say_writes_outbox_and_transcript(tmp_path):
    s = tmp_path / "20260101-000000"
    s.mkdir()
    (s / "transcript.md").write_text("# t\n\n", encoding="utf-8")
    assert cli("say", "--session", str(s), "--kind", "question", "Why", "split?").returncode == 0
    msg = json.loads((s / "outbox.jsonl").read_text(encoding="utf-8"))
    assert msg["kind"] == "question" and msg["text"] == "Why split?"
    assert "Claude (question)" in (s / "transcript.md").read_text(encoding="utf-8")
