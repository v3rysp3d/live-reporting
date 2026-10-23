# live-reporting

A [Claude Code](https://claude.com/claude-code) skill for explaining an idea to Claude the way you would to a
colleague: talk it through, sketch it in Paint (or anything else on screen), and let Claude listen, look, ask
questions and build an artifact of what it understood while you are still talking.

```
you talk + draw  ──►  capture (local)  ──►  Claude Code  ──►  artifact page (its understanding, live)
                      speech-to-text         reads events        └─► questions in a small window
                      screen changes         asks, confirms
```

- **Hears you**: the microphone is transcribed on your machine with
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (GPU if you have an NVIDIA card). Audio is never
  saved or sent anywhere; only the text reaches Claude.
- **Sees your screen**: a screenshot is saved whenever the screen changes and settles, and Claude reads the new
  ones as you talk. Screenshots Claude reads are sent to Claude like any image you paste.
- **Talks back** in a small always-on-top window: short "here's what I heard" confirmations and one question at a
  time. The window is hidden from the screenshots, so Claude never sees its own messages.
- **Builds as you go**: an artifact with the pieces of your idea, a diagram, how they work together, every
  decision with your reason for it, assumptions to confirm and open questions. Say "no, that's wrong" and it
  changes.

## Install

Needs Python 3.10+ and Claude Code.

```powershell
git clone https://github.com/v3rysp3d/live-reporting
powershell -ExecutionPolicy Bypass -File live-reporting\install.ps1     # Windows
```

```bash
git clone https://github.com/v3rysp3d/live-reporting && ./live-reporting/install.sh   # macOS / Linux
```

The installer makes a private venv (in `%LOCALAPPDATA%\live-reporting`, or `~/.local/share/live-reporting`),
downloads the speech model (about 1.6 GB with an NVIDIA GPU, 150 MB on CPU), links the skill into
`~/.claude/skills/live-reporting`, and prints your microphones and screens.

## Use

In Claude Code:

```
/live-reporting the plugin system for the editor: core vs. plugins and how they talk
```

Then talk. Click **Done** in the window or say "we're done" to finish; Claude does a final pass on the
artifact and offers to start building from it. **Mute mic** pauses listening.

Useful options (Claude passes them to `live.py start`; ask for them in plain words):

| Option | |
|---|---|
| `--monitor N` | which screen to watch (`live.py doctor` lists them; default primary, `0` = all) |
| `--no-screen` | voice only |
| `--mic N` | microphone index or name (default: system default) |
| `--prompt "names, jargon"` | words the transcriber should expect |
| `--pause 1.5` | seconds of silence that end a sentence (default 1.0) |
| `--chime` | beep when Claude asks a question |
| `--model small.en` / `--device cpu` | override the speech model |

## Files

Each session is a folder under the data folder's `sessions/`: `events.jsonl` (what Claude reads),
`transcript.md` (you and Claude, readable), `screens/`. Nothing is uploaded except what Claude reads. Delete the
folder to delete the session.

`scripts/live.py` (standard library only) is the control: `doctor`, `start`, `watch`, `say`, `stop`, `status`,
`transcript`. `scripts/capture.py` is the capture process that runs in the venv.

## Tests

```
<venv python> -m pytest tests
```

## License

MIT
