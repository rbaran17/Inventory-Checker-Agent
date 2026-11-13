"""Turn photos and videos on disk into the frames a scan sends to the model.

Photos become one frame each, like a phone close-up: orientation fixed from EXIF, downscaled to
``PHOTO_EDGE`` on the long side, JPEG. Videos become segments of ``segment_frames`` frames taken every
``every_seconds``, downscaled to ``VIDEO_EDGE``, like the phone's Scan mode. The sizes match the phone app
so headless runs and phone runs cost and behave the same.

Videos need PyAV (``pip install 'inventory-checker-agent[video]'``) or the ``ffmpeg`` command on PATH.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

PHOTO_EDGE, PHOTO_QUALITY = 2048, 90
VIDEO_EDGE, VIDEO_QUALITY = 1280, 80
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp"}
MAX_SOURCE_PIXELS = 100_000_000  # refuse decompression bombs

Frame = tuple[bytes, str]  # (JPEG bytes, media type)


class MediaError(ValueError):
    """A file that cannot be turned into frames, with a message a person can act on."""


@dataclass
class Segment:
    """One scan's worth of frames and where they came from."""

    source: Path
    frames: list[Frame]
    label: str  # e.g. "photo", or "0:04-0:08" for a video segment

    @property
    def kind(self) -> str:
        return "photo" if len(self.frames) == 1 else "video"


def _jpeg(img: Image.Image, edge: int, quality: int) -> bytes:
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((edge, edge))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def load_photo(path: str | Path, edge: int = PHOTO_EDGE, quality: int = PHOTO_QUALITY) -> Frame:
    path = Path(path)
    if path.suffix.lower() in (".heic", ".heif"):
        raise MediaError(f"{path.name}: HEIC photos are not supported. Export as JPEG (or set the iPhone camera "
                         "to 'Most Compatible').")
    Image.MAX_IMAGE_PIXELS = MAX_SOURCE_PIXELS
    try:
        with Image.open(path) as img:
            img.seek(0)
            return _jpeg(img, edge, quality), "image/jpeg"
    except FileNotFoundError as exc:
        raise MediaError(f"{path}: file not found") from exc
    except (OSError, Image.DecompressionBombError) as exc:
        raise MediaError(f"{path.name}: not a readable image ({exc})") from exc


def _video_frames_pyav(path: Path, every: float) -> Iterator[tuple[float, Image.Image]]:
    import av  # optional dependency

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        next_t = 0.0
        for frame in container.decode(stream):
            if frame.time is None or frame.time + 1e-6 < next_t:
                continue
            yield frame.time, frame.to_image()
            next_t = frame.time + every


def _video_frames_ffmpeg(path: Path, every: float) -> Iterator[tuple[float, Image.Image]]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MediaError("Reading videos needs PyAV (pip install 'inventory-checker-agent[video]') or ffmpeg on PATH")
    with tempfile.TemporaryDirectory(prefix="ic-frames-") as tmp:
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf", f"fps=1/{every}",
               "-q:v", "3", str(Path(tmp) / "f%06d.jpg")]
        done = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise MediaError(f"{path.name}: ffmpeg could not read the video ({done.stderr.strip()[:200]})")
        for i, f in enumerate(sorted(Path(tmp).glob("f*.jpg"))):
            with Image.open(f) as img:
                yield i * every, img.copy()


def video_segments(path: str | Path, every_seconds: float = 1.0, segment_frames: int = 4,
                   edge: int = VIDEO_EDGE, quality: int = VIDEO_QUALITY) -> Iterator[Segment]:
    """Frames every ``every_seconds``, grouped into segments of ``segment_frames`` (the last may be shorter)."""
    path = Path(path)
    if not path.is_file():
        raise MediaError(f"{path}: file not found")
    if every_seconds <= 0 or segment_frames < 1:
        raise MediaError("every_seconds must be > 0 and segment_frames >= 1")
    try:
        import av  # noqa: F401
        frames = _video_frames_pyav(path, every_seconds)
    except ImportError:
        frames = _video_frames_ffmpeg(path, every_seconds)
    batch: list[Frame] = []
    start = 0.0
    try:
        for t, img in frames:
            if not batch:
                start = t
            batch.append((_jpeg(img, edge, quality), "image/jpeg"))
            if len(batch) == segment_frames:
                yield Segment(path, batch, f"{_clock(start)}-{_clock(t)}")
                batch = []
    except MediaError:
        raise
    except Exception as exc:  # PyAV raises its own error types for broken or unsupported files
        raise MediaError(f"{path.name}: could not read the video ({exc})") from exc
    if batch:
        yield Segment(path, batch, f"{_clock(start)}-end")


def _clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def expand(paths: Iterable[str | Path]) -> list[Path]:
    """Files given directly, plus images and videos inside any folders given (sorted, not recursive)."""
    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS)
        elif p.exists():
            out.append(p)
        else:
            raise MediaError(f"{p}: file or folder not found")
    return out


def segments(paths: Iterable[str | Path], every_seconds: float = 1.0, segment_frames: int = 4) -> Iterator[Segment]:
    """Every scan to run for the given files and folders, in order."""
    for path in expand(paths):
        ext = path.suffix.lower()
        if ext in VIDEO_EXTENSIONS:
            yield from video_segments(path, every_seconds, segment_frames)
        elif ext in IMAGE_EXTENSIONS or ext in (".heic", ".heif"):
            yield Segment(path, [load_photo(path)], "photo")
        else:
            raise MediaError(f"{path.name}: unsupported file type (use photos {sorted(IMAGE_EXTENSIONS)} "
                             f"or videos {sorted(VIDEO_EXTENSIONS)})")
