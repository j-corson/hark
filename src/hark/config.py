"""hark's own settings in ~/.config/hark: which model service to use, and the access key and certificate.

config.json holds the choices made with `hark --setup`. It can hold an API key,
so it is written readable by you only. Command-line options and environment
variables override it.
"""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path

from .backends import PROVIDERS, OpenAICompatible

CONFIG = Path(os.environ.get("HARK_CONFIG", Path.home() / ".config" / "hark"))
KEYS = ("provider", "model", "research_model", "base_url", "api_key")


def config_dir() -> Path:
    CONFIG.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        CONFIG.chmod(0o700)
    except OSError:
        pass
    return CONFIG


def write_private(path: Path, data: bytes) -> None:
    """Write a secret so it is never readable by other users, even briefly."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    try:
        path.chmod(0o600)  # in case the file already existed with wider permissions
    except OSError:
        pass


def load() -> dict:
    try:
        data = json.loads((CONFIG / "config.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if k in KEYS and isinstance(v, str) and v}


def save(settings: dict) -> Path:
    path = config_dir() / "config.json"
    clean = {k: v for k, v in settings.items() if k in KEYS and v}
    write_private(path, (json.dumps(clean, indent=2) + "\n").encode())
    return path


def _ask(prompt: str, default: str = "", secret: bool = False) -> str:
    shown = f" [{default}]" if default and not secret else (" [keep the saved one]" if default else "")
    while True:
        try:
            raw = getpass.getpass(f"{prompt}{shown}: ") if secret else input(f"{prompt}{shown}: ")
        except EOFError:
            raise SystemExit("\nSetup cancelled.")
        raw = raw.strip()
        if raw or default:
            return raw or default
        print("  This one is needed.")


def wizard() -> dict:
    """Ask which model service to use and save the answers. Returns the saved settings."""
    old = load()
    print("Which AI service should hark use for the document?\n")
    print("  1. My Claude subscription, through Claude Code (no API key; uses your plan's limits)")
    print("  2. The Anthropic API, with my own API key (billed per use)")
    print("  3. Another service with an OpenAI-compatible API, with my own key")
    print("     (OpenAI, Google Gemini, OpenRouter, Mistral, or a local Ollama or LM Studio)\n")
    current = {"claude-code": "1", "anthropic": "2", "openai": "3"}.get(old.get("provider", ""), "1")
    choice = ""
    while choice not in ("1", "2", "3"):
        choice = _ask("Choose 1, 2 or 3", current)
    provider = PROVIDERS[int(choice) - 1]
    same = old.get("provider") == provider
    settings: dict = {"provider": provider}

    if provider == "claude-code":
        print("\nhark will use the `claude` command. If you haven't yet, install Claude Code and run `claude` once to sign in.")
        settings["model"] = _ask("Model (sonnet, opus or haiku)", old.get("model", "sonnet") if same else "sonnet")
    elif provider == "anthropic":
        print("\nCreate a key at https://platform.claude.com/settings/keys if you don't have one.")
        if os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY is set in your environment; it's used if you leave the key blank here.")
            key = getpass.getpass("API key (blank to use the environment variable): ").strip()
        else:
            key = _ask("API key (starts sk-ant-)", old.get("api_key", "") if same else "", secret=True)
        if key:
            settings["api_key"] = key
        settings["model"] = _ask("Model (sonnet, opus, haiku or a full model ID)", old.get("model", "sonnet") if same else "sonnet")
    else:
        print("\nThe base URL is the address of the service's OpenAI-compatible API, for example:")
        print("  OpenAI       https://api.openai.com/v1")
        print("  Gemini       https://generativelanguage.googleapis.com/v1beta/openai/")
        print("  OpenRouter   https://openrouter.ai/api/v1")
        print("  Ollama       http://localhost:11434/v1   (no key needed)")
        settings["base_url"] = _ask("Base URL", old.get("base_url", OpenAICompatible.DEFAULT_URL) if same else OpenAICompatible.DEFAULT_URL)
        local = OpenAICompatible("x", base_url=settings["base_url"], api_key="").local
        key = getpass.getpass("API key" + (" (blank if none is needed)" if local else "") + ": ").strip()
        if not key and same and old.get("api_key"):
            key = old["api_key"]
        if key:
            settings["api_key"] = key
        settings["model"] = _ask("Model name, exactly as the service lists it", old.get("model", "") if same else "")
        print("Note: web research needs Claude, so the Research panel is off with this service.")

    path = save(settings)
    print(f"\nSaved to {path} (readable by you only).")
    return settings
