"""Offline regression tests: format selection, quotas, tokenized file serving."""
import asyncio
import time
from pathlib import Path

from aiohttp import ClientSession, web
import download_jobs as jobs


def test_stream_formats():
    a = jobs.format_selector(720)
    assert "height<=720" in a
    assert "+ba" in a
    assert "/b" in a
    assert jobs.format_selector(None).startswith("b[ext=mp4]")
    assert jobs.MAX_MB <= 600


def test_rate_limits():
    user = 543001
    assert jobs.quota(user)
    assert jobs.quota(user)
    assert jobs.quota(user)
    assert not jobs.quota(user)


def test_file_http_range_and_expiry(tmp_path: Path):
    async def run():
        path = tmp_path / "sample.mp4"
        path.write_bytes(b"0123456789abcdef")
        token = jobs.register_file(path, 720)
        server = web.Application()
        server.router.add_get("/f/{token}", jobs.send_file)
        runner = web.AppRunner(server)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            async with ClientSession() as client:
                async with client.get(f"http://127.0.0.1:{port}/f/{token}") as response:
                    assert response.status == 200
                    assert await response.read() == b"0123456789abcdef"
                    assert "attachment" in response.headers["Content-Disposition"]
                async with client.get(f"http://127.0.0.1:{port}/f/{token}",
                                      headers={"Range": "bytes=0-3"}) as response:
                    assert response.status == 206
                    assert await response.read() == b"0123"
                async with client.get(f"http://127.0.0.1:{port}/f/bad-token") as response:
                    assert response.status == 404
            jobs.FILES[token] = (path, "sample.mp4", time.monotonic() - 1)
            jobs.cleanup()
            assert not path.exists()
        finally:
            await runner.cleanup()
    asyncio.run(run())
