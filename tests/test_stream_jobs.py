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


def test_http_streamed_mp4_without_server_file(monkeypatch):
    """Real FFmpeg -> aiohttp -> client streaming integration test (localhost)."""
    import asyncio
    from aiohttp import ClientSession, web

    source = media("https://cdn.example.org/video.mp4")
    monkeypatch.setattr(stream, "_extract_info", lambda url: {"formats": [source]})
    monkeypatch.setattr(stream, "_validate_dns", lambda urls: None)
    monkeypatch.setattr(stream, "ffmpeg_args", lambda info, height: [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=black:s=128x72:r=10:d=1",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=32000:duration=1",
        "-shortest", "-c:v", "mpeg4", "-c:a", "aac",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ])

    async def run():
        server = web.Application()
        stream.register_routes(server)
        runner = web.AppRunner(server)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        token = stream.register_stream("https://example.org/video", 720)
        url = f"http://127.0.0.1:{port}/stream/{token}"
        try:
            async with ClientSession() as client:
                async with client.head(url) as response:
                    assert response.status == 200
                    assert token in stream.STREAMS
                async with client.get(url) as response:
                    assert response.status == 200
                    assert "attachment" in response.headers["Content-Disposition"]
                    body = await response.read()
                    assert len(body) > 1000
                    assert b"ftyp" in body[:128]
                    assert b"moof" in body
                assert token not in stream.STREAMS
                async with client.get(url) as response:
                    assert response.status == 404
        finally:
            await runner.cleanup()

    asyncio.run(run())
