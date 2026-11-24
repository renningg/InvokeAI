import base64
import io
import json
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import imageio_ffmpeg
import pytest
import requests
from PIL import Image
from urllib3.response import HTTPResponse

from invokeai.app.services.external_generation import minimax_video as minimax
from invokeai.app.services.external_generation.errors import (
    ExternalProviderNotConfiguredError,
    ExternalProviderRequestError,
)
from invokeai.app.services.session_processor.session_processor_common import CanceledException
from invokeai.app.util.mp4_metadata import read_mp4_tags

_GLOBAL = "https://api.minimax.io"
_CHINA = "https://api.minimax.cn"
_CDN = "https://93.184.216.34/video.mp4?signature=private-test-signature"
_REQUEST = minimax.MiniMaxVideoRequest("MiniMax-H3", "Clouds drift.", "768P", 5, "16:9")
_CREATED = {"task_id": "123"}
_SUCCEEDED = {"task": {"id": "123", "status": "succeeded", "content": {"url": _CDN}}}


def _response(body: dict | bytes, status: int = 200, **headers: str) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers)
    response.raw = HTTPResponse(
        body=io.BytesIO(json.dumps(body).encode() if isinstance(body, dict) else body), preload_content=False
    )
    return response


class MiniMaxHTTP:
    def __init__(self) -> None:
        self.script: list[requests.Response | Exception] = []
        self.calls: list[requests.PreparedRequest] = []

    def send(self, request: requests.PreparedRequest) -> requests.Response:
        self.calls.append(request)
        assert self.script, f"Unexpected {request.method} request"
        result = self.script.pop(0)
        if isinstance(result, Exception):
            raise result
        result.request = request
        result.url = request.url
        return result


@pytest.fixture
def http(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MiniMaxHTTP:
    http = MiniMaxHTTP()
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", lambda _, request, **kwargs: http.send(request))
    monkeypatch.setattr(minimax, "_POLL_INTERVAL", 0)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return http


def _generate(
    request: minimax.MiniMaxVideoRequest = _REQUEST,
    origin: str | None = None,
    is_canceled: Callable[[], bool] = lambda: False,
    **kwargs: Any,
):
    return minimax.generate_minimax_video(
        api_key="test-token", base_url=origin, request=request, is_canceled=is_canceled, **kwargs
    )


@pytest.mark.parametrize(("origin", "api"), [(None, _GLOBAL), (_CHINA + "/", _CHINA)])
def test_downloads_from_both_regions_without_sending_credentials_to_cdn(
    http: MiniMaxHTTP, origin: str | None, api: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    netrc = tmp_path / "netrc"
    netrc.write_text("machine 93.184.216.34 login localuser password local-password")
    monkeypatch.setenv("NETRC", str(netrc))
    http.script = [
        _response(_CREATED),
        _response({"task": {"id": "123", "status": "queued"}}),
        _response({"task": {"id": "123", "status": "queued"}}),
        _response({}, status=503),
        requests.Timeout(),
        _response({"task": {"id": "123", "status": "running"}}),
        _response(_SUCCEEDED),
        _response(b"video content"),
    ]
    statuses = []
    with _generate(origin=origin, on_progress=statuses.append) as path:
        assert path.read_bytes() == b"video content"
    assert not path.exists()
    create, *queries, download = http.calls
    assert (create.method, create.url) == ("POST", api + "/v2/video_generation")
    assert json.loads(create.body) == {
        "model": "MiniMax-H3",
        "content": [{"type": "text", "text": "Clouds drift."}],
        "resolution": "768P",
        "duration": 5,
        "ratio": "16:9",
        "generate_audio": True,
    }
    assert all(call.method == "GET" and call.url == api + "/v2/query/video_generation/123" for call in queries)
    assert all(call.headers.get("Authorization") == "Bearer test-token" for call in [create, *queries])
    assert (download.method, download.url) == ("GET", _CDN)
    assert "Authorization" not in download.headers
    assert statuses == ["queued", "running", "succeeded"]


@pytest.mark.parametrize(
    ("fields", "expected_roles", "ratio"),
    [
        (("first_frame",), ["first_frame"], "adaptive"),
        (("last_frame",), ["last_frame"], "adaptive"),
        (("first_frame", "last_frame"), ["first_frame", "last_frame"], "adaptive"),
        (("reference_images",), ["reference_image", "reference_image"], "16:9"),
    ],
)
def test_image_roles_keep_pixels_and_exclude_gallery_metadata(fields, expected_roles, ratio) -> None:
    image = Image.new("RGBA", (256, 320), (127, 32, 0, 111))
    exif = Image.Exif()
    exif[270] = "private gallery metadata"
    image.info["exif"] = exif.tobytes()
    inputs = {field: (image.copy, image.copy) if field == "reference_images" else image.copy for field in fields}
    payload = json.loads(replace(_REQUEST, **inputs, generate_audio=False).encode("global", lambda: False))
    assert payload["ratio"] == ratio
    assert payload["generate_audio"] is False
    items = payload["content"][1:]
    assert [item["role"] for item in items] == expected_roles
    for item in items:
        assert item["type"] == "image_url"
        prefix, encoded = item["image_url"]["url"].split(",")
        assert prefix == "data:image/png;base64"
        raw = base64.b64decode(encoded)
        with Image.open(io.BytesIO(raw)) as result:
            assert result.getpixel((0, 0)) == (127, 32, 0, 111)
            assert not result.getexif()


@pytest.mark.parametrize(
    ("region", "model", "resolution", "duration"),
    [
        ("global", "MiniMax-H3", "480P", 4),
        ("cn", "MiniMax-H3", "2K", 15),
        ("cn", "MiniMax-H3-Lite", "480P", 4),
        ("cn", "MiniMax-H3-Highspeed", "768P", 4),
        ("cn", "MiniMax-H3-Max", "480P", 5),
        ("global", "MiniMax-H3-Max-Turbo", "768P", 15),
        ("global", "MiniMax-H3-MG", "2K", 4),
        ("global", "MiniMax-H3-NC", "768P", 4),
    ],
)
def test_supported_regional_combinations_are_submitted_unchanged(region, model, resolution, duration) -> None:
    request = replace(_REQUEST, model=model, resolution=resolution, duration=duration)
    payload = json.loads(request.encode(region, lambda: False))
    assert (payload["model"], payload["resolution"], payload["duration"]) == (model, resolution, duration)


@pytest.mark.parametrize(
    ("origin", "changes"),
    [
        (_CHINA, {"resolution": "480P"}),
        (_CHINA, {"model": "MiniMax-H3-Max-Turbo"}),
        (_CHINA, {"model": "MiniMax-H3-NC"}),
        (_CHINA, {"model": "MiniMax-H3-MG"}),
        (_GLOBAL, {"model": "MiniMax-H3-MG", "resolution": "480P"}),
        (_GLOBAL, {"model": "MiniMax-H3-Highspeed", "resolution": "2K"}),
        (_GLOBAL, {"model": "MiniMax-H3-Max", "duration": 4}),
        (_GLOBAL, {"model": "MiniMax-H3-Max-Turbo", "duration": 4}),
        (_GLOBAL, {"duration": 16}),
        (_GLOBAL, {"duration": 4.5}),
        (_GLOBAL, {"ratio": "adaptive"}),
        (_GLOBAL, {"prompt": "   "}),
        (_GLOBAL, {"prompt": "a" * 7001}),
    ],
)
def test_invalid_model_or_input_is_rejected_before_submission(http, origin, changes) -> None:
    with pytest.raises(ValueError), _generate(replace(_REQUEST, **changes), origin):
        pytest.fail("Unexpected output")
    assert not http.calls


def test_incompatible_reference_roles_dimensions_and_size_are_rejected_before_submission(http, monkeypatch) -> None:
    image = Image.new("RGB", (256, 256))
    for changes in (
        {"first_frame": image.copy, "reference_images": [image.copy]},
        {"first_frame": image.copy, "reference_video": lambda: pytest.fail("Loaded incompatible clip")},
        {"last_frame": image.copy, "reference_video": lambda: pytest.fail("Loaded incompatible clip")},
        {"model": "MiniMax-H3-Lite", "ratio": "adaptive", "reference_video": lambda: pytest.fail("Loaded clip")},
        {"model": "MiniMax-H3-NC", "ratio": "adaptive", "reference_video": lambda: pytest.fail("Loaded clip")},
        {"reference_images": [image.copy] * 10},
        {"reference_images": [image.copy], "model": "MiniMax-H3-Lite", "ratio": "adaptive"},
        {"reference_images": [image.copy], "model": "MiniMax-H3-NC", "ratio": "adaptive"},
        {"last_frame": lambda: Image.new("RGB", (256, 650))},
        {"first_frame": lambda: Image.new("RGB", (255, 256))},
    ):
        with pytest.raises(ValueError), _generate(replace(_REQUEST, **changes)):
            pytest.fail("Unexpected output")
    request = replace(_REQUEST, reference_images=[image.copy])
    monkeypatch.setattr(minimax, "_MAX_REQUEST_BYTES", 100)
    with pytest.raises(ValueError, match="64 MB"), _generate(request):
        pytest.fail("Unexpected output")
    monkeypatch.setattr(minimax, "_MAX_IMAGE_BYTES", 100)
    with pytest.raises(ValueError, match="30 MB"), _generate(request):
        pytest.fail("Unexpected output")
    assert not http.calls


@pytest.mark.parametrize(
    "origin", ["http://api.minimax.cn", "https://api.minimax.io.example", "https://api.minimax.cn/v1", ""]
)
def test_api_key_only_targets_official_https_origins(http, origin) -> None:
    with pytest.raises(ExternalProviderNotConfiguredError), _generate(origin=origin):
        pytest.fail("Unexpected output")
    assert not http.calls


@pytest.mark.parametrize(
    "first_response",
    [
        requests.Timeout("private-test-signature"),
        _response({}, 307, Location=_CDN),
        _response({"task_id": "../changed"}),
        _response({}, 500),
    ],
)
def test_never_follows_redirect_or_resubmits_an_ambiguous_create(http, first_response) -> None:
    http.script = [first_response]
    with pytest.raises(ExternalProviderRequestError) as error, _generate():
        pytest.fail("Unexpected output")
    assert "private-test-signature" not in str(error.value)
    assert [call.method for call in http.calls] == ["POST"]


@pytest.mark.parametrize(
    "task",
    [
        {"id": "123", "status": "failed", "error": {"message": _CDN}},
        {"id": "123", "status": "cancelled"},
        {"id": "123", "status": "unknown"},
        {"id": "123", "status": "succeeded", "content": {}},
        {"id": "wrong-task", "status": "succeeded", "content": {"url": _CDN}},
    ],
)
def test_invalid_or_failed_status_stops_before_download_without_echoing_sensitive_data(http, task) -> None:
    http.script = [_response(_CREATED), _response({"task": task})]
    with pytest.raises(ExternalProviderRequestError) as error, _generate():
        pytest.fail("Unexpected output")
    assert "123" in str(error.value)
    assert "signature" not in str(error.value)
    assert [call.method for call in http.calls] == ["POST", "GET"]


@pytest.mark.parametrize(
    "url", ["http://93.184.216.34/result", "https://127.0.0.1/private", "https://user:password@93.184.216.34/result"]
)
def test_unsafe_result_urls_are_rejected_before_download(http, url) -> None:
    http.script = [
        _response(_CREATED),
        _response({"task": {"id": "123", "status": "succeeded", "content": {"url": url}}}),
    ]
    with pytest.raises(ExternalProviderRequestError, match="unsafe URL"), _generate():
        pytest.fail("Unexpected output")
    assert len(http.calls) == 2


@pytest.mark.parametrize(
    "download",
    [
        _response(b""),
        _response(b"tiny", **{"Content-Length": "12345"}),
        _response(b"0123456789"),
        _response(b"", 302, Location="https://127.0.0.1/"),
    ],
)
def test_rejected_or_partial_download_is_cleaned_up(http, tmp_path, monkeypatch, download) -> None:
    monkeypatch.setattr(minimax, "MAX_VIDEO_BYTES", 9)
    http.script = [_response(_CREATED), _response(_SUCCEEDED), download]
    with pytest.raises(ExternalProviderRequestError), _generate():
        pytest.fail("Unexpected output")
    assert not list(tmp_path.iterdir())
    assert len(http.calls) == 3


def test_cancellation_during_poll_or_download_stops_locally_and_removes_partial_file(http, tmp_path) -> None:
    http.script = [_response(_CREATED), _response({"task": {"id": "123", "status": "queued"}})]
    with pytest.raises(CanceledException), _generate(is_canceled=lambda: len(http.calls) == 2):
        pytest.fail("Unexpected output")
    assert [call.method for call in http.calls] == ["POST", "GET"]

    http.script = [_response(_CREATED), _response(_SUCCEEDED), _response(b"x" * 70000)]
    with (
        pytest.raises(CanceledException),
        _generate(is_canceled=lambda: any(path.stat().st_size for path in tmp_path.rglob("video.mp4"))),
    ):
        pytest.fail("Unexpected output")
    assert not list(tmp_path.iterdir())


def test_timed_out_task_is_recoverable_by_id_and_not_resubmitted(http, monkeypatch) -> None:
    http.script = [_response(_CREATED)]
    monkeypatch.setattr(minimax, "_POLL_TIMEOUT", 0)
    with pytest.raises(ExternalProviderRequestError, match="Timed out.*123"), _generate():
        pytest.fail("Unexpected output")
    assert [call.method for call in http.calls] == ["POST"]


def test_stops_after_repeated_query_failures_and_preserves_the_paid_task_id(http) -> None:
    http.script = [_response(_CREATED), _response({}, 429), requests.Timeout(), _response({}, 503)]
    with pytest.raises(ExternalProviderRequestError, match="Unable to query MiniMax task 123"), _generate():
        pytest.fail("Unexpected output")
    assert [call.method for call in http.calls] == ["POST", "GET", "GET", "GET"]


def test_redirect_body_is_never_buffered_before_applying_the_download_cap(http, monkeypatch) -> None:
    redirect = _response(b"unbounded body", 302, Location="https://127.0.0.1/")
    monkeypatch.setattr(redirect, "iter_content", lambda *a, **kw: pytest.fail("Redirect body was read"))
    http.script = [_response(_CREATED), _response(_SUCCEEDED), redirect]
    with pytest.raises(ExternalProviderRequestError, match="redirect"), _generate():
        pytest.fail("Unexpected output")


def test_cancelled_request_does_not_load_inputs_or_submit_a_paid_task(http) -> None:
    request = replace(_REQUEST, first_frame=lambda: pytest.fail("Loaded a cancelled input"))
    with pytest.raises(CanceledException), _generate(request, is_canceled=lambda: True):
        pytest.fail("Unexpected output")
    assert not http.calls


def test_caller_ingest_failure_cleans_up_the_download(http, tmp_path) -> None:
    http.script = [_response(_CREATED), _response(_SUCCEEDED), _response(b"not a playable video")]
    with pytest.raises(ValueError, match="invalid media"), _generate() as path:
        assert path.exists()
        raise ValueError("invalid media")
    assert not list(tmp_path.iterdir())


def _ffmpeg(*args: str) -> bytes:
    return subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", *args],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout


@pytest.fixture
def tagged_reference(tmp_path: Path) -> Path:
    # The chapter creates an extra text/data track alongside the video and soundtrack.
    tags = tmp_path / "private.ffmeta"
    tags.write_text(
        ";FFMETADATA1\ninvokeai_graph=private_graph\ninvokeai_workflow=private_workflow\n"
        "comment=private_prompt\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=private_chapter\n"
    )
    source = tmp_path / "gallery.mp4"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=s=320x256:r=24000/1001:d=2",
        "-f",
        "lavfi",
        "-i",
        "sine=d=2",
        "-i",
        str(tags),
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-map_metadata",
        "2",
        "-map_chapters",
        "2",
        "-metadata:s:v:0",
        "handler_name=private_handler",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        "-movflags",
        "+use_metadata_tags",
        str(source),
    )
    return source


@pytest.mark.ffmpeg
@pytest.mark.parametrize("origin", [_GLOBAL, _CHINA])
def test_sends_one_metadata_free_video_with_optional_images_in_one_submission(
    http: MiniMaxHTTP, tagged_reference: Path, origin: str, tmp_path: Path
) -> None:
    image = Image.new("RGB", (256, 320))
    with_images = origin == _CHINA
    request = replace(
        _REQUEST,
        reference_video=lambda: tagged_reference,
        reference_images=[image.copy] if with_images else (),
        ratio="adaptive",
    )
    http.script = [_response(_CREATED), _response(_SUCCEEDED), _response(b"result")]
    with _generate(request, origin) as path:
        assert path.read_bytes() == b"result"
    assert [(call.method, call.url) for call in http.calls] == [
        ("POST", origin + "/v2/video_generation"),
        ("GET", origin + "/v2/query/video_generation/123"),
        ("GET", _CDN),
    ]
    payload = json.loads(http.calls[0].body)
    assert payload["ratio"] == "adaptive"
    assert [item.get("role") for item in payload["content"]] == (
        [None, "reference_image", "reference_video"] if with_images else [None, "reference_video"]
    )
    video = payload["content"][-1]
    assert video["type"] == "video_url"
    prefix, encoded = video["video_url"]["url"].split(",")
    assert prefix == "data:video/mp4;base64"
    raw = base64.b64decode(encoded, validate=True)
    for private_tag in [
        b"private_prompt",
        b"private_graph",
        b"private_workflow",
        b"private_chapter",
        b"private_handler",
    ]:
        assert private_tag in tagged_reference.read_bytes() and private_tag not in raw
    outgoing = tmp_path / "sent.mp4"
    outgoing.write_bytes(raw)
    assert read_mp4_tags(outgoing) == {}
    original = _ffmpeg(
        "-i", str(tagged_reference), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-f", "streamhash", "-"
    )
    sent = _ffmpeg("-i", str(outgoing), "-map", "0", "-c", "copy", "-f", "streamhash", "-")
    assert sent == original
    assert not list(tmp_path.glob("invokeai-minimax-reference-*"))


@pytest.mark.ffmpeg
@pytest.mark.parametrize("extra", ["audio", "video"])
def test_rejects_multiple_playable_tracks_even_when_the_second_is_default(http, tmp_path, tagged_reference, extra):
    source = tmp_path / "alternate-track.mp4"
    selector, alternate = (
        ("a", "sine=frequency=880:d=2") if extra == "audio" else ("v", "color=c=red:s=320x256:r=24:d=2")
    )
    _ffmpeg(
        "-i",
        str(tagged_reference),
        "-f",
        "lavfi",
        "-i",
        alternate,
        "-map",
        "0:V:0",
        "-map",
        "0:a:0",
        "-map",
        f"1:{selector}:0",
        "-c",
        "copy",
        f"-c:{selector}:1",
        "aac" if extra == "audio" else "libx264",
        f"-disposition:{selector}:0",
        "0",
        f"-disposition:{selector}:1",
        "default",
        str(source),
    )
    with (
        pytest.raises(ValueError, match="one video track and at most one audio track"),
        _generate(replace(_REQUEST, reference_video=lambda: source)),
    ):
        pytest.fail("Submitted ambiguous tracks")
    assert not http.calls and not list(tmp_path.glob("invokeai-minimax-reference-*"))


@pytest.mark.ffmpeg
def test_variable_rate_video_uses_its_own_average_frame_rate_with_a_longer_soundtrack(tmp_path):
    source = tmp_path / "vfr.mp4"
    # Alternating intervals on a 120 Hz clock average about 30 fps. The 5s audio tail would lower
    # the estimate to 12 fps if the video sample count were divided by the whole movie duration.
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=s=256x256:r=120:d=2",
        "-f",
        "lavfi",
        "-i",
        "sine=d=5",
        "-vf",
        "select='eq(mod(n,8),0)+eq(mod(n,8),3)'",
        "-fps_mode",
        "vfr",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        str(source),
    )
    request = replace(_REQUEST, reference_video=lambda: source)
    assert json.loads(request.encode("global", lambda: False))["content"][1]["role"] == "reference_video"


@pytest.mark.ffmpeg
def test_combined_utf8_request_limit_accepts_exact_size_and_rejects_one_byte_more(
    http: MiniMaxHTTP, tagged_reference: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request = replace(
        _REQUEST,
        prompt='舞动 "🌊"\n',
        reference_images=[Image.new("RGB", (256, 256)).copy],
        reference_video=lambda: tagged_reference,
    )
    expected = request.encode("global", lambda: False)
    monkeypatch.setattr(minimax, "_MAX_REQUEST_BYTES", len(expected))
    assert request.encode("global", lambda: False) == expected
    monkeypatch.setattr(minimax, "_MAX_REQUEST_BYTES", len(expected) - 1)
    with pytest.raises(ValueError, match="64 MB"), _generate(request):
        pytest.fail("Submitted oversized body")
    assert not http.calls
    assert not list(tmp_path.glob("invokeai-minimax-reference-*"))


@pytest.mark.ffmpeg
@pytest.mark.parametrize(
    ("video", "audio", "size", "fps", "duration", "error"),
    [
        ("mpeg4", None, "256x256", "24", "2", "H.264 or HEVC"),
        ("libx264", "ac3", "256x256", "24", "2", "AAC or MP3"),
        (None, "aac", "", "", "2", "readable MP4 or MOV video"),
        ("libx264", None, "254x256", "24", "2", "256–5760"),
        ("libx264", None, "256x642", "24", "2", "aspect ratio"),
        ("libx264", None, "256x256", "23975/1000", "2", "23.976–60 fps"),
        ("libx264", None, "256x256", "61", "2", "23.976–60 fps"),
        ("libx264", None, "256x256", "24", "1", "2–15 seconds"),
        ("libx264", None, "256x256", "24", "15.1", "2–15 seconds"),
        ("libx264", None, "256x256", "60", "2", None),
        ("libx265", "libmp3lame", "256x256", "24", "2", None),
    ],
)
def test_validates_actual_reference_streams_before_billing(http, tmp_path, video, audio, size, fps, duration, error):
    source = tmp_path / "source.mp4"
    args = []
    if video:
        args.extend(["-f", "lavfi", "-i", f"color=s={size}:r={fps}:d={duration}"])
    if audio:
        args.extend(["-f", "lavfi", "-i", f"sine=d={duration}"])
    if video:
        args.extend(["-c:v", video])
    if audio:
        args.extend(["-c:a", audio])
    _ffmpeg(*args, str(source))
    request = replace(_REQUEST, reference_video=lambda: source)
    if error:
        with pytest.raises(ValueError, match=error), _generate(request):
            pytest.fail("Submitted invalid reference")
    else:
        assert json.loads(request.encode("cn", lambda: False))["content"][1]["role"] == "reference_video"
    assert not http.calls
    assert not list(tmp_path.glob("invokeai-minimax-reference-*"))


@pytest.mark.ffmpeg
def test_invalid_reference_file_and_cancellation_remove_the_prepared_copy(http, tmp_path, tagged_reference):
    request = replace(_REQUEST, reference_video=lambda: tagged_reference)
    # After a temporary remux exists, cancellation must prevent both validation/encoding and the paid POST.
    with (
        pytest.raises(CanceledException),
        _generate(request, is_canceled=lambda: any(tmp_path.glob("invokeai-minimax-reference-*/reference.mp4"))),
    ):
        pytest.fail("Submitted canceled reference")
    tagged_reference.write_bytes(b"not video")
    with pytest.raises(ValueError, match="readable MP4"), _generate(request):
        pytest.fail("Submitted invalid reference")
    assert not http.calls
    assert not list(tmp_path.glob("invokeai-minimax-reference-*"))


def test_missing_and_oversized_gallery_files_fail_before_starting_ffmpeg(http, tmp_path, monkeypatch):
    path = tmp_path / "missing-private-name.mp4"
    request = replace(_REQUEST, reference_video=lambda: path)
    monkeypatch.setattr(minimax, "run_ffmpeg_process", lambda *a, **kw: pytest.fail("Started a decoder"))
    with pytest.raises(ValueError, match="could not be read") as failure, _generate(request):
        pytest.fail("Submitted missing clip")
    assert str(path) not in str(failure.value)
    with path.open("wb") as stream:
        stream.truncate(50_000_001)
    with pytest.raises(ValueError, match="50 MB"), _generate(request):
        pytest.fail("Submitted excessive source")
    assert not http.calls
