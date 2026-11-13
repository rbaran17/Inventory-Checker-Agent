"""Headless use: media loading, the InventoryCheck engine, the `check` CLI and the MCP server.

A fake detector stands in for the model, so nothing here calls a provider or spends credits.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path

import pytest
from openpyxl import load_workbook
from PIL import Image

from inventory_checker import InventoryCheck
from inventory_checker.detection import Attribute, DetectedObject, SceneResult
from inventory_checker.media import MediaError, load_photo, segments, video_segments

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


def make_video(path: Path, seconds: int = 5, fps: int = 10) -> Path:
    import av

    with av.open(str(path), "w") as out:
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
    assert len(out["scans"]) == 2 and out["summary"]["verified"] == 1
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
