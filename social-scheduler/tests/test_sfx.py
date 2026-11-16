import shutil
import subprocess

import pytest

from socialq import sfx


def test_plan_whooshes_cuts_ticks_text_and_chimes_the_reveal():
    changes = [(0.1, 0.9), (3.0, 0.02), (3.3, 0.02), (8.0, 1.0), (8.2, 0.5), (16.0, 0.8), (22.0, 0.025)]
    events = sfx.plan(changes, 26.0)
    assert [(e.t, e.kind) for e in events] == [(3.0, "tick"), (8.0, "whoosh"), (16.0, "whoosh"), (22.0, "chime")]


def test_plan_without_changes_still_chimes_near_the_end():
    events = sfx.plan([], 30.0)
    assert [(e.t, e.kind) for e in events] == [(27.5, "chime")]


def test_render_writes_a_wav_without_clipping(tmp_path):
    out = tmp_path / "a.wav"
    sfx.render([sfx.Event(1.0, "whoosh"), sfx.Event(2.0, "chime")], 3.0, True, out)
    import wave
    with wave.open(str(out)) as w:
        assert w.getnframes() == 3 * sfx.RATE
        frames = w.readframes(w.getnframes())
    peak = max(abs(int.from_bytes(frames[i:i + 2], "little", signed=True)) for i in range(0, len(frames), 2))
    assert 0 < peak <= 32767 * 0.9


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_add_sfx_end_to_end(tmp_path):
    video = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x284:d=3",
                    "-f", "lavfi", "-i", "color=c=red:s=160x284:d=3", "-filter_complex", "[0][1]concat=n=2:v=1",
                    "-pix_fmt", "yuv420p", str(video)], check=True)
    out, events = sfx.add_sfx(video)
    assert out.name == "v-sfx.mp4" and out.exists()
    assert any(e.kind == "whoosh" and abs(e.t - 3.0) < 0.1 for e in events)
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                            str(out)], capture_output=True, text=True).stdout.split()
    assert probe == ["video", "audio"]


def test_plan_chime_can_be_placed_by_hand():
    events = sfx.plan([(3.0, 0.02), (33.0, 0.02)], 34.0, chime_at=33.0)
    assert [(e.t, e.kind) for e in events] == [(3.0, "tick"), (33.0, "chime")]
