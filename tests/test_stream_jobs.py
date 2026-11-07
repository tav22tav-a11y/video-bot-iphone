"""Offline tests for the diskless media streaming mode."""
import time

import pytest

import stream_jobs as stream


def media(url, height=720, vcodec="avc1", acodec="mp4a.40.2", protocol="https", **more):
    return {"url": url, "height": height, "vcodec": vcodec,
            "acodec": acodec, "protocol": protocol, "ext": "mp4", **more}


def test_progressive_video_with_audio():
    info = {"formats": [
        media("https://cdn.example.org/360.mp4", 360),
        media("https://cdn.example.org/1080.mp4", 1080),
    ]}
    src = stream.select_inputs(info, 720)
    assert len(src) == 1
    assert src[0]["height"] == 360
    assert "-movflags" in stream.ffmpeg_args(info, 720)


def test_hls_with_audio():
    fmt = media("https://cdn.example.org/v720.m3u8",
                height=720, protocol="m3u8_native")
    assert stream.select_inputs({"formats": [fmt]}, 720) == [fmt]


def test_dash_video_plus_separate_audio():
    vid = media("https://cdn.example.org/v1080.mpd", 1080,
                protocol="http_dash_segments", acodec="none")
    aud = media("https://cdn.example.org/a.mpd", 0,
                vcodec="none", protocol="http_dash_segments")
    selected = stream.select_inputs({"formats": [vid, aud]}, 1080)
    assert selected == [vid, aud]
    args = stream.ffmpeg_args({"formats": [vid, aud]}, 1080)
    assert args.count("-i") == 2
    assert "pipe:1" in args


def test_reject_drm_and_live():
    info = {"has_drm": True, "formats": [media("https://cdn.example.org/a.mp4")]}
    with pytest.raises(stream.StreamUnavailable):
        stream.select_inputs(info, None)
    info = {"is_live": True, "formats": [media("https://cdn.example.org/a.mp4")]}
    with pytest.raises(stream.StreamUnavailable):
        stream.select_inputs(info, None)


def test_reject_private_media_addresses():
    bad = media("http://127.0.0.1/private.mp4")
    with pytest.raises(stream.StreamUnavailable):
        stream.select_inputs({"formats": [bad]}, None)
    assert not stream._public_http_url("file:///etc/passwd")
    assert not stream._public_http_url("https://localhost/secret")


def test_reject_authorization_headers():
    info = {"formats": [media("https://cdn.example.org/a.mp4",
                              http_headers={"Authorization": "Bearer SECRET"})]}
    with pytest.raises(stream.StreamUnavailable):
        stream.ffmpeg_args(info, None)


def test_tokens_expire_and_are_one_time():
    token = stream.register_stream("https://example.org/movie", 720)
    assert stream.STREAMS[token].height == 720
    stream.STREAMS[token].expires = time.monotonic() - 1
    stream.expire_old()
    assert token not in stream.STREAMS


def test_no_arbitrary_size_cap_in_streaming_module():
    assert not hasattr(stream, "MAX_BYTES")
    assert stream.MAX_STREAM_SECONDS <= 840
    args = stream.ffmpeg_args({"formats": [
        media("https://cdn.example.org/5000mb.mp4")], "height": 720}, 720)
    assert "-fs" not in args
    assert "-movflags" in args
