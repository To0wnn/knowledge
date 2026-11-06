# Speaking in chats

[Back to the README](../README.md). The chat box has a microphone button when the server knows a speech-to-text
server: click to start and again to stop, or hold it while you talk (Ctrl+Space in the box does the same; Settings can change that key). The text
lands in the box at the cursor; you read it and send it yourself.

## How it works

The browser records (MediaRecorder: webm/opus in Chrome and Firefox, mp4 in Safari) and posts the recording to the
knowledge server (`POST /ui/api/transcribe`), which passes it on to an **OpenAI-compatible** speech-to-text server
(`<KNOWLEDGE_STT_URL>/audio/transcriptions`, multipart, `response_format=json`) and answers `{text}`. The recording is
never stored. The microphone shows only when `KNOWLEDGE_STT_URL` is set (`stt` in `GET /ui/api/me`).

Browsers allow a microphone only on **https** (or `localhost`): open the web UI through its https address.

## Set it up

Pick one, then restart the server.

| Option | Setting | Note |
|---|---|---|
| The bundled one | `KNOWLEDGE_STT_URL=http://voice:8000/v1` | whisper.cpp, CPU, local |
| A server you run | its `/v1` base URL | any OpenAI-compatible one |
| OpenAI | `https://api.openai.com/v1` | audio leaves your network |

- **The bundled one** (whisper.cpp's server on the CPU): `mkdir -p data/voice`, put
  `KNOWLEDGE_STT_URL=http://voice:8000/v1` in `.env`, then `docker compose --profile voice up -d`. The first start
  fetches the model (`small`, ~470 MB) into `data/voice`; it then needs ~1 GB RAM and answers a sentence in ~2.5 s
  on 8 cores (measured 2 Nov 2026, Dutch). Another model: `KNOWLEDGE_VOICE_MODEL=large-v3-turbo` (most accurate,
  ~1.6 GB, slower on a CPU), `base` or `tiny` (faster, more mistakes).
  The image is pinned to the build of a whisper.cpp release (`main-<commit>`; `:main` moves with every commit). To
  move to a newer release: `gh api repos/ggml-org/whisper.cpp/releases/latest --jq .tag_name` gives the tag, `gh api
  repos/ggml-org/whisper.cpp/git/ref/tags/<tag>` its object (for an annotated tag, `gh api
  repos/ggml-org/whisper.cpp/git/tags/<sha> --jq .object.sha` the commit), and `ghcr.io/ggml-org/whisper.cpp:main-<full
  commit sha>` is its image; put it in `compose.yaml`, then `docker compose --profile voice up -d`.
- **A server you already run** (whisper.cpp's `whisper-server` started with `--inference-path /v1/audio/transcriptions`
  and `--convert`, LocalAI, speaches, …): its `/v1` base in `KNOWLEDGE_STT_URL`, its model name in
  `KNOWLEDGE_STT_MODEL` when it needs one.
- **OpenAI**: `KNOWLEDGE_STT_URL=https://api.openai.com/v1`, `KNOWLEDGE_STT_KEY=<key>`, `KNOWLEDGE_STT_MODEL=whisper-1`.

Optional for all: `KNOWLEDGE_STT_LANGUAGE` (`en`, `nl`, …; empty = detect, slower and sometimes the wrong language)
and `KNOWLEDGE_STT_PROMPT` (names and terms it should spell right, comma-separated).

## When it does not work

| You see | Cause |
|---|---|
| No microphone button | `KNOWLEDGE_STT_URL` not set, or the page is on http |
| "Microphone: not allowed" and no prompt | a reverse proxy sends `Permissions-Policy: microphone=()`: allow `microphone=(self)` for the web UI |
| "Microphone: not allowed" after a prompt | you refused it: allow it in the site settings (the lock icon) |
| "the speech server failed: …" | the server cannot reach the speech server, or it refused the model name |
