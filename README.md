# hark

hark listens to a conversation and keeps a live, structured document you can glance at while you talk. That could be a Teams call, people around a table, or two of you outlining a paper out loud.

- **Meeting** mode keeps a running summary, decisions, actions and open questions.
- **Co-writing** mode builds the document you're dictating or working out together, such as a paper outline or a list of interview questions. It keeps only what you settle on. Ideas still being discussed go in a folded **Working notes** section at the end. Spoken edits like "no, that goes in the methods" or "move the policy bit into its own section" are applied to the document instead of being written down.
- **Grilling** mode turns a list of ideas into specs that are clear enough to hand to an agent or a colleague. Claude keeps a few questions open on the item you're discussing, each with a suggested answer, and fills in the spec as you answer them.
- **Notes** mode groups the content by topic.

It tells speakers apart by voice. You can edit any line by hand at any time, and Claude works around your edits rather than overwriting them. Speech recognition runs on your machine. Document updates go through **your Claude subscription** via the Claude Code CLI by default, so no API key is needed. You can use an Anthropic API key instead, or any service with an OpenAI-compatible API (OpenAI, Gemini, OpenRouter, a local Ollama and so on): see [Choosing the AI service](#choosing-the-ai-service).

## Set up (once)

You need [uv](https://docs.astral.sh/uv/). To use your Claude subscription (the default), you also need [Claude Code](https://claude.com/claude-code), logged in with your Claude account (run `claude` once and sign in).

```sh
uv tool install git+https://github.com/mikefsway/hark
hark --setup
```

`--setup` asks which AI service to use (your Claude subscription, an Anthropic API key, or another provider's API) and saves your answer. It then downloads the speech models (about 520 MB, once) and makes one tiny call to confirm everything works. To check again later, run `hark --check`.

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

The link includes a private key, and only browsers that have opened it can connect. If a link gets out (a screenshot of the QR code, say), run `hark --new-key` and restart hark; every old link and signed-in browser then stops working. If you use Tailscale, `tailscale serve --bg 8765` gives you a trusted HTTPS address instead.

## Editing

- **Click any line** to edit it. Enter saves, Shift+Enter adds a line, Esc cancels. Clear a line to delete it.
- Click **Nothing yet** under an empty heading to write there. Tick checkboxes directly.
- **More → Edit as text** gives you the whole document as Markdown.
- **Undo** reverts the last change, whether you made it or Claude did.
- **Tell Claude** (the box at the bottom) takes typed instructions, such as "make Actions a table" or "merge the two budget points". These are sent straight away.
- **Say "Claude, …"** to give an instruction out loud, for example "Claude, swap questions four and five". hark updates as soon as you finish the sentence instead of waiting for the next update, and confirms what it did ("✓ Swapped Q4 and Q5").
- **Tidy** has Claude go over the whole document: it keeps only the latest version of anything that was revised, merges repeats and moves things where they belong. You can also say "Claude, tidy this up". In co-writing, hark tidies by itself when you press Stop. A tidy takes 20–60 seconds, and **Undo** puts the previous version back.
- While you're editing, Claude waits. Its next update applies small anchored changes to the document as it now stands, so your wording isn't reverted.
- Text Claude has just added or changed is highlighted for about 25 seconds.

## Starting from an agenda

Use **More → Start from an agenda…** (or `hark --agenda agenda.md`) to start from a plan written beforehand. Paste it or load a file. The format is loose: an optional `# Title`, then one item per bullet or line. Indented lines under an item are notes on it.

```md
# Research backlog ideas
- Ofgem consultation responses: how supplier views on flexibility changed
  - every response since 2010?
- Half-hourly smart meter data, 1,000 homes
```

- **For a meeting**, the agenda becomes a checklist at the top of the document. Claude ticks items off as the group finishes them, so anyone can see where the meeting has got to.
- **For grilling**, each item becomes a numbered spec. One Claude call (usually 15–30 seconds) drafts each item's first questions before anyone speaks. It may also suggest a few facts to look up; these appear in the Research panel but don't start until you press **Research this**.

If the current session already has something in it, the agenda starts a new session and the current one is kept.

## Grilling

Each item gets these fields: **Question**, **Data**, **Approach**, **Done when**, **Track**, **Depends on**, **Who cares** and **Status**. A field shows "?" until it's settled, so you can see what's still missing. **Track** says how hard to grill an item:

- **explore** is a quick go/no-go look. Question, Data and Done when are enough.
- **spec tightly** needs every field.

While you're on an item, Claude keeps up to three open questions under it, most important first, each with ➡️ a suggested answer. Saying "yes" accepts the suggestion. When something is settled, the answer goes into its field and the next question appears. Claude only grills the item you're on. New ideas raised along the way are added as items and left alone until you turn to them, so a brainstorm isn't interrupted.

- If people disagree, the question is marked as disputed with each position, and Claude doesn't take a side.
- Facts that can be looked up rather than decided (is the dataset public? what licence?) are listed under the item as "Look up". Say "Claude, look that up" to research one.
- "Claude, park that" moves a question or item to **Parked**. "Claude, say that more simply" rewords a question.
- Terms used loosely ("flexibility") get a question, then an entry in **Shared terms**.
- An item is marked ready only when the group says it's done with it ("next item").

The method (ask everything that can be answered now, with a recommended answer, and let "yes" accept it) is adapted from Matt Pocock's [grilling skill](https://github.com/mattpocock/skills/blob/main/skills/productivity/grilling/SKILL.md), which is MIT-licensed.

## Research

The panel beside the document (the **Research** tab; on a phone, the **Research** button) runs lookups in the background while you keep talking.

- **Ask out loud**, for example "Claude, can you find the seminal papers on this?", or in **Tell Claude**. Claude spots explicit requests, not every topic that comes up. You can also type a request into **Look something up**.
- Each request becomes a card. A separate Claude call with web search and page fetching (and no other tools) checks its sources and returns a one-line answer and up to five findings with links. This usually takes 20–60 seconds. At most two run at once.
- Say it out loud too: "add the Sorrell review to the background" or "put those papers in". Claude adds just what was asked for, and the panel marks it as added.
- **Add** puts one finding into the document and **Add all to document** puts in the lot. Claude places them where they fit. Nothing from research goes into the document until you add it.
- Untick **Start right away when someone asks out loud** to have spoken requests wait for you to press **Research this**.
- Findings are saved with the session, in `research.md`.

One lookup uses noticeably more of your plan than a document update: roughly as much as 5 to 10 updates.

## Speakers

Voices are grouped automatically as Speaker 1, Speaker 2 and so on. Claude may fill in real names when it's obvious, for example when someone introduces themselves.

- Click a name chip above the transcript to rename that speaker. The old label or spelling is replaced in the document too. **Giving two speakers the same name merges them.**
- If someone spells their name out ("Clare, without an i"), Claude corrects it.
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
| `--mode meeting\|outline\|grill\|notes` | Starting mode. You can also switch in the app; while a document is still empty, switching changes its template. |
| `--agenda file.md` | Start from an agenda (see [Starting from an agenda](#starting-from-an-agenda)). |
| `--setup` | Choose the AI service and save it (see [Choosing the AI service](#choosing-the-ai-service)). |
| `--provider claude-code\|anthropic\|openai` | Use a different service for this run. |
| `--model sonnet` | Model for updates. With Claude, `haiku` is lighter on usage and `opus` is the most careful. With an OpenAI-compatible service, use the name it lists. |
| `--base-url URL` | The API address, for `--provider openai`. |
| `--research-model opus` | Claude model for research lookups. The default is the same as `--model`. |
| `--asr parakeet\|moonshine` | Speech model. Parakeet (default) is accurate, adds punctuation and handles 25 European languages. Moonshine is English-only, smaller and faster, for older laptops. |
| `--lan` | Also serve over HTTPS on your network, for your phone. |
| `--replay file.m4a` | Feed a recording through as if it were live. Useful for old recordings or for trying hark out. Needs ffmpeg for formats other than WAV. |
| `--rebuild file.vtt` | Build a new session from a Teams transcript or a hark `transcript.md`, then exit. |
| `--port 8765` | Local port. The phone port is this plus one. |
| `--new-key` | Replace the access key, so old links and signed-in browsers stop working. |

**More → Instructions for Claude** shows the brief Claude follows for the current session. You can rewrite it, for example "this is a supervision meeting; track the student's next steps", and set how often Claude updates. The defaults are every 20 seconds for meetings and every 5 seconds for co-writing.

## Choosing the AI service

Run `hark --setup` to choose, or to change your choice later. It saves the settings, including any API key, in `~/.config/hark/config.json`, readable by you only. Each update sends the brief, the current document and the new transcript lines, and gets back a short list of edits. A one-hour meeting is roughly 100–200 small calls.

| Service | What you need | Cost | Web research |
|---|---|---|---|
| **Your Claude subscription** (default) | Claude Code, signed in | Counts towards your plan's usage limits; nothing extra | Yes |
| **Anthropic API** | An API key from [platform.claude.com](https://platform.claude.com/settings/keys) | Pay per use. Very roughly $1.50–3 for an hour-long meeting with Sonnet, or a few cents with `--model haiku`. Tidies, agendas and web research cost extra | Yes |
| **OpenAI-compatible API** | The service's address, an API key and a model name | Whatever that service charges; nothing with a local model | No (it needs Claude) |

The OpenAI-compatible option covers OpenAI, Google Gemini, OpenRouter, Mistral, and models running on your own machine with Ollama or LM Studio. Small local models may struggle to follow hark's instructions, so expect weaker documents from them.

You can also choose per run, without saving: `hark --provider anthropic` (reads `ANTHROPIC_API_KEY`), or `hark --provider openai --base-url http://localhost:11434/v1 --model llama3.1` (reads `HARK_API_KEY` or `OPENAI_API_KEY` when a key is needed).

Whichever service you choose, you still address it as "Claude, …" out loud.

**With your Claude subscription**, each update runs `claude -p` (headless Claude Code) once. The call runs with `--safe-mode`, which skips your CLAUDE.md, MCP servers, plugins and hooks. It has no tools, and it isn't saved to your `/resume` history. hark removes `ANTHROPIC_API_KEY` from the call's environment so that usage counts against your subscription rather than API billing. To use less of your plan, raise the update interval or use `--model haiku`.

hark never sees your Claude login. It runs your own copy of Claude Code, unchanged, which you sign in to through Anthropic's own sign-in. This is how Anthropic's [Claude Code terms](https://code.claude.com/docs/en/legal-and-compliance) allow a Claude plan to be used, and Anthropic's help page on [using the Agent SDK and `claude -p` with your plan](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan) says this usage counts towards your plan's limits. Those limits assume ordinary, individual use, so run hark for yourself rather than as a service for other people. If you'd rather not rely on your plan, or Anthropic changes its rules, use an API key instead (`hark --setup`).

**With the Anthropic API**, `sonnet`, `opus` and `haiku` mean the current models of those names; you can also give a full model ID. hark turns on Anthropic's automatic fallback, which retries a request on another Claude model if the first one declines it.

## Privacy

Audio stays on your machine: voice detection, transcription and speaker matching all run locally. Transcript text and the document go to the AI service you chose: Anthropic for your Claude subscription or an Anthropic key, or the provider you set up otherwise. With a local model (Ollama, LM Studio), nothing leaves your machine except research lookups, which aren't available there anyway.

**Tell people before you start, and get their agreement.** Recording or transcribing a conversation without consent is against the law in some places and against many organisations' policies, and the transcript and document are personal data. Sessions are saved as plain files in `~/Documents/Hark/`, so delete the ones you don't need.

## How it works

```
browser mic / call audio ──16 kHz PCM over WebSocket──▶ hark server (your laptop)
   Silero VAD cuts utterances ─▶ Parakeet speech-to-text ─▶ ERes2Net voice embedding ─▶ speaker clustering
   transcript lines ─▶ (pause, or enough new text) ─▶ claude -p ─▶ anchored edits ─▶ document ─▶ every open browser
```

Everything on the speech side uses [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx), with no PyTorch. On a Raspberry Pi 5, transcription runs about 7× faster than real time. A Claude update takes 3–8 seconds.

## Troubleshooting

- **"Claude couldn't update"**: run `hark --check`. With your subscription, if it says you're logged out, run `claude` and sign in. With an API, run `hark --setup` to check the key, address and model name.
- **The phone says the microphone is blocked**: open the `https://` link from `hark --lan`, not `http://`.
- **No call audio**: the share dialog has a separate **Share audio** toggle. Turn it on.
- **Transcription falls behind** (shown in the status line): try `--asr moonshine`.

## Licence

MIT. See [LICENSE](LICENSE).

hark is an independent project. It isn't made or endorsed by Anthropic.
