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
