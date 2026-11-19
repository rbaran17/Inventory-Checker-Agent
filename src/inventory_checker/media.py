"""Turn photos and videos on disk into the frames a scan sends to the model.

Photos become one frame each, like a phone close-up: orientation fixed from EXIF, downscaled to
``PHOTO_EDGE`` on the long side, JPEG. Videos become segments of ``segment_frames`` frames taken every
``every_seconds``, downscaled to ``VIDEO_EDGE``, like the phone's Scan mode. The sizes match the phone app
so headless runs and phone runs cost and behave the same.

Videos need PyAV (``pip install 'inventory-checker-agent[video]'``) or the ``ffmpeg`` command on PATH.

The files come from whoever is being counted, so they are read defensively: a file is only handed to the
decoders this tool documents whatever its name says, a video may not point at other files or at the network,
and reading stops at the caller's scan limit instead of decoding everything first.
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger("inventory_checker")

PHOTO_EDGE, PHOTO_QUALITY = 2048, 90
VIDEO_EDGE, VIDEO_QUALITY = 1280, 80
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp"}
MAX_SOURCE_PIXELS = 100_000_000  # refuse decompression bombs: checked from the header, before decoding

# What a file may be decoded as, whatever its extension says. Pillow and FFmpeg both pick the decoder from the
# content, so without these a ".jpg" holding EPS reaches Ghostscript, and a ".mp4" holding a DASH or HLS
# playlist makes FFmpeg fetch the URLs or read the other local files the playlist names.
IMAGE_FORMATS = ("JPEG", "PNG", "WEBP", "BMP", "TIFF", "GIF")  # JPEG also opens MPO, some cameras' multi-picture JPEG
VIDEO_FORMATS = "mov,matroska,avi"  # FFmpeg demuxers behind .mp4 .mov .m4v .3gp / .mkv .webm / .avi
VIDEO_PROTOCOLS = "file"            # the video itself, from disk; never http, concat, subfile, ...

MIN_EVERY_SECONDS, MAX_EVERY_SECONDS = 0.1, 3600.0  # time between frames taken from a video
MAX_SEGMENT_FRAMES = 6            # frames in one scan (detection.MAX_FRAMES)
MAX_FFMPEG_FRAMES = 6000          # frames the ffmpeg fallback takes from one video (100 minutes at one a second)
FFMPEG_TIMEOUT_SECONDS = 600

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
        with Image.open(path, formats=IMAGE_FORMATS) as img:
            # Pillow itself only refuses at twice MAX_IMAGE_PIXELS, and only warns below that. The size comes
            # from the header, so an oversized image is refused here without being decoded.
            width, height = img.size
            if width * height > MAX_SOURCE_PIXELS:
                raise MediaError(f"{path.name}: the image is {width} x {height} pixels, over the limit of "
                                 f"{MAX_SOURCE_PIXELS // 1_000_000} megapixels. Export a smaller copy.")
            img.seek(0)
            return _jpeg(img, edge, quality), "image/jpeg"
    except FileNotFoundError as exc:
        raise MediaError(f"{path}: file not found") from exc
    except UnidentifiedImageError as exc:
        raise MediaError(f"{path.name}: not a readable image. Photos must be JPEG, PNG, WebP, BMP, TIFF or GIF, "
                         "whatever the file is called.") from exc
    except (OSError, Image.DecompressionBombError) as exc:
        raise MediaError(f"{path.name}: not a readable image ({exc})") from exc


def check_video_options(every_seconds: float, segment_frames: int) -> None:
    """Refuse frame intervals and segment sizes outside the supported range (also NaN and infinity)."""
    if not MIN_EVERY_SECONDS <= every_seconds <= MAX_EVERY_SECONDS:
        raise MediaError(f"The time between video frames (every_seconds, --every) must be between "
                         f"{MIN_EVERY_SECONDS:g} and {MAX_EVERY_SECONDS:g} seconds")
    if not 1 <= segment_frames <= MAX_SEGMENT_FRAMES:
        raise MediaError(f"A video scan takes 1 to {MAX_SEGMENT_FRAMES} frames (segment_frames, --segment-frames)")


def _video_frames_pyav(path: Path, every: float) -> Iterator[tuple[float, Image.Image]]:
    import av  # optional dependency

    # An absolute path is always read as a file, never as "protocol:rest".
    with av.open(str(path.absolute()), options={"protocol_whitelist": VIDEO_PROTOCOLS,
                                                "format_whitelist": VIDEO_FORMATS}) as container:
        stream = container.streams.video[0]
        next_t = 0.0
        for frame in container.decode(stream):
            if frame.time is None or frame.time + 1e-6 < next_t:
                continue
            yield frame.time, frame.to_image()
            next_t = frame.time + every


def _video_frames_ffmpeg(path: Path, every: float, max_frames: int | None) -> Iterator[tuple[float, Image.Image]]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise MediaError("Reading videos needs PyAV (pip install 'inventory-checker-agent[video]') or ffmpeg on PATH")
    # ffmpeg writes every frame before the first one is used, so it is told where to stop: at the caller's
    # limit, and never later than one frame past the ceiling, which is how a video that is too long is
    # noticed (and refused, rather than silently cut short).
    wanted = MAX_FFMPEG_FRAMES + 1 if max_frames is None else min(max_frames, MAX_FFMPEG_FRAMES + 1)
    with tempfile.TemporaryDirectory(prefix="ic-frames-") as tmp:
        cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
               # Input options: they must come before -i to apply to the file being read.
               "-protocol_whitelist", VIDEO_PROTOCOLS, "-format_whitelist", VIDEO_FORMATS,
               "-i", str(path.absolute()), "-vf", f"fps=1/{every}", "-frames:v", str(wanted),
               "-q:v", "3", str(Path(tmp) / "f%06d.jpg")]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=False,
                                  stdin=subprocess.DEVNULL, timeout=FFMPEG_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise MediaError(f"{path.name}: ffmpeg was stopped after {FFMPEG_TIMEOUT_SECONDS} seconds. Use a "
                             "shorter video, or scan fewer segments of it.") from exc
        if done.returncode != 0:
            raise MediaError(f"{path.name}: ffmpeg could not read the video ({done.stderr.strip()[:200]})")
        files = sorted(Path(tmp).glob("f*.jpg"))
        if len(files) > MAX_FFMPEG_FRAMES:
            raise MediaError(f"{path.name}: more than {MAX_FFMPEG_FRAMES} frames at one every {every:g} seconds. "
                             "Use a longer interval, a shorter video, or a scan limit.")
        for i, f in enumerate(files):
            with Image.open(f, formats=("JPEG",)) as img:
                yield i * every, img.copy()


def video_segments(path: str | Path, every_seconds: float = 1.0, segment_frames: int = 4,
                   edge: int = VIDEO_EDGE, quality: int = VIDEO_QUALITY, *, limit: int | None = None
                   ) -> Iterator[Segment]:
    """Frames every ``every_seconds``, grouped into segments of ``segment_frames`` (the last may be shorter).

    The video is decoded as segments are asked for, and no further than ``limit`` segments when it is given.
    """
    path = Path(path)
    if not path.is_file():
        raise MediaError(f"{path}: file not found")
    check_video_options(every_seconds, segment_frames)
    if limit is not None and limit <= 0:
        return
    try:
        import av  # noqa: F401
        frames = _video_frames_pyav(path, every_seconds)
    except ImportError:
        frames = _video_frames_ffmpeg(path, every_seconds, None if limit is None else limit * segment_frames)
    batch: list[Frame] = []
    start = 0.0
    made = 0
    try:
        for t, img in frames:
            if not batch:
                start = t
            batch.append((_jpeg(img, edge, quality), "image/jpeg"))
            if len(batch) == segment_frames:
                yield Segment(path, batch, f"{_clock(start)}-{_clock(t)}")
                batch = []
                made += 1
                if limit is not None and made >= limit:
                    return
    except MediaError:
        raise
    except Exception as exc:  # PyAV raises its own error types for broken or unsupported files
        raise MediaError(f"{path.name}: could not read the video ({exc})") from exc
    finally:
        frames.close()  # stop decoding (and remove ffmpeg's frames) as soon as the caller has enough
    if batch:
        yield Segment(path, batch, f"{_clock(start)}-end")


def _clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def expand(paths: Iterable[str | Path]) -> list[Path]:
    """Files given directly, plus images and videos inside any folders given (sorted, not recursive).

    A folder only yields what is really inside it: a link that leads out of the folder is skipped, so scanning
    a folder cannot read (and send to the model) a file somewhere else on the disk.
    """
    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            root = p.resolve()
            for f in sorted(p.iterdir()):
                if f.suffix.lower() not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS or not f.is_file():
                    continue
                if root not in f.resolve().parents:
                    log.warning("Skipped %s: it is a link to a file outside %s", f, p)
                    continue
                out.append(f)
        elif p.exists():
            out.append(p)
        else:
            raise MediaError(f"{p}: file or folder not found")
    return out


def _file_segments(path: Path, every_seconds: float, segment_frames: int, limit: int | None) -> Iterator[Segment]:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        yield from video_segments(path, every_seconds, segment_frames, limit=limit)
    elif ext in IMAGE_EXTENSIONS or ext in (".heic", ".heif"):
        yield Segment(path, [load_photo(path)], "photo")
    else:
        raise MediaError(f"{path.name}: unsupported file type (use photos {sorted(IMAGE_EXTENSIONS)} "
                         f"or videos {sorted(VIDEO_EXTENSIONS)})")


def segments(paths: Iterable[str | Path], every_seconds: float = 1.0, segment_frames: int = 4, *,
             limit: int | None = None) -> Iterator[Segment]:
    """Every scan to run for the given files and folders, in order.

    A file is read when its scans are asked for, so a caller that stops early, or passes ``limit``, does not
    pay for decoding the rest.
    """
    check_video_options(every_seconds, segment_frames)
    left = limit
    for path in expand(paths):
        if left is not None and left <= 0:
            return
        for seg in _file_segments(path, every_seconds, segment_frames, left):
            yield seg
            if left is not None:
                left -= 1


def plan(paths: Iterable[str | Path], max_scans: int, every_seconds: float = 1.0, segment_frames: int = 4
         ) -> tuple[list[Segment], int]:
    """The first ``max_scans`` scans for the given files and folders, and how many files were left over.

    A file is left over when it was not read at all, or (a video) only in part. Reading stops at the cap: of
    what lies beyond it, only the one video segment that shows a video goes on is decoded.
    """
    check_video_options(every_seconds, segment_frames)
    files = expand(paths)
    planned: list[Segment] = []
    for n, path in enumerate(files):
        room = max_scans - len(planned)
        if room <= 0:
            return planned, len(files) - n
        with closing(_file_segments(path, every_seconds, segment_frames, room + 1)) as found:
            for seg in found:
                if len(planned) >= max_scans:
                    return planned, len(files) - n
                planned.append(seg)
    return planned, 0
