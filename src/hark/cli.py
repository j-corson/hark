"""`hark`: start the app and print the link to open."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import ipaddress
import logging
import os
import secrets
import socket
import ssl
import sys
import webbrowser
from pathlib import Path

from aiohttp import web

from . import __version__
from .modes import MODES
from .session import default_data_dir

CONFIG = Path(os.environ.get("HARK_CONFIG", Path.home() / ".config" / "hark"))


def token() -> str:
    CONFIG.mkdir(parents=True, exist_ok=True)
    f = CONFIG / "key"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(18))
        try:
            f.chmod(0o600)
        except OSError:
            pass
    return f.read_text().strip()


def lan_ips() -> list[str]:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def tls_context(ips: list[str]) -> ssl.SSLContext:
    """Self-signed certificate so phone browsers allow microphone access over the LAN."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    cert_f, key_f, ips_f = CONFIG / "cert.pem", CONFIG / "key.pem", CONFIG / "cert-ips.txt"
    want = ",".join(ips)
    if not (cert_f.exists() and key_f.exists() and ips_f.exists() and ips_f.read_text() == want):
        key = ec.generate_private_key(ec.SECP256R1())
        host = socket.gethostname()
        names = [x509.DNSName("localhost"), x509.DNSName(host), x509.DNSName(f"{host}.local")]
        names += [x509.IPAddress(ipaddress.ip_address(i)) for i in ips + ["127.0.0.1"]]
        subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"hark on {host}")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subj).issuer_name(subj).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=825))
                .add_extension(x509.SubjectAlternativeName(names), critical=False).sign(key, hashes.SHA256()))
        key_f.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        cert_f.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        ips_f.write_text(want)
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert_f, key_f)
    return ctx


def print_qr(url: str) -> None:
    try:
        import qrcode
        q = qrcode.QRCode(border=1)
        q.add_data(url)
        q.print_ascii(invert=True)
    except Exception:
        pass


def check(args) -> None:
    from . import models
    from .scribe import Scribe, build_prompt

    print("Speech models ...", flush=True)
    models.ensure(args.asr)
    print("  ok", flush=True)
    sc = Scribe(args.model, args.effort)
    if not sc.available:
        raise SystemExit("Claude Code CLI not found. Install it and run `claude` once to log in.")
    print(f"Claude ({args.model}) via {sc.bin} ...", flush=True)
    prompt = build_prompt("Meeting notes.", "# Notes\n\n## Decisions\n", [], [], ["[10:00:00] Ana: We agreed to ship on Friday."], [])
    try:
        data, took = asyncio.run(sc.edit(prompt))
    except Exception as e:
        raise SystemExit(f"  failed: {e}\n  Run `claude` in a terminal to log in, then try again.")
    print(f"  ok: {len(data.get('ops', []))} edit(s) in {took:.1f}s", flush=True)
    print("All set. Run `hark` to start.")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="hark", description="Listen to a conversation and keep a live document with Claude.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--mode", default="meeting", choices=list(MODES), help="starting document mode (switchable in the app)")
    p.add_argument("--lan", action="store_true", help="also serve over HTTPS on your local network (to use a phone)")
    p.add_argument("--model", default=os.environ.get("HARK_MODEL", "sonnet"), help="Claude model alias for document updates (default: sonnet)")
    p.add_argument("--effort", default="low", help="Claude effort level (low/medium/high); low keeps updates quick")
    p.add_argument("--asr", default=os.environ.get("HARK_ASR", "parakeet"), choices=["parakeet", "moonshine"],
                   help="speech model: parakeet (accurate, multilingual) or moonshine (faster, English)")
    p.add_argument("--speaker-threshold", type=float, default=0.40,
                   help="voice similarity needed to count as the same speaker (raise if people get merged, lower if one person is split)")
    p.add_argument("--data-dir", type=Path, default=default_data_dir(), help="where sessions are saved (default ~/Documents/Hark)")
    p.add_argument("--replay", metavar="AUDIO", help="feed an audio file through as if it were live (for testing or old recordings)")
    p.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--check", action="store_true", help="check the Claude login and download models, then exit")
    p.add_argument("--version", action="version", version=f"hark {__version__}")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    if args.check:
        check(args)
        return
    print("hark: loading speech models (first run downloads them) ...", file=sys.stderr, flush=True)
    from .engine import Engine
    from .hub import Hub
    from .scribe import Scribe
    from .server import make_app

    engine = Engine(args.asr)
    scribe = Scribe(args.model, args.effort)
    if not scribe.available:
        print("\n  ! Claude Code CLI not found: transcripts will work but the document won't update.\n"
              "    Install it (https://claude.com/claude-code) and run `claude` once to log in.\n", file=sys.stderr)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    hub = Hub(engine, scribe, args.data_dir, args.speaker_threshold)
    hub.session.set_mode(args.mode)
    key = token()
    app = make_app(hub, key)

    async def run():
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", args.port).start()
        local = f"http://localhost:{args.port}/?k={key}"
        print(f"\n  hark is running. Open:  {local}", flush=True)
        if args.lan:
            ips = lan_ips()
            await web.TCPSite(runner, "0.0.0.0", args.port + 1, ssl_context=tls_context(ips)).start()
            for ip in ips:
                print(f"  On your phone (same Wi-Fi):  https://{ip}:{args.port + 1}/?k={key}", flush=True)
            if ips:
                print("  (Your phone will warn about the self-signed certificate once. Accept it to continue.)\n", flush=True)
                print_qr(f"https://{ips[0]}:{args.port + 1}/?k={key}")
        print(f"  Sessions are saved in {args.data_dir}\n  Ctrl+C to stop.\n", flush=True)
        if not args.no_browser and not args.replay:
            webbrowser.open(local)
        if args.replay:
            hub.spawn(hub.replay(args.replay, args.speed))
        await asyncio.Event().wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        hub.session.save()
        print("\nSaved. Bye.")


if __name__ == "__main__":
    main()
