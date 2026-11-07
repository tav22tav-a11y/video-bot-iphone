from format_selector import direct_formats, human_size, looks_public_media_url


def build_fmt(url, **extra):
    return {"url": url, "ext": "mp4", "protocol": "https", "vcodec": "h264",
            "acodec": "aac", "height": 720, **extra}


def test_direct_progressive_with_audio():
    good = build_fmt("https://cdn.example.org/cat.mp4", filesize=64000000)
    result = direct_formats({"formats": [good]})
    assert len(result) == 1 and result[0]["height"] == 720
    assert result[0]["size"] == 64000000


def test_hls_not_direct():
    f = build_fmt("https://cdn.example.org/video.m3u8")
    f["protocol"] = "m3u8_native"
    assert not direct_formats({"formats": [f]})


def test_dash_video_only_not_direct():
    f = build_fmt("https://cdn.example.org/video.mp4")
    f["acodec"] = "none"
    assert not direct_formats({"formats": [f]})


def test_sensitive_referer_not_direct():
    f = build_fmt("https://cdn.example.org/video.mp4")
    f["http_headers"] = {"Referer": "https://example.org/"}
    assert not direct_formats({"formats": [f]})


def test_deduplicate_height_prefer_mp4():
    f1 = build_fmt("https://cdn.example.org/a.mov", ext="mov")
    f2 = build_fmt("https://cdn.example.org/b.mp4")
    assert direct_formats({"formats": [f1, f2]})[0]["url"].endswith(".mp4")


def test_reject_private_ips_and_suspicious_urls():
    assert not looks_public_media_url("http://127.0.0.1/private")
    assert not looks_public_media_url("http://10.0.0.1/foo.mp4")
    assert not looks_public_media_url("file:///etc/passwd")
    assert not looks_public_media_url("https://internal.local/file")
    assert looks_public_media_url("https://media.example.org/video.mp4")


def test_human_sizes():
    assert human_size(1024**3) == "1.0 ГБ"
    assert human_size(None) == "размер неизвестен"


def test_generic_direct_mp4_without_metadata():
    result = direct_formats({"url": "https://cdn.example.org/file.mp4", "ext": "mp4"})
    assert len(result) == 1
