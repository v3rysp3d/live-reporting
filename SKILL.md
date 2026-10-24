---
name: live-reporting
description: Live idea session. Claude listens to the user's microphone and watches their screen while they explain an idea out loud (talking while sketching in Paint, a whiteboard app, a doc, code), asks clarifying questions in a small on-screen window, and builds an artifact that mirrors its understanding as they talk. Use when the user says "live report", "live session", "let me walk you through this", "listen while I explain", "I'll draw it out", or invokes /live-reporting.
---

# Live reporting

The user is pitching an idea the way they would to a senior colleague or a manager: talking, sketching,
pointing at things. The session draws the whole idea out of them and proves it was understood by building an
artifact that reflects it, live, while they talk. They correct it out loud; Claude asks what is missing.

Two models share the work:

- **The fast model** (Haiku by default) runs inside the capture process on the user's machine, as one persistent
  `claude -p` process on their own login. It gets every utterance and every new screenshot and answers within
  a second or two: short "heard" confirmations and one question at a time in the live window, plus structured
  notes about the idea (`notes.json`). It escalates anything hard.
- **You** (the main session) are woken only when you are needed: an `escalate` (a judgement call, an opinion, a
  design comparison, deep technical or codebase knowledge, a contradiction), a `digest` (enough new notes to
  refresh the artifact), and the `end`. You own the artifact and the hard answers.

Speech is transcribed locally (faster-whisper; audio never leaves the machine). Screenshots are saved when the
screen changes and settles. The live window is hidden from screenshots.

`SKILL_DIR` below is this skill's base directory (shown when the skill loads). Use `python` on Windows,
`python3` elsewhere. `live.py` itself needs only the standard library.

## Start

1. **Check the install** once per machine: `python "SKILL_DIR/scripts/live.py" doctor`. It lists microphones
   and screens. If the venv is missing, tell the user to run `install.ps1` (Windows) or `install.sh` from the
   skill's repo, and stop.
2. **Start the capture**:
   `python "SKILL_DIR/scripts/live.py" start --topic "<topic>" --prompt "<names and jargon they will say>"`
   - `--topic` goes to the fast model too: make it a clear one-liner from the user's request.
   - `--prompt` helps the transcriber spell names right: project, product and module names from the user's
     message or the current repo (e.g. `"Importer, Billing service, Postgres, webhook"`). Keep it short.
   - The default screen is the primary one. If they say the drawing is on another screen, pass
     `--monitor N` using the numbers `doctor` printed (0 = all screens). `--no-screen` for voice only.
   - `--fast-model sonnet` if they ask for a smarter (slower) fast model; `--no-fast` to handle every
     utterance yourself (see "Without the fast model").
3. **Open the artifact immediately**: follow the Artifact tool's rules (quickstart, then publish a page from a
   file in your scratchpad). Sections, filled as the session goes:
   - **The idea in one line**
   - **The picture**: a diagram of the pieces and how they connect (inline SVG or a simple boxes-and-arrows
     layout); you draw this and redraw it as the picture changes
   - **The pieces**: each component and what it owns
   - **How they work together**: flows, hand-offs, who decides what, step by step
   - **Decisions and the reasons for them**: the "why" is the point of the session; in their words
   - **Assumed, please confirm**: inferred, never said
   - **Open questions**
   - **Their words**: the terms they use
   Then send the link to the window: `live.py say --kind link "<artifact url>"`.
4. **Arm the Monitor**: `Monitor(command: 'python "SKILL_DIR/scripts/live.py" watch', description: "live
   session: escalations and notes", timeout_ms: 1800000)`. When it expires while the session runs, arm it again:
   `watch` resumes where it stopped, nothing repeats or gets lost.

## Events you get

```
[4 status 13:08:48] fast listener ready: haiku via claude CLI
[19 escalate 13:10:02] Should plugins run in-process or sandboxed? | they said: "..." / "..." | notes: C:\...\notes.json
[23 digest 13:11:30] 5 note changes | notes: C:\...\notes.json
[40 end 13:20:02] session over: they said they are done | notes: C:\...\notes.json
```

- **escalate**: answer it. Think it through properly (read their code if they pointed at it), then put the
  answer in the window in 40 words or fewer: `live.py say --kind answer "<answer>"`. Lead with the
  recommendation and the one reason that decides it; offer the trade-off only if it matters. The fast model sees
  your answer and carries on from it. If the answer settles a decision, put it in the artifact.
- **digest**: refresh the artifact from the notes: `live.py notes` prints them (`#id kind: text`; kinds idea,
  piece, flow, decision, assumed, open, term). Rewrite the sections in plain sentences, merge duplicates,
  redraw the picture when pieces or connections changed. One read and one publish (use `edits` for small
  changes); this is not the moment for a long investigation.
- **end**: see End below.
- **error**: if the fast listener died it restarts itself; if the capture died, tell the user and offer to
  restart it.
- Don't ask questions of your own between events: the fast model owns the conversation. Speak only to answer an
  escalation, or when a digest shows a misunderstanding the fast model missed (`say --kind question`).

## End

When the `end` event arrives (they said they're done or pressed Done), or they tell you in the terminal:

1. `python "SKILL_DIR/scripts/live.py" stop` (harmless if already stopped) and stop the Monitor (TaskStop).
2. Final artifact pass from `live.py notes` and `live.py transcript`: the idea in one line, the picture, the
   pieces and how they work together, every decision with its reason, what is still open, and a proposed next
   step (usually a build plan in the order you would build it). Keep unconfirmed assumptions clearly marked.
3. Reply in the terminal: a short recap, the artifact link, the transcript path
   (`<session>/transcript.md`, local only), and offer to start building from it.

## Without the fast model

If the events say `fast listener off` or `failed to start` (no `claude` CLI on PATH, or `--no-fast`), `watch`
gives you every utterance instead (`[12 speech ...] "text" | screen: path (NEW)`), and you do the fast model's
job yourself:

- Speech is machine transcribed: fix mishearings silently. Read `(NEW)` screenshots; skip `(unchanged)`.
- Mid-thought: stay quiet. A significant piece understood: `say --kind heard "<one sentence>"`. A gap that
  matters: one question at a time, 25 words at most, either/or when possible, about reasons, boundaries,
  ownership, triggers and failure cases; never what they are about to explain. A correction: fix the artifact,
  then `say --kind heard` the fixed version.
- Republish the artifact every one to three significant pieces and after every correction.

## Always

- The user is looking at their drawing and the small window, not the terminal: everything meant for them goes
  through `live.py say`. Keep terminal output minimal during the session.
- Don't build or edit code during the session unless they ask; the artifact is the thing being built.
- Never put secrets seen on screen (keys, passwords, tokens, personal data) into the artifact or the window.

## Files

Session data stays on the user's machine in the session folder `start` printed: `events.jsonl`,
`notes.json`, `transcript.md` (their speech plus every message shown to them), `screens/`. Audio is never
saved. `live.py status`, `notes`, `transcript` and `latest` read the newest session; `watch --all` shows every
event.
