"""Add sound effects to a silent video: a whoosh on each cut, a soft tick when on-screen text
changes, a chime on the final reveal, and (optionally) a quiet music bed. The sounds are
synthesized here, so there's nothing to license. Needs ffmpeg (brew install ffmpeg).

    ./sq sfx video.mp4            -> video-sfx.mp4 next to it
"""

from __future__ import annotations

import math
import random
import re
import shutil
import struct
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

RATE = 44100
CUT_SCORE = 0.30        # scene score for a real cut -> whoosh
TEXT_SCORE = 0.012      # smaller change (a caption or number appearing) -> tick
MIN_GAP_S = 0.45        # never two effects closer than this
TICK_GAP_S = 1.0        # and ticks at most about once a second, so captions don't rattle


@dataclass
class Event:
    t: float
    kind: str            # whoosh | tick | chime


# --- synthesis ------------------------------------------------------------------------

def _whoosh(rng: random.Random, length: float = 0.42) -> list[float]:
    n = int(RATE * length)
    out, low, band = [], 0.0, 0.0
    for i in range(n):
        x = i / n
        cutoff = 0.02 + 0.25 * math.sin(math.pi * x)          # filter opens then closes
        low += cutoff * (rng.uniform(-1, 1) - low)
        band += 0.5 * (low - band)
        out.append((low - band) * 2.2 * math.sin(math.pi * x) ** 1.5)
    return out


def _tick(freq: float = 1500.0, length: float = 0.07) -> list[float]:
    n = int(RATE * length)
    return [0.35 * math.sin(2 * math.pi * freq * i / RATE) * math.exp(-i / (RATE * 0.012)) for i in range(n)]


def _chime(length: float = 1.6) -> list[float]:
    n = int(RATE * length)
    notes = (1046.5, 1318.5, 1568.0, 2093.0)                   # C6 E6 G6 C7, slightly staggered
    out = [0.0] * n
    for k, f in enumerate(notes):
        start = int(RATE * 0.06 * k)
        for i in range(n - start):
            env = math.exp(-i / (RATE * 0.45)) * min(1.0, i / 200)
            out[start + i] += 0.16 * env * (math.sin(2 * math.pi * f * i / RATE)
                                            + 0.3 * math.sin(4 * math.pi * f * i / RATE))
    return out


def _bed(duration: float) -> list[float]:
    """A quiet, slow chord pad (C - Am - F - G, 4 s each) so the video isn't silent."""
    chords = ((130.8, 164.8, 196.0), (110.0, 130.8, 164.8), (87.3, 110.0, 130.8), (98.0, 123.5, 146.8))
    n = int(RATE * duration)
    out = []
    for i in range(n):
        t = i / RATE
        chord = chords[int(t // 4) % len(chords)]
        pos = (t % 4) / 4
        env = min(1.0, pos * 8) * min(1.0, (1 - pos) * 8)      # soft crossfade between chords
        s = sum(math.sin(2 * math.pi * f * t) + 0.25 * math.sin(4 * math.pi * f * t) for f in chord)
        fade = min(1.0, t / 1.5) * min(1.0, (duration - t) / 1.5)
        out.append(0.035 * s * env * fade * (0.85 + 0.15 * math.sin(2 * math.pi * 0.25 * t)))
    return out


def render(events: list[Event], duration: float, bed: bool, path: Path, seed: int = 7) -> None:
    rng = random.Random(seed)
    n = int(RATE * duration)
    mix = _bed(duration) if bed else [0.0] * n
    sounds = {"tick": _tick(), "chime": _chime()}
    for ev in events:
        sound = _whoosh(rng) if ev.kind == "whoosh" else sounds[ev.kind]
        start = int(RATE * max(0.0, ev.t - (0.18 if ev.kind == "whoosh" else 0.0)))   # whoosh peaks on the cut
        for i, s in enumerate(sound):
            if start + i < n:
                mix[start + i] += s
    peak = max((abs(s) for s in mix), default=0.0) or 1.0
    gain = min(1.0, 0.89 / peak)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, s * gain)) * 32767)) for s in mix))


# --- timing -----------------------------------------------------------------------------

def plan(changes: list[tuple[float, float]], duration: float, chime_at: float | None = None) -> list[Event]:
    """Pick effects from (time, scene score) pairs: whooshes on cuts, ticks on smaller changes,
    and a chime on the last big change in the final third (or near the end)."""
    events: list[Event] = []
    last = last_tick = -10.0
    for t, score in sorted(changes):
        if t < 0.2 or t > duration - 0.3 or t - last < MIN_GAP_S:
            continue
        if score >= CUT_SCORE:
            events.append(Event(t, "whoosh"))
        elif score >= TEXT_SCORE and t - last_tick >= TICK_GAP_S:
            events.append(Event(t, "tick"))
            last_tick = t
        else:
            continue
        last = t
    reveal = [e for e in events if e.t >= duration * 0.66]
    target = reveal[-1].t if reveal else max(0.0, duration - 2.5)
    if chime_at is not None:
        target = min(max(0.0, chime_at), max(0.0, duration - 0.3))
    events = [e for e in events if abs(e.t - target) >= MIN_GAP_S]
    events.append(Event(target, "chime"))
    return sorted(events, key=lambda e: e.t)


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg") or next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
                                          if Path(p).exists()), None)
    if not exe:
        raise RuntimeError("ffmpeg isn't installed. Install it with:  brew install ffmpeg")
    return exe


def detect(video: Path, ffmpeg: str) -> tuple[list[tuple[float, float]], float]:
    done = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(video), "-an", "-vf",
         f"select='gt(scene,{TEXT_SCORE})',metadata=print", "-f", "null", "-"],
        capture_output=True, text=True)
    log = done.stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", log)
    if not m:
        raise RuntimeError(f"couldn't read {video.name} with ffmpeg:\n{log[-500:]}")
    duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    changes = []
    for t, score in re.findall(r"pts_time:([\d.]+).*?lavfi\.scene_score=([\d.]+)", log, re.S):
        changes.append((float(t), float(score)))
    return changes, duration


def add_sfx(video: Path, out: Path | None = None, bed: bool = False,
            chime_at: float | None = None) -> tuple[Path, list[Event]]:
    ffmpeg = _ffmpeg()
    video = Path(video).expanduser().resolve()
    if not video.is_file():
        raise RuntimeError(f"not found: {video}")
    out = out or video.with_name(f"{video.stem}-sfx{video.suffix}")
    changes, duration = detect(video, ffmpeg)
    events = plan(changes, duration, chime_at)
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / "sfx.wav"
        render(events, duration, bed, audio)
        done = subprocess.run(
            [ffmpeg, "-hide_banner", "-y", "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-shortest",
             "-movflags", "+faststart", str(out)],
            capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{done.stderr[-800:]}")
    return out, events
