#!/usr/bin/env bash
# Installs live-reporting for Claude Code on macOS / Linux:
#   1. a private Python venv for the capture (speech-to-text + screen + window)
#   2. the speech model download
#   3. a symlink from ~/.claude/skills/live-reporting to this folder
# Linux needs PortAudio and Tk first, e.g. `sudo apt install libportaudio2 python3-tk python3-venv`.
# macOS asks for Microphone and Screen Recording permission for your terminal on first use.
set -euo pipefail
repo="$(cd "$(dirname "$0")" && pwd)"
home_="${LIVE_REPORTING_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/live-reporting}"
venv="$home_/venv"

python3 -c 'import sys; assert sys.version_info >= (3, 10)' || { echo "Python 3.10+ required"; exit 1; }
echo "venv: $venv"
[ -x "$venv/bin/python" ] || python3 -m venv "$venv"
"$venv/bin/python" -m pip install --quiet --upgrade pip
"$venv/bin/python" -m pip install --quiet -r "$repo/requirements.txt"
if command -v nvidia-smi >/dev/null 2>&1; then
  echo "NVIDIA GPU found: installing the CUDA libraries for fast transcription"
  "$venv/bin/python" -m pip install --quiet -r "$repo/requirements-cuda.txt"
fi

echo "Downloading the speech model (first time only)..."
tmp="$(mktemp -d)"
"$venv/bin/python" "$repo/scripts/capture.py" --session "$tmp/s" --download
tail -n 1 "$tmp/s/events.jsonl"
rm -rf "$tmp"

mkdir -p "$HOME/.claude/skills"
link="$HOME/.claude/skills/live-reporting"
if [ -L "$link" ]; then echo "skill link already there: $link"
elif [ -e "$link" ]; then echo "warning: $link exists and is not a link; leaving it alone"
else ln -s "$repo" "$link"; echo "skill linked: $link -> $repo"; fi

"$venv/bin/python" "$repo/scripts/live.py" doctor
printf '\nDone. In Claude Code: /live-reporting <what you want to explain>\n'
