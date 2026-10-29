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
from . import config as settings_file
from .agenda import parse as parse_agenda, plan as agenda_plan
from .backends import PROVIDERS, make_backend
from .config import CONFIG, config_dir, write_private
from .modes import MODES
from .session import default_data_dir

_config_dir, _write_private = config_dir, write_private


def token(new: bool = False) -> str:
    f = _config_dir() / "key"
    if new or not f.exists():
        _write_private(f, secrets.token_urlsafe(18).encode())
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
        _config_dir()
        _write_private(key_f, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
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


def resolve(args) -> dict:
    """Settings from the command line, then the environment, then `hark --setup`, then defaults."""
    saved = settings_file.load()
    provider = args.provider or os.environ.get("HARK_PROVIDER") or saved.get("provider") or "claude-code"
    same = provider == saved.get("provider", "claude-code")
    pick = lambda flag, env, key: flag or os.environ.get(env) or (saved.get(key) if same else None)
    return {"provider": provider, "model": pick(args.model, "HARK_MODEL", "model"),
            "research_model": pick(args.research_model, "HARK_RESEARCH_MODEL", "research_model"),
            "base_url": pick(args.base_url, "HARK_BASE_URL", "base_url"),
            "api_key": saved.get("api_key") if same else None}


def build_scribe(args):
    from .scribe import Scribe
    cfg = resolve(args)
    backend = make_backend(cfg["provider"], cfg["model"], cfg["base_url"], cfg["api_key"])
    return Scribe(backend, args.effort), cfg


def build_corpora(args):
    """Saved collections (re-read whenever the config file changes) plus any given with --corpus."""
    from .corpus import Library, parse_spec

    extra = []
    for spec in args.corpus:
        name, path = parse_spec(spec)
        if os.path.isdir(path):
            extra.append({"name": name, "path": os.path.abspath(path), "about": "", "repo": ""})
        else:
            print(f"  ! --corpus {spec}: no such folder; skipping it.", file=sys.stderr)
    return Library(settings_file.config_file(), extra)


async def prepare_corpora(corpora) -> None:
    """Build each collection's text copy in the background, so the first search doesn't wait for it."""
    log = logging.getLogger("hark")
    for c in corpora:
        try:
            n = await asyncio.to_thread(c.sync)
            log.info("collection %s: %d files ready (%d updated, %d not text)", c.name, n["files"], n["changed"], n["skipped"])
        except Exception as e:
            log.warning("collection %s: %s", c.name, e)


def manage_corpora(args) -> None:
    if args.remove_corpus:
        if not settings_file.remove_corpus(args.remove_corpus):
            saved = ", ".join(c["name"] for c in settings_file.load_corpora()) or "none"
            raise SystemExit(f"No saved collection called {args.remove_corpus!r}. Saved: {saved}")
        print(f"Forgot the collection {args.remove_corpus!r}.")
        return
    try:
        c = settings_file.add_corpus(args.add_corpus, args.about)
    except (OSError, ValueError) as e:
        raise SystemExit(str(e))
    what = f"GitHub repo {c['repo']} (hark keeps its own copy up to date)" if c["repo"] else c["path"]
    print(f"Saved the collection {c['name']!r}: {what}.")
    print(f'A running hark picks it up straight away. Ask for it by name ("Claude, what do the {c["name"]} documents say about ...?")'
          " or pick it in the Research panel.")


def check(args) -> None:
    from . import models
    from .scribe import build_prompt

    print("Speech models ...", flush=True)
    models.ensure(args.asr)
    print("  ok", flush=True)
    sc, cfg = build_scribe(args)
    if not sc.available:
        raise SystemExit(sc.problem)
    print(f"Model {sc.model} via {sc.backend.label} ...", flush=True)
    prompt = build_prompt("Meeting notes.", "# Notes\n\n## Decisions\n", [], [], ["[10:00:00] Ana: We agreed to ship on Friday."], [])
    async def once():
        try:
            return await sc.edit(prompt)
        finally:
            await sc.backend.aclose()

    try:
        data, took = asyncio.run(once())
    except Exception as e:
        hint = "Run `claude` in a terminal to log in" if cfg["provider"] == "claude-code" else "Run `hark --setup` to check the settings"
        raise SystemExit(f"  failed: {e}\n  {hint}, then try again.")
    print(f"  ok: {len(data.get('ops', []))} edit(s) in {took:.1f}s", flush=True)
    print("  Web research: " + ("available" if sc.backend.can_research else "not available with this service (it needs Claude)"))
    for c in build_corpora(args).current().values():
        if not sc.backend.can_search_corpus:
            print(f"  Collection {c.name}: can't be searched with this service (it needs Claude, and a recent Claude Code)")
            continue
        try:
            n = c.sync()
            print(f"  Collection {c.name}: {n['files']} files ready to search" + (f", {n['skipped']} skipped (not text)" if n["skipped"] else ""))
            if c.problem:
                print(f"    ! {c.problem}")
        except OSError as e:
            print(f"  Collection {c.name}: {e}")
    print("All set. Run `hark` to start.")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="hark", description="Listen to a conversation and keep a live document with Claude.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--mode", default="meeting", choices=list(MODES), help="starting document mode (switchable in the app)")
    p.add_argument("--agenda", metavar="FILE", type=Path,
                   help="start from an agenda (Markdown or text, one item per bullet or line); in grilling mode Claude drafts first questions")
    p.add_argument("--lan", action="store_true", help="also serve over HTTPS on your local network (to use a phone)")
    p.add_argument("--setup", action="store_true", help="choose which AI service hark uses (your Claude subscription, or an API key), then exit")
    p.add_argument("--provider", choices=PROVIDERS,
                   help="AI service: claude-code (your Claude subscription, the default), anthropic (API key) or openai (any OpenAI-compatible API)")
    p.add_argument("--model", help="model for document updates (default: sonnet; with --provider openai, the service's model name)")
    p.add_argument("--base-url", help="API address, for --provider openai (default https://api.openai.com/v1)")
    p.add_argument("--research-model", help="model for background research (default: same as --model)")
    p.add_argument("--corpus", action="append", default=[], metavar="[NAME=]FOLDER",
                   help="a folder of your own documents (a repo, project notes) Claude can search when asked; repeatable. "
                        "Use --add-corpus to keep it for every run")
    p.add_argument("--add-corpus", metavar="[NAME=]FOLDER|OWNER/REPO",
                   help="save a folder, or a GitHub repo (hark clones it and keeps it up to date), as a searchable collection, then exit")
    p.add_argument("--about", default="", help="with --add-corpus: a few words on what's in it, which helps Claude know when to search it")
    p.add_argument("--remove-corpus", metavar="NAME", help="forget a saved collection, then exit")
    p.add_argument("--effort", default="low", help="Claude effort level (low/medium/high); low keeps updates quick")
    p.add_argument("--asr", default=os.environ.get("HARK_ASR", "parakeet"), choices=["parakeet", "moonshine"],
                   help="speech model: parakeet (accurate, multilingual) or moonshine (faster, English)")
    p.add_argument("--speaker-threshold", type=float, default=0.40,
                   help="voice similarity needed to count as the same speaker (raise if people get merged, lower if one person is split)")
    p.add_argument("--data-dir", type=Path, default=default_data_dir(), help="where sessions are saved (default ~/Documents/Hark)")
    p.add_argument("--replay", metavar="AUDIO", help="feed an audio file through as if it were live (for testing or old recordings)")
    p.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier")
    p.add_argument("--rebuild", metavar="TRANSCRIPT", type=Path,
                   help="build a new session from a Teams .vtt transcript or hark's transcript.md (uses --mode), then exit")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--check", action="store_true", help="check the Claude login and download models, then exit")
    p.add_argument("--new-key", action="store_true",
                   help="replace the access key so old links and signed-in browsers stop working, then exit")
    p.add_argument("--version", action="version", version=f"hark {__version__}")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for name in ("aiohttp.access", "httpx", "httpx2", "httpcore2", "anthropic", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)  # no line per API request

    if args.setup:
        settings_file.wizard()
        print("Checking it works ...\n")
        args.provider = args.model = args.base_url = args.research_model = None
        check(args)
        return
    if args.check:
        check(args)
        return
    if args.add_corpus or args.remove_corpus:
        manage_corpora(args)
        return
    if args.new_key:
        token(new=True)
        print("hark: made a new access key. Restart hark and open the new link it prints;"
              " old links and signed-in browsers no longer work.")
        return
    agenda_text = None
    if args.agenda:
        if not args.agenda.is_file():
            raise SystemExit(f"No such file: {args.agenda}")
        agenda_text = args.agenda.read_text(encoding="utf-8", errors="replace")
    if args.rebuild:
        from .rebuild import rebuild

        if not args.rebuild.is_file():
            raise SystemExit(f"No such file: {args.rebuild}")
        scribe, _cfg = build_scribe(args)
        if not scribe.available:
            raise SystemExit(scribe.problem)
        args.data_dir.mkdir(parents=True, exist_ok=True)
        async def go():
            try:
                await rebuild(args.rebuild, args.data_dir, args.mode, scribe)
            finally:
                await scribe.backend.aclose()

        asyncio.run(go())
        return
    print("hark: loading speech models (first run downloads them) ...", file=sys.stderr, flush=True)
    from .engine import Engine
    from .hub import Hub
    from .server import make_app

    engine = Engine(args.asr)
    scribe, cfg = build_scribe(args)
    if not scribe.available:
        print(f"\n  ! {scribe.problem}\n    Transcripts will work but the document won't update.\n", file=sys.stderr)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    from .research import Researcher
    library = build_corpora(args)
    corpora = list(library.current().values())
    hub = Hub(engine, scribe, args.data_dir, args.speaker_threshold, Researcher(scribe, cfg["research_model"], corpora=library))
    hub.session.set_mode(args.mode)
    key = token()
    app = make_app(hub, key)

    async def run():
        if agenda_text is not None:
            if not await hub.load_agenda(agenda_text, args.mode):
                raise SystemExit(f"No agenda items or aims found in {args.agenda}. Put one item per bullet or line.")
            n = len(parse_agenda(agenda_plan(agenda_text)[0])[2])
            print(f"  Agenda: {n} item{'s' * (n != 1)} from {args.agenda}"
                  + (" (Claude is drafting first questions)" if hub.prep else ""), flush=True)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", args.port).start()
        local = f"http://localhost:{args.port}/?k={key}"
        print(f"\n  hark is running. Open:  {local}", flush=True)
        if scribe.available:
            print(f"  Documents are written by {scribe.model} via {scribe.backend.label}.", flush=True)
        if corpora and hub.researcher.corpus_ok:
            print(f"  Collections Claude can search: {', '.join(c.name for c in corpora)}", flush=True)
            corpus_prep = asyncio.get_running_loop().create_task(prepare_corpora(corpora))  # noqa: F841 (kept so it isn't collected)
        elif corpora:
            print("  ! Collections need Claude (and a recent Claude Code) to be searched; they're off for now.", flush=True)
        if args.lan:
            ips = lan_ips()
            await web.TCPSite(runner, "0.0.0.0", args.port + 1, ssl_context=tls_context(ips)).start()
            for ip in ips:
                print(f"  On your phone (same Wi-Fi):  https://{ip}:{args.port + 1}/?k={key}", flush=True)
            if ips:
                print(f"  Room screen (read-only, for a TV or projector):  https://{ips[0]}:{args.port + 1}/present?k={key}", flush=True)
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
