"""Read a video's length and size from its MP4/MOV header (no ffmpeg needed)."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass
class VideoInfo:
    duration_s: float
    width: int
    height: int

    @property
    def vertical(self) -> bool:
        return self.height > self.width


def _boxes(data: bytes, start: int = 0, end: int | None = None):
    """Yield (type, payload_start, payload_end) for the boxes in data[start:end]."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header:
            return
        yield kind.decode("latin-1"), pos + header, min(pos + size, end)
        pos += size


def _read_moov(path: Path) -> bytes | None:
    """The moov box, wherever it sits in the file (start or end)."""
    with path.open("rb") as fh:
        fh.seek(0, 2)
        file_size = fh.tell()
        pos = 0
        while pos + 8 <= file_size:
            fh.seek(pos)
            head = fh.read(16)
            size, kind = struct.unpack(">I4s", head[:8])
            header = 8
            if size == 1:
                size, header = struct.unpack(">Q", head[8:16])[0], 16
            elif size == 0:
                size = file_size - pos
            if size < header:
                return None
            if kind == b"moov":
                fh.seek(pos + header)
                return fh.read(size - header)
            pos += size
    return None


def probe(path: Path) -> VideoInfo | None:
    """Duration and display size of an MP4/MOV, or None if the header can't be read."""
    try:
        moov = _read_moov(Path(path))
    except (OSError, struct.error):
        return None
    if not moov:
        return None
    try:
        duration = None
        width = height = 0
        for kind, s, e in _boxes(moov):
            if kind == "mvhd":
                version = moov[s]
                if version == 1:
                    timescale, length = struct.unpack(">IQ", moov[s + 20:s + 32])
                else:
                    timescale, length = struct.unpack(">II", moov[s + 12:s + 20])
                duration = length / timescale if timescale else None
            elif kind == "trak" and not width:
                for sub, ts, te in _boxes(moov, s, e):
                    if sub != "tkhd":
                        continue
                    matrix_at = ts + (52 if moov[ts] == 1 else 40)
                    a, b = struct.unpack(">ii", moov[matrix_at:matrix_at + 8])
                    w, h = struct.unpack(">II", moov[matrix_at + 36:matrix_at + 44])
                    w, h = w >> 16, h >> 16
                    if w and h:  # audio tracks have no size
                        rotated = a == 0 and b != 0   # 90 or 270 degrees: phones store it sideways
                        width, height = (h, w) if rotated else (w, h)
        if duration is None or not width:
            return None
        return VideoInfo(duration, width, height)
    except (struct.error, IndexError):
        return None


# --- copies the background job can read ------------------------------------------
#
# macOS won't let a background job read Desktop, Documents or Downloads, and a file
# moved or deleted before its upload breaks the post. So `add` copies videos into
# media_dir/queued/, and a copy is deleted once everything that uses it has posted.

COPY_DIR = "queued"
ORPHAN_GRACE_S = 3600


def copy_into_media(media_dir: Path, video: Path) -> str:
    """Copy `video` into media_dir/queued/ (reusing an identical copy); return its path relative to media_dir."""
    import shutil

    target_dir = Path(media_dir) / COPY_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    size = video.stat().st_size
    dest = target_dir / video.name
    n = 2
    while dest.exists() and dest.stat().st_size != size:
        dest = target_dir / f"{video.stem}-{n}{video.suffix}"
        n += 1
    if not dest.exists():
        partial = dest.with_name(dest.name + ".partial")
        shutil.copyfile(video, partial)   # fresh mtime: the orphan check goes by it
        partial.replace(dest)
    return str(dest.relative_to(media_dir))


def delete_finished_copies(media_dir: Path, posts, is_done, now_ts: float) -> list[str]:
    """Delete copies whose posts have all gone out (or that no post uses any more)."""
    folder = Path(media_dir) / COPY_DIR
    if not folder.is_dir():
        return []
    users: dict[str, list] = {}
    for post in posts:
        users.setdefault(Path(str(post.video)).as_posix(), []).append(post)
    deleted = []
    for f in folder.iterdir():
        if not f.is_file() or f.name.endswith(".partial"):
            continue
        ref = f"{COPY_DIR}/{f.name}"
        using = users.get(ref)
        if using is None:
            if now_ts - f.stat().st_mtime < ORPHAN_GRACE_S:
                continue
        elif not all(is_done(p) for p in using):
            continue
        f.unlink()
        deleted.append(ref)
    return deleted
