from aiohttp.test_utils import make_mocked_request

from hark.server import same_origin


def req(origin=None, host="localhost:8765", fwd=None):
    headers = {"Host": host}
    if origin:
        headers["Origin"] = origin
    if fwd:
        headers["X-Forwarded-Host"] = fwd
    return make_mocked_request("GET", "/ws", headers=headers)


def test_own_page_allowed():
    assert same_origin(req("http://localhost:8765"))
    assert same_origin(req("https://192.168.1.5:8766", host="192.168.1.5:8766"))


def test_behind_proxy_allowed():
    assert same_origin(req("https://box.ts.net:10000", host="127.0.0.1:8765", fwd="box.ts.net:10000"))


def test_no_origin_allowed():
    assert same_origin(req())


def test_other_sites_refused():
    assert not same_origin(req("http://localhost:3000"))
    assert not same_origin(req("https://evil.example"))
    assert not same_origin(req("null"))


def test_room_screen_needs_the_key():
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from hark.server import make_app

    class Hub:
        async def updater(self):
            pass

        def spawn(self, coro):
            coro.close()

    async def run():
        async with TestClient(TestServer(make_app(Hub(), "secret"))) as c:
            assert (await c.get("/present")).status == 403
            r = await c.get("/present?k=secret")
            assert r.status == 200 and "present.js" in await r.text()

    asyncio.run(run())
