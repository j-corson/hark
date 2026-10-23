# hark

hark listens to a conversation and keeps a live, structured document you can glance at while you talk. That could be a Teams call, people around a table, or two of you outlining a paper out loud.

- **Meeting** mode keeps a running summary, decisions, actions and open questions.
- **Co-writing** mode builds the document you're dictating. Spoken edits like "no, that goes in the methods" or "move the policy bit into its own section" are applied to the document instead of being written down.
- **Notes** mode groups the content by topic.

It tells speakers apart by voice. You can edit any line by hand at any time, and Claude works around your edits rather than overwriting them. Speech recognition runs on your machine. Document updates go through **your Claude subscription** via the Claude Code CLI, so no API key is needed.

## Set up (once)

You need [uv](https://docs.astral.sh/uv/) and [Claude Code](https://claude.com/claude-code), logged in with your Claude account (run `claude` once and sign in).

```sh
uv tool install git+https://github.com/mikefsway/hark
hark --check
```

`--check` downloads the speech models (about 520 MB, once) and makes one tiny Claude call to confirm your login works.

To update later: `uv tool upgrade hark`. To work on the code instead: `git clone`, then `uv run hark` inside the repo.

## Day to day

```sh
hark
```

Your browser opens on the live document. Press **Start listening** and pick:

- **Room** for everyone near this device's microphone.
- **Call** for your microphone plus the call audio. When the browser asks what to share, pick the Teams tab, or your whole screen on Windows, and turn on **Share audio**. Wear headphones so your mic doesn't pick up the other people a second time. If your browser doesn't offer audio for the whole screen (often the case on a Mac), join the meeting in Teams on the web and share that tab.

Press **Stop** when you're done. Everything is saved as you go.

### On your phone

```sh
hark --lan
```

This prints a link and a QR code for your phone, which must be on the same Wi-Fi. The phone warns once about the self-signed certificate; accept it. Phones need HTTPS before they'll share the microphone, which is why the certificate exists. The phone can be the microphone, the screen, or both. For example, your laptop captures a Teams call while you glance at your phone.

The link includes a private key, and only browsers that have opened it can connect. If you use Tailscale, `tailscale serve --bg 8765` gives you a trusted HTTPS address instead.

## Editing

- **Click any line** to edit it. Enter saves, Shift+Enter adds a line, Esc cancels. Clear a line to delete it.
- Click **Nothing yet** under an empty heading to write there. Tick checkboxes directly.
- **More → Edit as text** gives you the whole document as Markdown.
- **Undo** reverts the last change, whether you made it or Claude did.
- **Tell Claude** (the box at the bottom) takes typed instructions, such as "make Actions a table" or "merge the two budget points". These are sent straight away.
- While you're editing, Claude waits. Its next update applies small anchored changes to the document as it now stands, so your wording isn't reverted.
- Text Claude has just added or changed is highlighted for about 25 seconds.

## Speakers

Voices are grouped automatically as Speaker 1, Speaker 2 and so on. Claude may fill in real names when it's obvious, for example when someone introduces themselves.

- Click a name chip above the transcript to rename that speaker. **Giving two speakers the same name merges them.**
- Click the name on a single transcript line to reassign just that line.
- If different people keep getting merged, raise `--speaker-threshold` (default 0.40). If one person keeps getting split, lower it.

## Where things are saved

Each session is a folder in `~/Documents/Hark/` (change it with `--data-dir`):

- `doc.md`: the document
- `transcript.md`: who said what, and when
- `session.json`: everything needed to reopen it
- `versions.jsonl`: every version of the document, one per line, labelled with who made it (you, Claude or Undo). Undo uses it, so it works after reopening a session, and it's there if a document ever gets lost or mangled.

### Rebuilding a document from a transcript

If a document is lost or ruined, you can build a fresh one from a transcript:

```sh
hark --rebuild meeting.vtt
```

This reads a Teams transcript (in Teams, open the meeting recap, then download the transcript as .vtt) or hark's own `transcript.md`, and has Claude write the document from it in a few large steps. A one-hour meeting takes a few minutes. The result is a new session, so nothing is overwritten; open it with **More → Open a previous session**. Use `--mode` to pick meeting, co-writing or notes. Hand edits aren't in any transcript, so they won't come back; use the versions file for those.

Use **More → Open a previous session** to pick one up again, or **More → New session** to start fresh. Audio is never saved.

To send the document to someone, use **More → Share…**, which opens your phone's share sheet (WhatsApp, email and so on) with the document as tidy text. **Share as file…** attaches it instead. Downloaded and shared files are named with the date and title, for example `2026-10-23 Budget review with finance.md`. On Android the shared file is a `.txt`, because Chrome won't share `.md` files.

## Options

| Option | What it does |
|---|---|
| `--mode meeting\|outline\|notes` | Starting mode. You can also switch in the app; while a document is still empty, switching changes its template. |
| `--model sonnet` | Claude model for updates. `haiku` is lighter on usage, and `opus` is the most careful. |
| `--asr parakeet\|moonshine` | Speech model. Parakeet (default) is accurate, adds punctuation and handles 25 European languages. Moonshine is English-only, smaller and faster, for older laptops. |
| `--lan` | Also serve over HTTPS on your network, for your phone. |
| `--replay file.m4a` | Feed a recording through as if it were live. Useful for old recordings or for trying hark out. Needs ffmpeg for formats other than WAV. |
| `--rebuild file.vtt` | Build a new session from a Teams transcript or a hark `transcript.md`, then exit. |
| `--port 8765` | Local port. The phone port is this plus one. |

**More → Instructions for Claude** shows the brief Claude follows for the current session. You can rewrite it, for example "this is a supervision meeting; track the student's next steps", and set how often Claude updates. The defaults are every 20 seconds for meetings and every 5 seconds for co-writing.

## How it uses your Claude subscription

Each update runs `claude -p` (headless Claude Code) once. It sends the brief, the current document and the new transcript lines, and gets back a short list of edits. The call runs with `--safe-mode`, which skips your CLAUDE.md, MCP servers, plugins and hooks. It has no tools, and it isn't saved to your `/resume` history. hark removes `ANTHROPIC_API_KEY` from the call's environment so that usage counts against your subscription rather than API billing.

Updates count towards your plan's usage limits. A one-hour meeting is roughly 100–200 small calls. To use less, raise the update interval or use `--model haiku`.

## Privacy

Audio stays on your machine: voice detection, transcription and speaker matching all run locally. Transcript text and the document go to Claude through Claude Code. Let people know you're transcribing.

## How it works

```
browser mic / call audio ──16 kHz PCM over WebSocket──▶ hark server (your laptop)
   Silero VAD cuts utterances ─▶ Parakeet speech-to-text ─▶ ERes2Net voice embedding ─▶ speaker clustering
   transcript lines ─▶ (pause, or enough new text) ─▶ claude -p ─▶ anchored edits ─▶ document ─▶ every open browser
```

Everything on the speech side uses [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx), with no PyTorch. On a Raspberry Pi 5, transcription runs about 7× faster than real time. A Claude update takes 3–8 seconds.

## Troubleshooting

- **"Claude couldn't update"**: run `hark --check`. If it says you're logged out, run `claude` and sign in.
- **The phone says the microphone is blocked**: open the `https://` link from `hark --lan`, not `http://`.
- **No call audio**: the share dialog has a separate **Share audio** toggle. Turn it on.
- **Transcription falls behind** (shown in the status line): try `--asr moonshine`.
