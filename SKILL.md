---
name: live-reporting
description: Live idea session. Claude listens to the user's microphone and watches their screen while they explain an idea out loud (talking while sketching in Paint, a whiteboard app, a doc, code), asks clarifying questions in a small on-screen window, and builds an artifact that mirrors its understanding as they talk. Use when the user says "live report", "live session", "let me walk you through this", "listen while I explain", "I'll draw it out", or invokes /live-reporting.
---

# Live reporting

The user is pitching an idea to you the way they would to a senior colleague or a manager: talking, sketching,
pointing at things. Your job is to draw the whole idea out of them and to prove you understood it by building an
artifact that reflects it, live, while they talk. They correct the artifact out loud; you ask what is missing.

A capture process on their machine does the listening and watching: it transcribes their speech locally
(faster-whisper, nothing leaves the machine), saves a screenshot whenever the screen changes and settles, and
shows your messages in a small always-on-top window (hidden from the screenshots). You read its events through
the Monitor tool and talk back with `live.py say`.

`SKILL_DIR` below is this skill's base directory (shown when the skill loads). Use `python` on Windows,
`python3` elsewhere. `live.py` itself needs only the standard library.

## Start

1. **Check the install** once per machine: `python "SKILL_DIR/scripts/live.py" doctor`. It lists microphones
   and screens. If the venv is missing, tell the user to run `install.ps1` (Windows) or `install.sh` from the
   skill's repo, and stop.
2. **Start the capture**:
   `python "SKILL_DIR/scripts/live.py" start --topic "<topic>" --prompt "<names and jargon they will say>"`
   - `--prompt` helps the transcriber spell names right: project, product and module names from the user's
     message or the current repo (e.g. `"Importer, Billing service, Postgres, webhook"`). Keep it short.
   - The default screen is the primary one. If they say the drawing is on another screen, pass
     `--monitor N` using the numbers `doctor` printed (0 = all screens). `--no-screen` for voice only.
   - It prints the session folder and `ready: listening` once the model is loaded (first run downloads it).
3. **Open the artifact immediately**, before they get far: follow the Artifact tool's rules (quickstart, then
   publish a page from a file in your scratchpad). Start it as a skeleton with these sections and fill them as
   they talk:
   - **The idea in one line**
   - **The pieces**: each component, what it owns, a diagram of how they connect (inline SVG or a simple
     boxes-and-arrows layout; redraw it as the picture changes)
   - **How they work together**: flows, hand-offs, who decides what, step by step
   - **Decisions and the reasons for them**: the "why" is the point of the session; record it in their words
   - **Assumed, please confirm**: things you inferred but they never said
   - **Open questions**
   - **Their words**: a glossary of the terms they use, so you use the same ones
   Then send the link to the window: `live.py say --kind link "<artifact url>"`.
4. **Arm the Monitor** on the event stream:
   `Monitor(command: 'python "SKILL_DIR/scripts/live.py" watch', description: "live session: voice + screen",
   timeout_ms: 1800000)`. When it expires while the session is still running, arm it again: `watch` resumes
   where it stopped, nothing repeats or gets lost.
5. Say hello in the window, one line: `live.py say --kind note "Listening. Talk me through it; I'll ask as we go."`

## Each event

Events arrive as lines like:

```
[12 speech 14:03:22] "so the importer is its own service, separate from billing" | screen: C:\...\0007.jpg (NEW)
[13 screen 14:03:40] C:\...\0008.jpg (screen changed while you were quiet)
[14 end 14:20:02] session over: user pressed Done
```

- **Speech** is machine transcribed: fix obvious mishearings from context and the screen, silently. Never quote
  a mishearing back at them.
- **Screen (NEW)**: Read the image; it is what they are drawing or pointing at. Skip `(unchanged)` frames, you
  have already seen them. A `screen` event with no speech means they are drawing quietly: look, but usually stay
  quiet until they talk.
- Then pick one:
  - **Mid-thought** (sentence trails off, they are clearly continuing): do nothing yet.
  - **A new piece understood**: update your model. For a significant piece, confirm it in one short line:
    `say --kind heard "Importer and Billing stay separate services; Billing listens for import events."`
  - **A gap that matters**: ask it (rules below).
  - **A correction** ("no, that's not it"): fix the artifact first, then `say --kind heard` the corrected version.
- **Republish the artifact** when your understanding changed in a way they would notice, typically every one to
  three significant pieces, and always after a correction or an answered question. Not on every utterance. Use
  the Artifact tool's `edits` for small changes so publishing stays fast.

## Asking questions

You are the experienced listener who makes the idea better by asking the right thing at the right moment.

- **One open question at a time.** Wait for the answer (their next speech that addresses it) before the next.
  If they move on without answering, park it under Open questions and ask again at a natural pause.
- **Don't ask what they are about to explain.** If they are mid-flow on that very point, wait.
- **Short**: it must read at a glance in a small window while they draw. 25 words at most. Offer an either/or
  when you can: "When an import fails halfway, does Billing see nothing, or the partial rows?"
- **Ask for reasons, boundaries and failure cases**, not trivia: why is it split this way, who owns this state,
  what happens when X fails or is interrupted, what triggers the hand-off, what does "done" look like, what
  must never happen, what is out of scope.
- When an answer settles a decision, write the decision and its reason into the artifact.
- A gap you can fill with a reasonable assumption goes under **Assumed, please confirm** instead of a question.

## Keep it live

- The user is looking at their drawing and the small window, not the terminal. Everything meant for them goes
  through `live.py say`. Keep terminal output to a minimum during the session.
- Stay fast: few tool calls per event, no long investigations. Reading code in the repo is fine when they point
  you at it, but don't build or edit code during the session unless they ask; the artifact is the thing you build.
- Voice commands: "pause" / "hold on" means stop asking until they continue; "what have you got?" means
  `say` a three-line summary plus the link; "we're done" / "that's it" means end the session.
- Never put secrets you see on screen (keys, passwords, tokens, personal data) into the artifact or the window.
  The artifact describes the idea, not the screenshots.

## End

When an `end` event arrives or they say they are done:

1. `python "SKILL_DIR/scripts/live.py" stop` (harmless if already stopped) and stop the Monitor (TaskStop).
2. Final artifact pass: the idea in one line, the pieces and how they work together, every decision with its
   reason, what is still open, and a proposed next step (usually a build plan in the order you would build it).
   Remove the "Assumed" items they confirmed, keep the rest clearly marked.
3. Reply in the terminal: a short recap, the artifact link, the transcript path
   (`<session>/transcript.md`, local only), and offer to start building from it.

## Files

Session data stays on the user's machine in the session folder `start` printed: `events.jsonl`,
`transcript.md` (their speech plus your messages), `screens/`. Audio is never saved. `live.py status`,
`live.py transcript` and `live.py latest` read the newest session.
