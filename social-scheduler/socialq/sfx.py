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


def _beat(duration: float, bpm: float = 112.0, seed: int = 3) -> list[float]:
    """An upbeat loop: kick on every beat, clap on 2 and 4, offbeat hats, and a bouncing bass line."""
    rng = random.Random(seed)
    n = int(RATE * duration)
    out = [0.0] * n
    beat = 60.0 / bpm
    bass_notes = (65.4, 65.4, 55.0, 55.0, 43.7, 43.7, 49.0, 49.0)          # C C A A F F G G (per bar half)
    kick = [0.9 * math.sin(2 * math.pi * (50 + 90 * math.exp(-i / (RATE * 0.03))) * i / RATE)
            * math.exp(-i / (RATE * 0.12)) for i in range(int(RATE * 0.3))]
    hat = [rng.uniform(-1, 1) * 0.12 * math.exp(-i / (RATE * 0.015)) for i in range(int(RATE * 0.06))]
    hat = [h - hat[i - 1] if i else h for i, h in enumerate(hat)]                # crude high-pass
    clap = [rng.uniform(-1, 1) * 0.3 * math.exp(-i / (RATE * 0.05)) for i in range(int(RATE * 0.15))]

    def add(sound, at):
        start = int(at * RATE)
        for i, v in enumerate(sound):
            if start + i < n:
                out[start + i] += v

    k = 0
    while k * beat < duration:
        t = k * beat
        add(kick, t)
        add(hat, t + beat / 2)
        if k % 2 == 1:
            add(clap, t)
        f = bass_notes[(k // 2) % len(bass_notes)]
        add([0.22 * math.sin(2 * math.pi * f * i / RATE) * math.exp(-i / (RATE * 0.18))
             for i in range(int(RATE * beat * 0.9))], t + beat / 2)
        k += 1
    fade = int(RATE * 1.5)
    for i in range(min(fade, n)):
        out[n - 1 - i] *= i / fade
    return [0.55 * v for v in out]


def render(events: list[Event], duration: float, bed: bool, path: Path, seed: int = 7, beat: bool = False) -> None:
    rng = random.Random(seed)
    n = int(RATE * duration)
    mix = _bed(duration) if bed else [0.0] * n
    if beat:
        mix = [a + b for a, b in zip(mix, _beat(duration))]
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


def add_sfx(video: Path, out: Path | None = None, bed: bool = False, chime_at: float | None = None,
            speed: float = 1.0, music: Path | None = None, beat: bool = False,
            music_volume: float = 0.5) -> tuple[Path, list[Event]]:
    """Speed the video up (optional), add effects, and mix in a music file or a synthesized beat/pad."""
    ffmpeg = _ffmpeg()
    video = Path(video).expanduser().resolve()
    if not video.is_file():
        raise RuntimeError(f"not found: {video}")
    if music is not None:
        music = Path(music).expanduser().resolve()
        if not music.is_file():
            raise RuntimeError(f"music file not found: {music}")
    if not 0.5 <= speed <= 2.0:
        raise RuntimeError("--speed must be between 0.5 and 2.0")
    out = out or video.with_name(f"{video.stem}-sfx{video.suffix}")
    with tempfile.TemporaryDirectory() as tmp:
        source = video
        if abs(speed - 1.0) > 1e-3:
            source = Path(tmp) / f"fast{video.suffix}"
            done = subprocess.run(
                [ffmpeg, "-hide_banner", "-y", "-i", str(video), "-an", "-vf", f"setpts=PTS/{speed}",
                 "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", str(source)],
                capture_output=True, text=True)
            if done.returncode != 0:
                raise RuntimeError(f"ffmpeg couldn't speed it up:\n{done.stderr[-800:]}")
        changes, duration = detect(source, ffmpeg)
        events = plan(changes, duration, chime_at)
        audio = Path(tmp) / "sfx.wav"
        render(events, duration, bed and music is None, audio, beat=beat and music is None)
        cmd = [ffmpeg, "-hide_banner", "-y", "-i", str(source), "-i", str(audio)]
        if music is not None:
            fade_at = max(0.0, duration - 1.5)
            cmd += ["-stream_loop", "-1", "-i", str(music), "-filter_complex",
                    f"[2:a]atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,volume={music_volume},"
                    f"afade=t=in:d=0.2,afade=t=out:st={fade_at:.3f}:d=1.5[m];"
                    "[1:a][m]amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.9[a]",
                    "-map", "0:v:0", "-map", "[a]"]
        else:
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-shortest",
                "-movflags", "+faststart", str(out)]
        done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{done.stderr[-800:]}")
    return out, events
