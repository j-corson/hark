"""HTTP + WebSocket server. Serves the page and relays audio, edits and updates."""

from __future__ import annotations

import hmac
import json
import logging
from importlib import resources
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from .hub import Hub
from .modes import MODES

log = logging.getLogger("hark")
STATIC = resources.files("hark") / "static"
COOKIE = "hark_key"


def same_origin(request: web.Request) -> bool:
    """Browsers send the cookie with WebSocket requests from other sites on the same host
    (another app on localhost, say), so only accept connections from hark's own page."""
    origin = request.headers.get("Origin")
    if not origin:
        return True  # not a browser
    host = urlsplit(origin).netloc.lower()
    allowed = {request.host.lower(), request.headers.get("X-Forwarded-Host", "").lower()} - {""}
    return host in allowed


def make_app(hub: Hub, token: str) -> web.Application:
    def authed(request: web.Request) -> bool:
        return hmac.compare_digest(request.cookies.get(COOKIE, ""), token)

    @web.middleware
    async def guard(request: web.Request, handler):
        key = request.query.get("k")
        if key is not None:
            if not hmac.compare_digest(key, token):
                raise web.HTTPForbidden(text="Wrong key. Use the link printed in the terminal.")
            # Serve the page straight away (no redirect) and remember the key in a cookie.
            # Lax, not Strict: links opened from another app are cross-site navigations,
            # and Strict cookies would be withheld from them.
            resp = await handler(request)
            resp.set_cookie(COOKIE, token, max_age=400 * 86400, httponly=True, samesite="Lax",
                            secure=request.scheme == "https" or request.headers.get("X-Forwarded-Proto") == "https")
            return resp
        if not authed(request):
            raise web.HTTPForbidden(text="Open hark using the link printed in the terminal (it carries a key).")
        return await handler(request)

    async def index(_request):
        return web.Response(body=(STATIC / "index.html").read_bytes(), content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def static(request):
        name = request.match_info["name"]
        f = STATIC / name
        if "/" in name or ".." in name or not f.is_file():
            raise web.HTTPNotFound()
        ctype = {"js": "text/javascript", "css": "text/css", "svg": "image/svg+xml", "json": "application/json"}.get(name.rsplit(".", 1)[-1], "application/octet-stream")
        return web.Response(body=f.read_bytes(), content_type=ctype, headers={"Cache-Control": "no-cache"})

    async def modes(_request):
        return web.json_response({k: {"label": v["label"], "brief": v["brief"], "interval": v["interval"]} for k, v in MODES.items()})

    async def ws_handler(request):
        if not same_origin(request):
            raise web.HTTPForbidden(text="This page isn't hark.")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=8 * 1024 * 1024)
        await ws.prepare(request)
        await hub.connect(ws)
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    await hub.audio(ws, msg.data)
                elif msg.type == WSMsgType.TEXT:
                    try:
                        await hub.handle(ws, json.loads(msg.data))
                    except Exception:
                        log.exception("bad message")
        finally:
            await hub.disconnect(ws)
        return ws

    async def on_startup(_app):
        hub.spawn(hub.updater())

    app = web.Application(middlewares=[guard])
    app.router.add_get("/", index)
    app.router.add_get("/modes", modes)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/static/{name}", static)
    app.on_startup.append(on_startup)
    return app
