"""Capture daemon for a live-reporting session.

Listens to the microphone, cuts speech into utterances at pauses, transcribes them locally with
faster-whisper, watches the screen for changes, and shows Claude's messages in a small always-on-top window.
Everything it produces goes into the session folder:

  events.jsonl   one JSON object per line: status / speech / screen / error / end
  transcript.md  the same speech, human readable
  screens/       downscaled JPEG screenshots
  outbox.jsonl   messages from Claude (written by `live.py say`), shown in the window
  capture.pid    this process
  stop           created by `live.py stop`; the daemon exits when it appears

Nothing leaves the machine from here: audio is transcribed locally and never saved.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EventLog, now_iso  # noqa: E402

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME = SAMPLE_RATE * FRAME_MS // 1000

# Whisper's usual inventions on silence or breath; dropped when they are the whole utterance.
HALLUCINATIONS = {
    "", "you", "thank you", "thank you.", "thanks for watching", "thanks for watching!", "bye", "bye.",
    "thank you for watching", "thank you for watching.", ".", "...", "okay.", "so",
}


# ---------------------------------------------------------------- speech segmentation

class Segmenter:
    """Energy-based voice activity detection with an adaptive noise floor.

    feed() takes 30 ms float32 frames and returns a finished utterance (one numpy array) when the speaker
    pauses for `pause_s`, or when an utterance reaches `max_s`.
    """

    def __init__(self, pause_s: float = 1.0, max_s: float = 30.0, min_speech_s: float = 0.35,
                 pre_roll_s: float = 0.3, min_level: float = 0.006):
        import numpy as np
        self.np = np
        self.pause_frames = int(pause_s * 1000 / FRAME_MS)
        self.max_frames = int(max_s * 1000 / FRAME_MS)
        self.min_speech_frames = int(min_speech_s * 1000 / FRAME_MS)
        self.pre_roll = int(pre_roll_s * 1000 / FRAME_MS)
        self.min_level = min_level
        self.noise = min_level
        self.levels: deque = deque(maxlen=int(4500 / FRAME_MS))
        self.frames: list = []
        self.history: list = []
        self.in_speech = False
        self.voiced = 0
        self.silent = 0
        self.onset = 0

    def level(self, frame) -> float:
        return float(self.np.sqrt(self.np.mean(frame * frame)))

    def threshold(self) -> float:
        return max(self.noise * 3.0, self.min_level)

    def feed(self, frame):
        rms = self.level(frame)
        # Noise floor = a low percentile of the last few seconds, so steady room noise (a fan) never counts as
        # speech for long, while the gaps between words keep it low during talking.
        self.levels.append(rms)
        self.noise = max(float(self.np.percentile(self.levels, 10)), 1e-5)
        loud = rms > self.threshold()
        if not self.in_speech:
            self.history.append(frame)
            self.history = self.history[-(self.pre_roll + 3):]
            self.onset = self.onset + 1 if loud else 0
            if self.onset >= 3:
                self.in_speech = True
                self.frames = list(self.history)
                self.voiced = self.onset
                self.silent = 0
            return None
        self.frames.append(frame)
        if loud:
            self.voiced += 1
            self.silent = 0
        else:
            self.silent += 1
        if self.silent >= self.pause_frames or len(self.frames) >= self.max_frames:
            return self._finish()
        return None

    def flush(self):
        return self._finish() if self.in_speech else None

    def _finish(self):
        frames, voiced = self.frames, self.voiced
        self.frames, self.history = [], []
        self.in_speech, self.voiced, self.silent, self.onset = False, 0, 0, 0
        if voiced < self.min_speech_frames:
            return None
        return self.np.concatenate(frames)


# ---------------------------------------------------------------- transcription

def add_cuda_dll_dirs() -> None:
    """faster-whisper's CUDA path needs cuBLAS/cuDNN; the nvidia-* wheels ship them but Windows won't find them."""
    if os.name != "nt":
        return
    try:
        import nvidia  # type: ignore
    except ImportError:
        return
    for base in getattr(nvidia, "__path__", []):
        for sub in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
            d = Path(base) / sub / "bin"
            if d.is_dir():
                os.add_dll_directory(str(d))
                os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


def load_model(name: str, device: str):
    add_cuda_dll_dirs()
    from faster_whisper import WhisperModel
    import ctranslate2
    if device == "auto":
        device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    if name == "auto":
        name = "large-v3-turbo" if device == "cuda" else "base.en"
    compute = "float16" if device == "cuda" else "int8"
    try:
        model = WhisperModel(name, device=device, compute_type=compute)
    except Exception:
        if device != "cuda":
            raise
        # CUDA present but its libraries are not: fall back rather than fail the session
        device, compute = "cpu", "int8"
        name = "base.en" if name == "large-v3-turbo" else name
        model = WhisperModel(name, device=device, compute_type=compute)
    return model, name, device


def transcribe(model, audio, language: str | None, prompt: str | None) -> str:
    segments, _ = model.transcribe(
        audio, language=language, beam_size=5, vad_filter=True,
        initial_prompt=prompt or None, condition_on_previous_text=False,
    )
    text = " ".join(s.text.strip() for s in segments).strip()
    return "" if text.lower() in HALLUCINATIONS else text


# ---------------------------------------------------------------- screen

class ScreenWatcher:
    """Grabs the screen every `interval` s; saves a frame when it has changed and then held still for `settle` s."""

    def __init__(self, folder: Path, monitor: int, interval: float = 1.0, settle: float = 2.0,
                 change: float = 1.5, max_edge: int = 1568):
        self.folder = folder
        self.monitor = monitor
        self.interval = interval
        self.settle = settle
        self.change = change
        self.max_edge = max_edge
        self.saved_thumb = None
        self.last_thumb = None
        self.changed_at: float | None = None
        self.latest: str | None = None
        self.count = 0
        self.lock = threading.Lock()

    @staticmethod
    def thumb(img):
        import numpy as np
        return np.asarray(img.convert("L").resize((96, 54)), dtype="float32")

    @staticmethod
    def diff(a, b) -> float:
        import numpy as np
        return float(np.mean(np.abs(a - b)))

    def grab(self, sct):
        from PIL import Image
        mon = sct.monitors[self.monitor]
        raw = sct.grab(mon)
        return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    def save(self, img) -> str:
        self.count += 1
        path = self.folder / f"{self.count:04d}.jpg"
        w, h = img.size
        scale = min(1.0, self.max_edge / max(w, h))
        if scale < 1.0:
            img = img.resize((int(w * scale), int(h * scale)))
        img.save(path, "JPEG", quality=82)
        return str(path)

    def step(self, img, t: float) -> str | None:
        """Feed one frame; returns the saved path when a new settled frame was stored."""
        th = self.thumb(img)
        if self.saved_thumb is None:
            self.saved_thumb = self.last_thumb = th
            with self.lock:
                self.latest = self.save(img)
            return self.latest
        moving = self.diff(th, self.last_thumb) > self.change
        self.last_thumb = th
        if moving:
            self.changed_at = t
            return None
        if self.changed_at is None or t - self.changed_at < self.settle:
            return None
        self.changed_at = None
        if self.diff(th, self.saved_thumb) <= self.change:
            return None
        self.saved_thumb = th
        with self.lock:
            self.latest = self.save(img)
        return self.latest


# ---------------------------------------------------------------- overlay window

class Overlay:
    """Small always-on-top window with Claude's latest messages, a mic toggle and a Done button."""

    def __init__(self, session: Path, state: "State"):
        import tkinter as tk
        self.tk = tk
        self.session = session
        self.state = state
        self.outbox = session / "outbox.jsonl"
        self.read_upto = 0
        self.messages: list[dict] = []

        if os.name == "nt":
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on scaled displays
            except Exception:
                pass
        root = tk.Tk()
        self.root = root
        root.title("live-reporting")
        root.attributes("-topmost", True)
        root.configure(bg="#1e1e1e")
        scale = root.winfo_fpixels("1i") / 96.0
        w, h = int(460 * scale), int(300 * scale)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{w}x{h}+{sw - w - int(24 * scale)}+{sh - h - int(72 * scale)}")
        root.protocol("WM_DELETE_WINDOW", self.done)

        top = tk.Frame(root, bg="#1e1e1e")
        top.pack(fill="x", padx=10, pady=(8, 4))
        self.status = tk.Label(top, text="starting", fg="#9a9a9a", bg="#1e1e1e", font=("Segoe UI", 9), anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        tk.Button(top, text="Done", command=self.done, relief="flat", bg="#3a3a3a", fg="#e8e8e8",
                  activebackground="#4a4a4a", font=("Segoe UI", 9), padx=10).pack(side="right")
        self.mic_btn = tk.Button(top, text="Mute mic", command=self.toggle_mic, relief="flat", bg="#3a3a3a",
                                 fg="#e8e8e8", activebackground="#4a4a4a", font=("Segoe UI", 9), padx=10)
        self.mic_btn.pack(side="right", padx=6)

        self.text = tk.Text(root, wrap="word", bg="#1e1e1e", fg="#e8e8e8", relief="flat", font=("Segoe UI", 11),
                            padx=10, pady=4, highlightthickness=0, borderwidth=0)
        self.text.pack(fill="both", expand=True, padx=2, pady=(0, 8))
        self.text.tag_configure("kind", foreground="#9a9a9a", font=("Segoe UI", 8))
        self.text.tag_configure("question", foreground="#f0c674")
        self.text.tag_configure("body", foreground="#e8e8e8")
        self.text.insert("end", "Claude's questions and notes show up here.\n", "kind")
        self.text.configure(state="disabled")

        root.after(200, self.exclude_from_capture)
        root.after(300, self.poll)

    def exclude_from_capture(self) -> None:
        """Keep this window out of the screenshots Claude sees (Windows 10 2004+)."""
        if os.name != "nt" or self.state.overlay_in_capture:
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11)  # WDA_EXCLUDEFROMCAPTURE
        except Exception:
            pass

    def toggle_mic(self) -> None:
        self.state.muted = not self.state.muted
        self.mic_btn.configure(text="Unmute mic" if self.state.muted else "Mute mic")
        self.state.log.write({"type": "status", "text": "mic muted" if self.state.muted else "mic on"})

    def done(self) -> None:
        self.state.stop_reason = "user pressed Done"
        self.state.stopping.set()

    def poll(self) -> None:
        if self.state.stopping.is_set():
            self.root.destroy()
            return
        self.status.configure(text=self.state.status_text())
        self.read_outbox()
        self.root.after(300, self.poll)

    def read_outbox(self) -> None:
        if not self.outbox.exists():
            return
        with self.outbox.open("r", encoding="utf-8") as f:
            f.seek(self.read_upto)
            new = f.read()
            self.read_upto = f.tell()
        added = False
        for line in new.splitlines():
            try:
                self.messages.append(json.loads(line))
                added = True
            except json.JSONDecodeError:
                continue
        if added:
            self.render()

    def render(self) -> None:
        t = self.text
        t.configure(state="normal")
        t.delete("1.0", "end")
        for m in self.messages[-6:]:
            kind = m.get("kind", "note")
            t.insert("end", f"{kind.upper()}  {m.get('time', '')[11:19]}\n", "kind")
            t.insert("end", m.get("text", "") + "\n\n", "question" if kind == "question" else "body")
        t.configure(state="disabled")
        t.see("end")
        self.root.bell() if self.messages[-1].get("kind") == "question" and self.state.chime else None

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------- daemon

class State:
    def __init__(self, log: EventLog, chime: bool):
        self.log = log
        self.chime = chime
        self.overlay_in_capture = False
        self.muted = False
        self.stopping = threading.Event()
        self.stop_reason = ""
        self.phase = "starting"
        self.utterances = 0

    def status_text(self) -> str:
        mic = "mic muted" if self.muted else self.phase
        return f"{mic}  ·  {self.utterances} heard"


def audio_thread(args, state: State, chunks: "queue.Queue") -> None:
    import numpy as np
    seg = Segmenter(pause_s=args.pause)
    if args.wav:
        import wave
        with wave.open(args.wav, "rb") as w:
            if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
                state.log.write({"type": "error", "text": "--wav must be 16 kHz mono 16-bit"})
                state.stopping.set()
                return
            data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype("float32") / 32768.0
        data = np.concatenate([data, np.zeros(SAMPLE_RATE * 2, dtype="float32")])
        for i in range(0, len(data) - FRAME, FRAME):
            out = seg.feed(data[i:i + FRAME])
            if out is not None:
                chunks.put(out)
        chunks.put(None)
        return

    import sounddevice as sd
    frames: "queue.Queue" = queue.Queue()

    def cb(indata, _n, _t, status):
        frames.put(indata[:, 0].copy())

    try:
        stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=FRAME,
                                device=args.mic, callback=cb)
    except Exception as e:
        state.log.write({"type": "error", "text": f"microphone failed to open: {e}"})
        state.stopping.set()
        return
    with stream:
        dev = sd.query_devices(stream.device, "input")["name"]
        state.log.write({"type": "status", "text": f"mic: {dev}"})
        while not state.stopping.is_set():
            try:
                frame = frames.get(timeout=0.5)
            except queue.Empty:
                continue
            if state.muted:
                seg.flush()
                continue
            state.phase = "hearing you" if seg.in_speech else "listening"
            out = seg.feed(frame)
            if out is not None:
                chunks.put(out)
    tail = seg.flush()
    if tail is not None:
        chunks.put(tail)
    chunks.put(None)


def transcribe_thread(args, state: State, chunks: "queue.Queue", model, screens: ScreenWatcher | None,
                      transcript: Path) -> None:
    last_screen_sent: str | None = None
    while True:
        audio = chunks.get()
        if audio is None:
            break
        started = time.time()
        state.phase = "transcribing"
        try:
            text = transcribe(model, audio, args.language, args.prompt)
        except Exception as e:
            state.log.write({"type": "error", "text": f"transcription failed: {e}"})
            continue
        finally:
            state.phase = "listening"
        if not text:
            continue
        state.utterances += 1
        ev = {"type": "speech", "text": text, "seconds": round(len(audio) / SAMPLE_RATE, 1),
              "lag": round(time.time() - started, 1)}
        if screens is not None:
            with screens.lock:
                latest = screens.latest
            if latest:
                ev["screen"] = latest
                ev["screen_new"] = latest != last_screen_sent
                last_screen_sent = latest
        state.log.write(ev)
        with transcript.open("a", encoding="utf-8") as f:
            f.write(f"**{now_iso()[11:19]}** {text}\n\n")
    if args.wav:
        state.stop_reason = "wav finished"
        state.stopping.set()


def screen_thread(args, state: State, screens: ScreenWatcher) -> None:
    import mss
    quiet_since = time.time()
    with getattr(mss, "MSS", mss.mss)() as sct:
        if screens.monitor < 0:
            # mss lists screens in enumeration order; the primary one is the screen at the desktop origin
            screens.monitor = next((i for i, m in enumerate(sct.monitors) if i and m["left"] == 0 and m["top"] == 0), 1)
        elif screens.monitor >= len(sct.monitors):
            state.log.write({"type": "error", "text": f"monitor {screens.monitor} not found; using 1"})
            screens.monitor = 1
        m = sct.monitors[screens.monitor]
        state.log.write({"type": "status", "text": f"screen {screens.monitor}: {m['width']}x{m['height']}"})
        while not state.stopping.is_set():
            t = time.time()
            try:
                saved = screens.step(screens.grab(sct), t)
            except Exception as e:
                state.log.write({"type": "error", "text": f"screen capture failed: {e}"})
                saved = None
            if state.phase in ("hearing you", "transcribing"):
                quiet_since = t
            # A changed screen with no talking (drawing in silence) is its own event; while talking,
            # the next speech event carries the newest frame instead.
            if saved and t - quiet_since > args.quiet_screen:
                state.log.write({"type": "screen", "screen": saved, "note": "screen changed while you were quiet"})
            state.stopping.wait(screens.interval)


def stop_file_thread(session: Path, state: State) -> None:
    while not state.stopping.is_set():
        if (session / "stop").exists():
            state.stop_reason = "live.py stop"
            state.stopping.set()
        state.stopping.wait(0.5)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--session", required=True)
    p.add_argument("--model", default="auto", help="faster-whisper model name, or auto")
    p.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    p.add_argument("--language", default="en", help="spoken language code, or '' to detect")
    p.add_argument("--prompt", default="", help="words the transcriber should expect (names, jargon)")
    p.add_argument("--mic", default=None, help="input device index or name (default: system default)")
    p.add_argument("--monitor", type=int, default=-1,
                   help="screen number from `live.py doctor`; -1 = primary (default), 0 = all screens")
    p.add_argument("--no-screen", action="store_true")
    p.add_argument("--no-overlay", action="store_true")
    p.add_argument("--pause", type=float, default=1.0, help="seconds of silence that end an utterance")
    p.add_argument("--quiet-screen", type=float, default=6.0,
                   help="a screen change is its own event after this many seconds without speech")
    p.add_argument("--chime", action="store_true", help="beep when Claude asks a question")
    p.add_argument("--overlay-in-capture", action="store_true",
                   help="let screenshots include the live window (it is hidden from them by default)")
    p.add_argument("--wav", default=None, help="transcribe this 16 kHz mono WAV instead of the mic (testing)")
    p.add_argument("--download", action="store_true", help="only download the model, then exit")
    args = p.parse_args(argv)
    if args.mic is not None and args.mic.isdigit():
        args.mic = int(args.mic)
    args.language = args.language or None

    session = Path(args.session)
    session.mkdir(parents=True, exist_ok=True)
    log = EventLog(session / "events.jsonl")
    (session / "capture.pid").write_text(str(os.getpid()))
    state = State(log, args.chime)
    state.overlay_in_capture = args.overlay_in_capture

    try:
        log.write({"type": "status", "text": "loading speech model"})
        model, name, device = load_model(args.model, args.device)
    except Exception as e:
        log.write({"type": "error", "text": f"speech model failed to load: {e}"})
        log.write({"type": "end", "reason": "startup failed"})
        return 1
    if args.download:
        log.write({"type": "status", "text": f"model ready: {name} on {device}"})
        return 0

    screens = None
    threads = []
    if not args.no_screen:
        (session / "screens").mkdir(exist_ok=True)
        screens = ScreenWatcher(session / "screens", args.monitor)
        threads.append(threading.Thread(target=screen_thread, args=(args, state, screens), daemon=True))
    chunks: "queue.Queue" = queue.Queue()
    transcript = session / "transcript.md"
    if not transcript.exists():
        transcript.write_text(f"# Live session {session.name}\n\n", encoding="utf-8")
    threads += [
        threading.Thread(target=audio_thread, args=(args, state, chunks), daemon=True),
        threading.Thread(target=stop_file_thread, args=(session, state), daemon=True),
    ]
    worker = threading.Thread(target=transcribe_thread, args=(args, state, chunks, model, screens, transcript))
    for t in threads:
        t.start()
    worker.start()
    state.phase = "listening"
    log.write({"type": "status", "text": f"ready: listening (model {name} on {device}"
                                         f"{'' if screens else ', no screen'})"})

    if args.no_overlay or args.wav:
        while not state.stopping.is_set():
            state.stopping.wait(0.5)
    else:
        try:
            Overlay(session, state).run()
        except Exception as e:
            log.write({"type": "error", "text": f"overlay window failed: {e}"})
            while not state.stopping.is_set():
                state.stopping.wait(0.5)
    state.stopping.set()
    worker.join(timeout=60)  # let the last utterance finish transcribing
    log.write({"type": "end", "reason": state.stop_reason or "stopped"})
    try:
        (session / "capture.pid").unlink()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
