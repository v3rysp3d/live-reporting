"""The fast listener: a fast model (Haiku by default) reacts to every utterance and screenshot within a second or two.

It runs one persistent `claude -p` process in stream-json mode (the user's own Claude Code login, no API key,
no tools, no MCP), so each turn costs only the model's own latency. Per turn it may:

  - put a short confirmation or question in the live window (outbox.jsonl, by "fast")
  - record notes about the idea (notes.json): pieces, flows, decisions with reasons, assumptions, open questions
  - escalate something hard to the main Claude Code session (an `escalate` event)

The main session is woken only by `escalate`, by throttled `digest` events (notes worth a refresh of the
artifact) and by `end`. Messages the main session sends to the window are fed back into this conversation so
both stay in step.
"""
from __future__ import annotations

import base64
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from common import EventLog, now_iso

NOTE_KINDS = ("idea", "piece", "flow", "decision", "assumed", "open", "term")

SYSTEM = """You are the live ears of a senior engineer. A person is explaining an idea out loud, often while sketching
on screen, the way they would pitch it to an experienced colleague or manager. You receive their speech
(machine transcribed, so fix obvious mishearings silently) and screenshots of what they are drawing or showing.
Your job: confirm what you understood in a few words, ask the one question that matters most at the right
moment, and record the idea as notes. A stronger model builds the full write-up from your notes and handles
anything hard you escalate.

Answer every message with lines in exactly this format and nothing else:

SAY heard: <one short sentence: a significant piece you just understood, in their words>
SAY question: <one question, 25 words at most, either/or when possible>
NOTE <kind>: <one fact about the idea>
DROP <id>: <why a note is now wrong>
ESCALATE: <what the stronger model should decide or answer, with the context it needs>
END
-

Rules:
- If they are mid-thought (sentence trails off, clearly continuing), or nothing new was said, answer just "-".
- At most one SAY per message, and only for something worth interrupting for. Prefer silence over chatter:
  never confirm an intro or a restatement of the topic ("I want to explain X"), only real content, and not every
  sentence: confirm a piece once it is complete.
- One open question at a time. Ask again only after they answered or moved on; don't ask what they are about
  to explain. Ask for reasons, boundaries, ownership of state, triggers, failure cases, what "done" looks like.
  No background trivia (new vs. existing, who it is for) unless it changes the design.
- NOTE kinds: idea (the one-line idea), piece (a component and what it owns), flow (how pieces interact, step by
  step), decision (a choice AND their reason for it), assumed (your inference they never said), open (an
  unanswered question), term (their vocabulary, "word = meaning"). Write notes in their words. Note every new
  fact, even when you say nothing.
- When they correct something, DROP the wrong note by its id and NOTE the right version, then SAY heard the fix.
- ESCALATE when they ask for your opinion or a recommendation, compare designs, ask something that needs deep
  technical knowledge or their codebase, contradict themselves in a way you can't resolve, ask to change how the
  write-up is structured, or say "think hard" / "ask the big model". When you escalate, SAY exactly
  "SAY heard: Thinking about that one." and don't answer it yourself; the main model's answer is shown to them. The stronger model's answer comes back to you as [main model said ...].
- END only when they clearly say they are finished ("we're done", "that's it, wrap up").
- Never repeat secrets you see on screen (keys, passwords, tokens, personal data)."""

DONE = re.compile(r"\b(we're done|we are done|i'm done|that's it|that is it|that's all|wrap (it )?up|finished|"
                  r"(stop|end) (the )?(session|recording))\b", re.I)
LINE = re.compile(r"^(SAY (heard|question)|NOTE (\w+)|DROP #?(\d+)|ESCALATE|END)\s*:?\s*(.*)$")


def find_claude() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    for p in (Path.home() / ".local" / "bin" / "claude.exe", Path.home() / ".local" / "bin" / "claude",
              Path.home() / ".claude" / "local" / "claude"):
        if p.exists():
            return str(p)
    return None


def parse_reply(text: str) -> list[tuple]:
    """Turn the fast model's reply into actions: ('say', kind, text) ('note', kind, text) ('drop', id, why)
    ('escalate', text) ('end',). Unknown lines are ignored."""
    out = []
    for raw in text.splitlines():
        m = LINE.match(raw.strip())
        if not m:
            continue
        head, say_kind, note_kind, drop_id, rest = m.groups()
        rest = rest.strip()
        if say_kind and rest:
            out.append(("say", say_kind, rest))
        elif note_kind:
            kind = note_kind.lower()
            if kind in NOTE_KINDS and rest:
                out.append(("note", kind, rest))
        elif drop_id:
            out.append(("drop", int(drop_id), rest))
        elif head == "ESCALATE" and rest:
            out.append(("escalate", rest))
        elif head == "END":
            out.append(("end",))
    return out


class Notes:
    """notes.json: the running record of the idea, rewritten atomically on every change."""

    def __init__(self, path: Path, topic: str):
        self.path = path
        self.topic = topic
        self.items: list[dict] = []
        self.next_id = 1
        self.lock = threading.Lock()

    def add(self, kind: str, text: str) -> int:
        with self.lock:
            nid = self.next_id
            self.next_id += 1
            self.items.append({"id": nid, "kind": kind, "text": text, "time": now_iso()})
            self._save()
            return nid

    def drop(self, nid: int) -> bool:
        with self.lock:
            before = len(self.items)
            self.items = [i for i in self.items if i["id"] != nid]
            self._save()
            return len(self.items) != before

    def render(self) -> str:
        with self.lock:
            return "\n".join(f"#{i['id']} {i['kind']}: {i['text']}" for i in self.items) or "(none yet)"

    def _save(self) -> None:
        data = {"topic": self.topic, "updated": now_iso(), "items": self.items}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


class FastListener:
    def __init__(self, session: Path, log: EventLog, topic: str, model: str = "haiku",
                 digest_every: float = 90.0, digest_notes: int = 4, rotate_turns: int = 60,
                 question_gap: float = 15.0, on_end=None):
        self.question_gap = question_gap
        self.last_question = 0.0
        self.session = session
        self.log = log
        self.topic = topic
        self.model = model
        self.claude = find_claude()
        self.notes = Notes(session / "notes.json", topic)
        self.digest_every = digest_every
        self.digest_notes = digest_notes
        self.rotate_turns = rotate_turns
        self.on_end = on_end
        self.inbox: "queue.Queue" = queue.Queue()
        self.proc: subprocess.Popen | None = None
        self.replies: "queue.Queue" = queue.Queue()
        self.turns = 0
        self.recent: list[str] = []
        self.pending_ids: list[int] = []
        self.new_notes = 0
        self.correction = False
        self.last_digest = time.time()
        self.outbox = session / "outbox.jsonl"
        self.outbox_seen = 0
        self.stopping = threading.Event()
        self.busy = False
        self.failed = False

    @property
    def available(self) -> bool:
        return self.claude is not None

    # ------------------------------------------------------------ process

    def _spawn(self, seed: str) -> None:
        cmd = [self.claude, "-p", "--model", self.model, "--input-format", "stream-json",
               "--output-format", "stream-json", "--verbose", "--tools", "", "--strict-mcp-config",
               "--setting-sources", "", "--disable-slash-commands", "--no-session-persistence",
               "--system-prompt", SYSTEM]
        work = self.session / "fast-cwd"  # an empty folder: no project files or CLAUDE.md to pick up
        work.mkdir(exist_ok=True)
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=(self.session / "fast.log").open("a", encoding="utf-8"),
                                     cwd=work, text=True, encoding="utf-8", bufsize=1, creationflags=flags)
        proc = self.proc
        threading.Thread(target=self._read, args=(proc,), daemon=True).start()
        self.turns = 0
        self._send([{"type": "text", "text": seed}], wait=True)

    def _read(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "result":
                self.replies.put(ev.get("result") or "" if not ev.get("is_error") else f"__error__ {ev.get('result')}")
        self.replies.put(None)  # process ended

    def _send(self, content: list[dict], wait: bool = True) -> str | None:
        msg = {"type": "user", "message": {"role": "user", "content": content}}
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            return None
        self.turns += 1
        if not wait:
            return ""
        try:
            return self.replies.get(timeout=60)
        except queue.Empty:
            return None

    def _seed(self) -> str:
        parts = [f"Session topic: {self.topic or '(not given; work it out from what they say)'}",
                 "Notes so far:\n" + self.notes.render()]
        if self.recent:
            parts.append("Their most recent words:\n" + "\n".join(self.recent[-8:]))
        parts.append("Reply \"-\" to this message; the live session continues with the next message.")
        return "\n\n".join(parts)

    # ------------------------------------------------------------ public

    def start(self) -> None:
        if not self.available:
            return
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, ev: dict) -> None:
        self.inbox.put(ev)

    def stop(self) -> None:
        self.stopping.set()
        self.inbox.put(None)
        if self.proc:
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    # ------------------------------------------------------------ loop

    def _loop(self) -> None:
        try:
            self._spawn(self._seed())
        except Exception as e:
            self.log.write({"type": "status", "fast": False,
                            "text": f"fast listener failed to start ({e}); the main session gets every utterance"})
            self.failed = True
            return
        self.log.write({"type": "status", "text": f"fast listener ready: {self.model} via claude CLI", "fast": True})
        while not self.stopping.is_set():
            try:
                ev = self.inbox.get(timeout=5)
            except queue.Empty:
                self._maybe_digest()  # notes that came in before a silence still reach the main session
                continue
            if ev is None:
                break
            self.busy = True
            batch = [ev]
            while True:  # whatever piled up while the last turn ran goes in as one turn, so it never falls behind
                try:
                    more = self.inbox.get_nowait()
                except queue.Empty:
                    break
                if more is None:
                    self.stopping.set()
                    break
                batch.append(more)
            try:
                self._turn(batch)
                self._maybe_digest()
            finally:
                self.busy = False

    def _turn(self, batch: list[dict]) -> None:
        content: list[dict] = []
        lines: list[str] = []
        main_said = self._main_messages()
        for m in main_said:
            lines.append(f"[main model said to them ({m.get('kind')})]: {m.get('text')}")
        if self.pending_ids:
            lines.append("(your last notes were saved as " + " ".join(f"#{i}" for i in self.pending_ids) + ")")
            self.pending_ids = []
        image = None
        for ev in batch:
            if ev["type"] == "speech":
                lines.append(f"[they said] {ev['text']}")
                self.recent.append(ev["text"])
                if ev.get("screen_new") and ev.get("screen"):
                    image = ev["screen"]
            elif ev["type"] == "screen":
                lines.append("[their screen changed while they were quiet]")
                image = ev.get("screen") or image
        self.recent = self.recent[-20:]
        if image:
            try:
                data = base64.b64encode(Path(image).read_bytes()).decode()
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}})
                lines.append("(screenshot attached: their screen now)")
            except OSError:
                pass
        content.append({"type": "text", "text": "\n".join(lines)})

        if self.turns >= self.rotate_turns:
            self._restart()
        started = time.time()
        reply = self._send(content)
        if reply is None or reply.startswith("__error__"):
            self.log.write({"type": "error", "text": f"fast listener: {reply or 'no reply'}; restarting it"})
            self._restart()
            return
        said = " ".join(ev.get("text", "") for ev in batch if ev["type"] == "speech")
        self._apply(parse_reply(reply), round(time.time() - started, 1), said)

    def _restart(self) -> None:
        try:
            if self.proc:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
        except Exception:
            pass
        while not self.replies.empty():
            self.replies.get_nowait()
        self._spawn(self._seed())

    def _apply(self, actions: list[tuple], lag: float, said: str = "") -> None:
        spoken: set[str] = set()
        for a in actions:
            if a[0] == "say":
                if a[1] in spoken:  # one confirmation and one question per turn at most
                    continue
                if a[1] == "question" and time.time() - self.last_question < self.question_gap:
                    # too soon after the last one: keep it for the write-up instead of piling questions on them
                    self.pending_ids.append(self.notes.add("open", a[2]))
                    self.new_notes += 1
                    continue
                spoken.add(a[1])
                if a[1] == "question":
                    self.last_question = time.time()
                self._say(a[1], a[2])
            elif a[0] == "note":
                self.pending_ids.append(self.notes.add(a[1], a[2]))
                self.new_notes += 1
            elif a[0] == "drop":
                if self.notes.drop(a[1]):
                    self.new_notes += 1
                    self.correction = True
            elif a[0] == "escalate":
                self.log.write({"type": "escalate", "text": a[1], "notes": str(self.notes.path),
                                "recent": self.recent[-6:]})
            elif a[0] == "end":
                # The model's END is only a hint: ending early would lose the user's next words, so their own
                # words in this turn must say they are done.
                if not DONE.search(said):
                    continue
                self.log.write({"type": "status", "text": "they said they are done"})
                if self.on_end:
                    self.on_end()
        if actions:
            self.log.write({"type": "fast", "lag": lag, "actions": [a[0] for a in actions]})

    def _say(self, kind: str, text: str) -> None:
        with self.outbox.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": now_iso(), "kind": kind, "text": text, "by": "fast"}, ensure_ascii=False) + "\n")
        with (self.session / "transcript.md").open("a", encoding="utf-8") as f:
            f.write(f"> **Claude ({kind})** {text}\n\n")

    def _main_messages(self) -> list[dict]:
        """Messages the main session put in the window since we last looked."""
        if not self.outbox.exists():
            return []
        with self.outbox.open("r", encoding="utf-8") as f:
            f.seek(self.outbox_seen)
            new = f.read()
            self.outbox_seen = f.tell()
        out = []
        for line in new.splitlines():
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if m.get("by") != "fast":
                out.append(m)
        return out

    def _maybe_digest(self, force: bool = False) -> None:
        since = time.time() - self.last_digest
        due = (self.new_notes >= self.digest_notes and since >= self.digest_every) or \
              (self.correction and since >= 20) or (force and self.new_notes)
        if not due:
            return
        self.log.write({"type": "digest", "text": f"{self.new_notes} note changes", "notes": str(self.notes.path)})
        self.new_notes = 0
        self.correction = False
        self.last_digest = time.time()

    def finish(self, timeout: float = 20.0) -> None:
        """Let queued utterances get their turn, send a last digest, then close the process."""
        end = time.time() + timeout
        while not self.failed and (not self.inbox.empty() or self.busy) and time.time() < end:
            time.sleep(0.2)
        self._maybe_digest(force=True)
        self.stop()
