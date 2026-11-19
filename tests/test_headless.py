"""Headless use: media loading, the InventoryCheck engine, the `check` CLI and the MCP server.

A fake detector stands in for the model, so nothing here calls a provider or spends credits.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook
from PIL import Image

from inventory_checker import InventoryCheck, detection
from inventory_checker import media as media_module
from inventory_checker.detection import Attribute, DetectedObject, SceneResult
from inventory_checker.media import MediaError, expand, load_photo, plan, segments, video_segments

LISTING = ("item_id,description,expected_qty,location,serial_number,model_number\n"
           "A-1,Laptop computer,1,Office,C02XK1ZZJG5H,A2337\n"
           "B-1,Coffee mug,2,Kitchen,,\n")
CALLS: list[int] = []


async def fake_detector(frames, sample):
    """Laptop with a matching serial, one mug (expected 2), and a duck that is not listed."""
    CALLS.append(len(frames))
    return SceneResult(objects=[
        DetectedObject(name="laptop", count=1, confidence=0.95, matched_item_id="A-1",
                       attributes=[Attribute(name="Serial number", value="C02X-K1ZZ-JG5H"),
                                   Attribute(name="Model number", value="A2337")]),
        DetectedObject(name="coffee mug", count=1, confidence=0.9, matched_item_id="B-1"),
        DetectedObject(name="rubber duck", count=1, confidence=0.6),
    ], image_quality="good")


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    CALLS.clear()
    (tmp_path / "items.csv").write_text(LISTING)
    photos = tmp_path / "photos"
    photos.mkdir()
    # A tall photo stored sideways with an EXIF rotation, like phones do.
    img = Image.new("RGB", (4000, 3000), "white")
    exif = img.getexif()
    exif[0x0112] = 6  # rotate 90 degrees when displayed
    img.save(photos / "desk.jpg", exif=exif.tobytes())
    Image.new("RGB", (800, 600), "gray").save(photos / "shelf.png")
    (photos / "notes.txt").write_text("ignored")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def make_video(path: Path, seconds: int = 5, fps: int = 10, container: str | None = None) -> Path:
    import av

    with av.open(str(path), "w", format=container) as out:
        stream = out.add_stream("mpeg4", rate=fps)
        stream.width, stream.height, stream.pix_fmt = 320, 240, "yuv420p"
        for i in range(seconds * fps):
            frame = av.VideoFrame.from_image(Image.new("RGB", (320, 240), (i * 5 % 255, 80, 160)))
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)
    return path


# ----------------------------------------------------------------------------- media
def test_photo_is_rotated_downscaled_and_jpeg(workdir):
    data, media_type = load_photo(workdir / "photos" / "desk.jpg")
    with Image.open(io.BytesIO(data)) as img:
        assert img.format == "JPEG" and media_type == "image/jpeg"
        assert img.size == (1536, 2048)  # EXIF rotation applied, long edge 2048


def test_folder_expansion_skips_other_files_and_rejects_missing(workdir):
    segs = list(segments([workdir / "photos"]))
    assert [s.source.name for s in segs] == ["desk.jpg", "shelf.png"] and all(s.kind == "photo" for s in segs)
    with pytest.raises(MediaError, match="not found"):
        list(segments([workdir / "nope.jpg"]))
    with pytest.raises(MediaError, match="HEIC"):
        (workdir / "x.heic").write_bytes(b"x")
        list(segments([workdir / "x.heic"]))


def test_video_becomes_segments(workdir):
    video = make_video(workdir / "pan.mp4", seconds=5)
    segs = list(video_segments(video, every_seconds=1.0, segment_frames=4))
    assert [len(s.frames) for s in segs] == [4, 1]
    assert segs[0].label == "0:00-0:03" and segs[0].kind == "video"


def test_video_falls_back_to_ffmpeg(workdir, monkeypatch):
    import builtins
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    video = make_video(workdir / "pan.mp4", seconds=4)
    real_import = builtins.__import__

    def no_av(name, *a, **k):
        if name == "av":
            raise ImportError("no av")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_av)
    segs = list(video_segments(video, every_seconds=2.0, segment_frames=4))
    assert [len(s.frames) for s in segs] == [2]


# ----------------------------------------------------------------------------- media from an untrusted source
def any_video(path: Path, seconds: int = 3, container: str | None = None) -> Path:
    """A small real video, written with PyAV or, where only the ffmpeg command is installed, with that."""
    try:
        return make_video(path, seconds, container=container)
    except ImportError:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"testsrc=duration={seconds}:size=320x240:rate=10", "-c:v", "mpeg4",
                        *(["-f", container] if container else []), str(path)], check=True, stdin=subprocess.DEVNULL)
        return path


def without_pyav(monkeypatch) -> None:
    """Make `import av` fail, so videos go through the ffmpeg command."""
    import builtins

    real_import = builtins.__import__

    def no_av(name, *a, **k):
        if name == "av":
            raise ImportError("no av")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_av)


@pytest.fixture(params=["pyav", "ffmpeg"])
def video_reader(request):
    """Run a test once per way of reading videos, skipping the ones this machine does not have."""
    if request.param == "pyav":
        pytest.importorskip("av", reason="PyAV not installed")
    elif not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    return request.param


@pytest.fixture
def listener():
    """A port on 127.0.0.1 that records whatever connects to it."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    server.settimeout(0.1)
    hits: list[bytes] = []
    done = threading.Event()

    def accept() -> None:
        while not done.is_set():
            try:
                conn, _ = server.accept()
            except OSError:  # timeout, or closed
                continue
            conn.settimeout(0.5)
            try:
                hits.append(conn.recv(200))
            except OSError:
                hits.append(b"(connected)")
            conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield SimpleNamespace(port=server.getsockname()[1], hits=hits)
    done.set()
    thread.join(2)
    server.close()


DASH_MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" profiles="urn:mpeg:dash:profile:isoff-on-demand:2011" type="static"
     mediaPresentationDuration="PT3S" minBufferTime="PT1S">
 <Period><AdaptationSet mimeType="video/mp4">
  <Representation id="1" bandwidth="100000" codecs="avc1.64000c" width="320" height="240">
   <BaseURL>{url}</BaseURL><SegmentBase/></Representation></AdaptationSet></Period>
</MPD>
"""
HLS_PLAYLIST = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\n{url}\n#EXT-X-ENDLIST\n"


def test_video_file_cannot_send_the_decoder_to_urls_or_other_files(workdir, listener, video_reader, monkeypatch):
    """FFmpeg chooses how to read a file from its content. A playlist named clip.mp4 made it fetch the URLs in
    the playlist, or decode another video on the disk whose frames then went to the model."""
    private = any_video(workdir / "private.mp4")           # stands for a video outside the folder being scanned
    stream = any_video(workdir / "stream.mp4", container="mpegts")  # a real video, but not a documented container
    real = any_video(workdir / "real.mp4")
    url = f"http://127.0.0.1:{listener.port}/private.mp4"
    clips = {
        "dash_url.mp4": DASH_MANIFEST.format(url=url),
        "dash_file.mp4": DASH_MANIFEST.format(url=private.resolve().as_uri()),
        "hls_url.mp4": HLS_PLAYLIST.format(url=url),
        "hls_file.mov": HLS_PLAYLIST.format(url=private.resolve()),
        "concat.mkv": f"ffconcat version 1.0\nfile '{private.resolve()}'\n",
    }
    for name, text in clips.items():
        (workdir / name).write_text(text, encoding="utf-8")
    if video_reader == "ffmpeg":
        without_pyav(monkeypatch)

    for name in [*clips, stream.name]:
        with pytest.raises(MediaError):
            list(video_segments(workdir / name))
    time.sleep(0.5)  # a connection made just before the decoder gave up would still be on its way
    assert listener.hits == []
    assert [len(s.frames) for s in video_segments(real)] == [3]  # ordinary videos are still read


def test_ffmpeg_command_is_confined_bounded_and_timed(workdir, monkeypatch):
    """Checked on the command itself, so it holds on machines without ffmpeg too."""
    video = workdir / "clip.mp4"
    video.write_bytes(b"stand-in")
    without_pyav(monkeypatch)
    monkeypatch.setattr(media_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 1, "", "no such luck")

    monkeypatch.setattr(media_module.subprocess, "run", fake_run)
    with pytest.raises(MediaError, match="no such luck"):
        list(video_segments(video, every_seconds=2.0, segment_frames=4, limit=3))
    cmd, kw = calls[0]
    source = cmd.index("-i")
    assert cmd[source + 1] == str(video.absolute())  # absolute: never read as "protocol:rest"
    for option, value in (("-protocol_whitelist", "file"), ("-format_whitelist", "mov,matroska,avi")):
        assert cmd[cmd.index(option) + 1] == value and cmd.index(option) < source, option  # input options
    assert cmd[cmd.index("-frames:v") + 1] == "12" and cmd.index("-frames:v") > source  # 3 scans of 4 frames
    assert "-nostdin" in cmd and kw["stdin"] == subprocess.DEVNULL  # stdin may be the MCP protocol stream
    assert kw["timeout"] == media_module.FFMPEG_TIMEOUT_SECONDS

    calls.clear()
    with pytest.raises(MediaError):
        list(video_segments(video))  # no limit from the caller: one frame past the ceiling, to notice excess
    assert calls[0][0][calls[0][0].index("-frames:v") + 1] == str(media_module.MAX_FFMPEG_FRAMES + 1)

    def slow_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(media_module.subprocess, "run", slow_run)
    with pytest.raises(MediaError, match="stopped after"):
        list(video_segments(video))


def test_scan_limit_stops_the_decoding(workdir, video_reader, monkeypatch):
    """--max-scans / max_scans used to be applied after every frame of every file had been decoded and kept."""
    video = any_video(workdir / "long.mp4", seconds=30)  # 30 frames at one a second: 8 scans
    if video_reader == "ffmpeg":
        without_pyav(monkeypatch)
    encoded = []
    real_jpeg = media_module._jpeg
    monkeypatch.setattr(media_module, "_jpeg", lambda *a: encoded.append(1) or real_jpeg(*a))

    planned, left = plan([workdir / "photos", video], max_scans=4)
    assert [s.source.name for s in planned] == ["desk.jpg", "shelf.png", "long.mp4", "long.mp4"] and left == 1
    assert len(encoded) == 2 + 3 * 4  # two photos, two video scans, and the one scan that shows there is more
    encoded.clear()
    assert plan([workdir / "photos", video], max_scans=1) == (planned[:1], 2) and len(encoded) == 1
    assert [len(x.frames) for x in plan([workdir / "photos", video], max_scans=50)[0]] == [1, 1, 4, 4, 4, 4, 4, 4, 4, 2]
    assert plan([workdir / "photos"], max_scans=2)[1] == 0 and plan([workdir / "photos"], max_scans=0) == ([], 2)

    encoded.clear()
    assert len(list(segments([video], limit=2))) == 2 and len(encoded) == 8
    check = InventoryCheck.demo(detector=fake_detector)
    encoded.clear()
    assert len(check.check_paths([video], max_scans=3)) == 3 and len(encoded) == 12


def test_video_sampling_options_are_range_checked(workdir, monkeypatch, capsys):
    video = workdir / "clip.mp4"
    video.write_bytes(b"never opened")
    for every in (0.01, 0, -1, float("nan"), float("inf"), 1e9):
        with pytest.raises(MediaError, match="between 0.1 and 3600 seconds"):
            list(video_segments(video, every_seconds=every))
        with pytest.raises(MediaError, match="between 0.1 and 3600 seconds"):
            plan([workdir / "photos"], 5, every_seconds=every)  # refused up front, even with no video given
    for count in (0, -4, detection.MAX_FRAMES + 1, 1000):
        with pytest.raises(MediaError, match="1 to 6 frames"):
            list(segments([video], segment_frames=count))
    assert media_module.MAX_SEGMENT_FRAMES == detection.MAX_FRAMES
    assert run_cli("check", "--demo", "photos", "--every", "0.01", "--dry-run") == 2
    assert "between 0.1 and 3600 seconds" in capsys.readouterr().err

    from inventory_checker.mcp_server import build_server

    server = build_server(allowed_dir=workdir, detector=fake_detector)
    call(server, "load_demo_listing")
    with pytest.raises(RuntimeError, match="between 0.1 and 3600 seconds"):
        call(server, "check_media", paths=["photos"], every_seconds=0.01)
    with pytest.raises(RuntimeError, match="1 to 6 frames"):
        call(server, "check_media", paths=["photos"], segment_frames=600)
    assert CALLS == []  # nothing was scanned


def test_oversized_photo_is_refused_from_its_header(workdir, monkeypatch):
    """Pillow only refuses at twice its limit: a 580 KB PNG of 14,100 x 14,100 pixels was decoded at 1.7 GB."""
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", Image.MAX_IMAGE_PIXELS)  # load_photo sets it: put it back after
    monkeypatch.setattr(media_module, "MAX_SOURCE_PIXELS", 150 * 100)
    monkeypatch.setattr(media_module, "_jpeg", lambda *a: pytest.fail("the image was decoded"))
    Image.new("L", (200, 100)).save(workdir / "wide.png")       # 20,000 pixels: over the limit, under twice it
    Image.new("L", (400, 100)).save(workdir / "wider.png")      # over twice the limit
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        with pytest.raises(MediaError, match="200 x 100 pixels, over the limit"):
            load_photo(workdir / "wide.png")
        with pytest.raises(MediaError, match="wider.png"):
            load_photo(workdir / "wider.png")


def test_photo_is_only_decoded_as_a_documented_format(workdir):
    """Pillow picks the decoder from the content: a .jpg holding EPS was handed to the Ghostscript launcher."""
    eps = (b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n%%EndComments\n"
           b"0 0 moveto 10 10 lineto stroke\nshowpage\n%%EOF\n")
    (workdir / "label.jpg").write_bytes(eps)
    Image.new("RGB", (40, 30), "red").save(workdir / "scan.png", "PPM")  # readable by Pillow, not documented here
    for name in ("label.jpg", "scan.png"):
        with pytest.raises(MediaError, match="must be JPEG, PNG, WebP, BMP, TIFF or GIF"):
            load_photo(workdir / name)
    for name, fmt in (("a.jpg", "JPEG"), ("a.jpeg", "MPO"), ("a.png", "PNG"), ("a.webp", "WEBP"), ("a.bmp", "BMP"),
                      ("a.tif", "TIFF"), ("a.gif", "GIF"), ("misnamed.png", "JPEG")):
        Image.new("RGB", (40, 30), "red").save(workdir / name, fmt)
        assert load_photo(workdir / name)[1] == "image/jpeg", name


def test_folder_scan_does_not_follow_links_out_of_the_folder(workdir, caplog):
    """The allowed folder is checked for the path that was named, so its contents must not lead elsewhere."""
    outside = workdir / "outside"
    outside.mkdir()
    Image.new("RGB", (64, 48), (200, 10, 10)).save(outside / "private.png")
    (outside / "more").mkdir()
    Image.new("RGB", (64, 48), (10, 10, 200)).save(outside / "more" / "deep.png")
    photos = workdir / "photos"
    try:
        os.symlink(outside / "private.png", photos / "innocent.png")   # e.g. unpacked from an archive
        os.symlink(outside / "more", photos / "album.png", target_is_directory=True)
        os.symlink(photos / "shelf.png", photos / "same_folder.png")   # stays inside: still read
        os.symlink(outside / "missing.png", photos / "dangling.png")
    except (OSError, NotImplementedError):
        pytest.skip("this platform or account cannot create symbolic links")

    with caplog.at_level("WARNING", logger="inventory_checker"):
        assert [p.name for p in expand([photos])] == ["desk.jpg", "same_folder.png", "shelf.png"]
    assert "innocent.png" in caplog.text and "outside" in caplog.text  # skipped, and said so
    assert [s.source.name for s in segments([photos])] == ["desk.jpg", "same_folder.png", "shelf.png"]

    from inventory_checker.mcp_server import build_server

    seen = []

    async def detector(frames, sample):
        seen.append(frames[0][0])
        return SceneResult()

    server = build_server(allowed_dir=workdir / "photos", detector=detector)
    call(server, "load_demo_listing")
    assert len(call(server, "check_media", paths=["."])["scans"]) == 3
    assert load_photo(outside / "private.png")[0] not in seen and len(seen) == 3
    with pytest.raises(RuntimeError, match="outside the allowed folder"):
        call(server, "check_media", paths=["innocent.png"])  # naming the link itself was already refused
    tools = {t.name: t for t in InventoryCheck.demo(detector=detector).as_tools(allowed_dir=photos)}
    seen.clear()
    asyncio.run(tools["check_media"].invoke(paths=["."]))
    assert load_photo(outside / "private.png")[0] not in seen and len(seen) == 3


# ----------------------------------------------------------------------------- engine
def test_engine_end_to_end(workdir):
    check = InventoryCheck.from_file("items.csv", detector=fake_detector)
    make_video(workdir / "pan.mp4", seconds=4)
    scans = check.check_paths(["photos", "pan.mp4"])
    assert [s["kind"] for s in scans] == ["photo", "photo", "video"] and CALLS == [1, 1, 4]
    assert scans[2]["source"] == "pan.mp4 0:00-0:03"
    rows = {r["item_id"]: r for r in check.results()["items"]}
    assert rows["A-1"]["status"] == "verified"
    assert rows["B-1"]["status"] == "exception"  # 1 mug counted, 2 expected

    check.review("B-1", "follow_up", "second mug in dishwasher")
    check.set_match("S001", "3", "B-1")  # the duck is actually the second mug
    assert check.scan("S001")["objects"][2]["mapped_by"] == "you"
    check.remove_object("S002", "3")
    check.remove_scan("S003")
    with pytest.raises(KeyError):
        check.set_match("S001", "1", "NOPE")

    wb = load_workbook(io.BytesIO(check.export(workdir / "r.xlsx").read_bytes()))
    assert "Results" in wb.sheetnames
    assert check.export(workdir / "r.csv").read_text().startswith("Item ID,")
    data = json.loads(check.export(workdir / "r.json").read_text())
    assert data["results"]["summary"]["total"] == 2 and data["listing"] == "items.csv"
    with pytest.raises(ValueError, match="xlsx"):
        check.export(workdir / "r.pdf")


def test_engine_without_provider_explains(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "AZURE_OPENAI_ENDPOINT", "OPENAI_API_KEY", "DETECTOR", "AI_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    check = InventoryCheck.demo()
    assert not check.ai_ready
    with pytest.raises(RuntimeError, match="No AI provider"):
        check.check_frames([(b"x", "image/jpeg")])


def test_engine_sync_call_inside_event_loop_is_refused():
    check = InventoryCheck.demo(detector=fake_detector)

    async def inner():
        with pytest.raises(RuntimeError, match="async methods"):
            check.check_frames([(b"x", "image/jpeg")])
        return await check.acheck_frames([(b"x", "image/jpeg")])

    assert asyncio.run(inner())["id"] == "S001"


def test_engine_as_maf_tools(workdir):
    check = InventoryCheck.from_file("items.csv", detector=fake_detector)
    tools = {t.name: t for t in check.as_tools(allowed_dir=workdir)}
    assert set(tools) == {"check_media", "get_results", "review_item", "export_results"}
    out = json.loads(asyncio.run(tools["check_media"].invoke(paths=["photos/shelf.png"]))[0].text)
    assert out["summary"]["total"] == 2
    with pytest.raises(Exception, match="outside"):
        asyncio.run(tools["export_results"].invoke(path="/etc/x.json"))


# ----------------------------------------------------------------------------- CLI
def run_cli(*argv):
    from inventory_checker.cli import main

    try:
        main(list(argv))
    except SystemExit as exc:
        return exc.code
    return 0


def test_cli_check(workdir, monkeypatch, capsys):
    monkeypatch.setenv("DETECTOR", "tests.test_headless:fake_detector")
    code = run_cli("check", "--listing", "items.csv", "photos", "-o", "out.xlsx", "-o", "out.csv",
                   "--client", "Acme", "--json")
    out = capsys.readouterr()
    assert code == 0
    assert (workdir / "out.xlsx").exists() and (workdir / "out.csv").exists()
    data = json.loads(out.out)
    assert data["details"]["client"] == "Acme" and len(data["scans"]) == 2
    assert "1 verified" in out.err and "S001 desk.jpg photo" in out.err
    assert run_cli("check", "-l", "items.csv", "photos", "--fail-on", "exception", "-q") == 1
    assert run_cli("check", "-l", "items.csv", "photos/shelf.png", "--max-scans", "1", "--fail-on", "never") == 0


def test_cli_dry_run_needs_no_provider(workdir, monkeypatch, capsys):
    for k in ("ANTHROPIC_API_KEY", "AZURE_OPENAI_ENDPOINT", "OPENAI_API_KEY", "DETECTOR", "AI_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    make_video(workdir / "pan.mp4", seconds=9)
    assert run_cli("check", "--demo", "photos", "pan.mp4", "--dry-run") == 0
    err = capsys.readouterr().err
    assert "Dry run: 5 model call(s)" in err and "pan.mp4 0:04-0:07 (4 frames)" in err
    assert run_cli("check", "--demo", "photos") == 2  # real run without a provider
    assert "No AI provider" in capsys.readouterr().err


def test_cli_errors(workdir, capsys):
    assert run_cli("check", "-l", "missing.csv", "photos") == 2
    assert run_cli("check", "--demo", "nope/") == 2
    assert run_cli("check", "--demo", "photos", "--segment-frames", "9") == 2
    assert "not found" in capsys.readouterr().err


# ----------------------------------------------------------------------------- MCP server
def call(server, name, **args):
    try:
        res = asyncio.run(server.call_tool(name, args))
    except Exception as exc:  # the in-process API raises tool errors; over the wire they are is_error results
        raise RuntimeError(str(exc)) from exc
    if res.is_error:
        raise RuntimeError(res.content[0].text)
    return res.structured_content.get("result", res.structured_content) if res.structured_content else \
        json.loads(res.content[0].text)


def test_mcp_server_flow(workdir):
    from inventory_checker.mcp_server import build_server

    server = build_server(allowed_dir=workdir, detector=fake_detector, ai_label="fake")
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"load_listing", "check_media", "check_image", "get_results", "review_item", "export_results"} <= names

    with pytest.raises(RuntimeError, match="No listing loaded"):
        call(server, "get_results")
    listing = call(server, "load_listing", path="items.csv")
    assert listing["items"] == 2 and listing["attribute_names"] == ["Serial number", "Model number"]
    assert listing["detection"] == "fake"

    out = call(server, "check_media", paths=["photos"])
    assert len(out["scans"]) == 2 and out["summary"]["verified"] == 1 and out["skipped"] == 0
    assert out["scans"][0]["objects"][0]["attributes"]["Serial number"] == "C02X-K1ZZ-JG5H"

    png = io.BytesIO()
    Image.new("RGB", (10, 10)).save(png, "PNG")
    scan = call(server, "check_image", data_base64=base64.b64encode(png.getvalue()).decode(), media_type="image/png")
    assert scan["scan"]["id"] == "S003"

    exc = call(server, "get_results", status="exception")
    assert [r["item_id"] for r in exc["items"]] == ["B-1"] and exc["not_on_listing"]
    call(server, "review_item", item_id="B-1", decision="accepted", note="ok")
    call(server, "set_match", scan_id="S001", object_id="3", item_id=None)
    call(server, "remove_scan", scan_id="S003")
    written = call(server, "export_results", path="results.xlsx")
    assert Path(written["path"]).exists()

    with pytest.raises(RuntimeError, match="outside the allowed folder"):
        call(server, "export_results", path="../escape.json")
    with pytest.raises(RuntimeError, match="outside the allowed folder"):
        call(server, "load_listing", path="/etc/passwd")
    with pytest.raises(RuntimeError, match="base64"):
        call(server, "check_image", data_base64="***")
    with pytest.raises(RuntimeError, match="not found"):
        call(server, "remove_scan", scan_id="S999")


def test_mcp_check_media_stops_at_max_scans(workdir, monkeypatch):
    from inventory_checker import mcp_server

    server = mcp_server.build_server(allowed_dir=workdir, detector=fake_detector)
    call(server, "load_demo_listing")
    out = call(server, "check_media", paths=["photos"], max_scans=1)
    assert len(out["scans"]) == 1 and out["skipped"] == 1 and CALLS == [1]  # one file was left unscanned
    assert call(server, "check_media", paths=["photos"], max_scans=0) == {
        "scans": [], "skipped": 2, "errors": [], "summary": out["summary"]}
    # However large the number asked for, one call prepares at most MAX_SCANS_PER_CALL scans.
    monkeypatch.setattr(mcp_server, "MAX_SCANS_PER_CALL", 1)
    out = call(server, "check_media", paths=["photos"], max_scans=10**9)
    assert len(out["scans"]) == 1 and out["skipped"] == 1


def test_cli_says_what_max_scans_left_out(workdir, monkeypatch, capsys):
    monkeypatch.setenv("DETECTOR", "tests.test_headless:fake_detector")
    assert run_cli("check", "--demo", "photos", "--max-scans", "1") == 0
    err = capsys.readouterr().err
    assert "only the first 1 scan(s) will run (--max-scans). 1 file(s) were not read" in err and CALLS == [1]
    assert run_cli("check", "--demo", "photos", "--max-scans", "1", "--dry-run") == 0
    err = capsys.readouterr().err
    assert "Dry run: 1 model call(s)" in err and "1 file(s) were not read" in err


def test_mcp_demo_and_inline_csv(workdir):
    from inventory_checker.mcp_server import build_server

    server = build_server(allowed_dir=workdir, detector=fake_detector)
    assert call(server, "load_demo_listing")["items"] == 12
    assert call(server, "load_listing_csv", csv_text=LISTING)["items"] == 2
    details = call(server, "set_details", client="Acme", counter="R. Auditor")
    assert details["client"] == "Acme"


# ----------------------------------------------------------------------------- trying it with no API key


def _no_provider(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "DETECTOR", "AI_PROVIDER"):
        monkeypatch.delenv(name, raising=False)


def test_cli_offline_runs_end_to_end_without_a_key(workdir, monkeypatch, capsys):
    from inventory_checker import cli

    _no_provider(monkeypatch)
    out = workdir / "offline.xlsx"
    args = ["check", "--listing", str(workdir / "items.csv"), str(workdir / "photos"), "--offline", "--json",
            "--out", str(out)]
    with pytest.raises(SystemExit) as done:
        cli.main(args)
    captured = capsys.readouterr()
    assert done.value.code == 0
    data = json.loads(captured.out)
    assert "offline stand-in" in data["detection"] and data["errors"] == [] and data["scans"]
    assert "does not look at the images" in captured.err
    assert "offline stand-in" in load_workbook(out)["Summary"]["B8"].value  # the workbook says so too


def test_cli_without_key_points_to_offline(workdir, monkeypatch, capsys):
    from inventory_checker import cli

    _no_provider(monkeypatch)
    with pytest.raises(SystemExit) as done:
        cli.main(["check", "--listing", str(workdir / "items.csv"), str(workdir / "photos")])
    assert done.value.code == 2 and "--offline" in capsys.readouterr().err


def test_cli_detector_in_current_folder_and_plain_errors(workdir, monkeypatch, capsys):
    import sys

    from inventory_checker import cli

    _no_provider(monkeypatch)
    (workdir / "my_detector.py").write_text(
        "from inventory_checker.detection import SceneResult\n"
        "async def detect(frames, sample):\n    return SceneResult(notes='mine')\n", encoding="utf-8")
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p not in ("", str(workdir))])
    base = ["check", "--listing", "items.csv", "photos", "-q"]

    with pytest.raises(SystemExit) as done:
        cli.main([*base, "--detector", "my_detector:detect", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert done.value.code == 0 and data["detection"] == "custom: my_detector:detect"
    assert data["scans"][0]["notes"] == "mine"

    for spec, text in [("no_such_module:detect", "cannot import"), ("my_detector:nope", "has no 'nope'"),
                       ("my_detector", "module:function")]:
        with pytest.raises(SystemExit) as done:
            cli.main([*base, "--detector", spec])
        err = capsys.readouterr().err
        assert done.value.code == 2 and text in err and "Traceback" not in err, spec

    monkeypatch.setenv("DETECTOR", "no_such_module:detect")  # the setting gets the same plain message
    with pytest.raises(SystemExit) as done:
        cli.main(base)
    assert done.value.code == 2 and "cannot import" in capsys.readouterr().err


def test_cli_with_nothing_to_scan_says_so(workdir, monkeypatch, capsys):
    from inventory_checker import cli

    _no_provider(monkeypatch)
    empty = workdir / "empty"
    empty.mkdir()
    (empty / "notes.txt").write_text("not a photo", encoding="utf-8")
    for media in ([], [str(empty)]):
        with pytest.raises(SystemExit) as done:
            cli.main(["check", "--demo", *media, "--offline"])
        assert done.value.code == 2 and "No photos or videos" in capsys.readouterr().err
    with pytest.raises(SystemExit) as done:  # CI smoke-tests the image with exactly this
        cli.main(["check", "--demo", "--dry-run"])
    assert done.value.code == 0 and "0 model call(s)" in capsys.readouterr().err
