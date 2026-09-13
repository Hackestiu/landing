---
title: Cultura Viva
emoji: 🏛️
colorFrom: green
colorTo: indigo
sdk: gradio
sdk_version: 5.49.1
app_file: app.py
pinned: false
short_description: The Arduino UNO Q Gaudí audio guide, running in a browser
---

# Cultura Viva — the UNO Q pipeline in a browser

This Space runs the AI pipeline of [cultura-viva-uno-q](https://github.com/Hackestiu/cultura-viva-uno-q),
an offline audio guide for Park Güell and the Sagrada Família built on an
Arduino UNO Q. It exists so the landing page can show the thing working, rather
than only describing it.

## What is identical to the device

`core/` and `hw/` are copied from the board repo **byte-for-byte** and imported
unmodified. `scripts/sync_from_board.sh` re-copies them and fails loudly if they
have drifted, so the demo cannot quietly diverge from the hardware.

| Stage | Module (vendored) | Model |
|---|---|---|
| Vision | `core/vision_module.py` | Per-site ONNX classifier, 224×224, confidence ≥ 0.5 |
| Speech-to-text | `hw/microphone_module.py` | `faster-whisper base.en`, int8, VAD, Gaudí hotwords |
| Answer | `core/model_module.py` | Qwen2.5 GGUF via llama.cpp, personality prompts + knowledge sheets |
| Speech | `hw/audio_playback_module.py` | Piper — one voice per personality |

The personality prompts, the knowledge-graph retrieval and its per-personality
field selection, the confidence threshold and the ASR corrections are all the
device's own code.

## What the browser replaces

| On the UNO Q | Here |
|---|---|
| Logitech Brio 105 webcam | Upload or webcam capture |
| Brio microphone (ALSA) | Browser recording, or a typed question |
| GPS NEO-6M proximity detection | Site dropdown |
| Modulino Buttons A / B / C | Personality radio group |
| 3.5 mm headphone jack (`aplay`) | Audio player |
| ST7735S display + Bridge RPC to the STM32 | Not reproduced — it is the device's UI |

`hw/microphone_module.py` and `hw/audio_playback_module.py` still carry their
ALSA capture and `aplay` playback code; it is simply never called here, and
`sounddevice` is not installed so the module's own guard takes the other path.

## What this does **not** prove

A Space runs on x86 server cores. The UNO Q runs four Cortex-A53 cores with no
`asimddp`/`i8mm`, on 2 GB of RAM. Every stage here is faster than on the board,
so the latency shown in the UI is a server figure, not a device figure. The real
per-stage numbers come from `python/benchmark.py` run on the board itself; show
those next to this demo, not instead of it.

## Configuration

Weights are downloaded on startup by `bootstrap.py` and cached. The public
models (Qwen, faster-whisper, Piper) need no configuration.

The two Gaudí classifiers are ViT models trained for this project, one repo per
site: [`culturaviva/park_guell-vit`](https://huggingface.co/culturaviva/park_guell-vit)
and [`culturaviva/sagrada_familia-vit`](https://huggingface.co/culturaviva/sagrada_familia-vit).
**Both are private**, so the Space needs a token to read them.

| Variable | Purpose |
|---|---|
| `HF_TOKEN` | **Required.** A read token with access to the two classifier repos. Set it as a Space **secret**, never as a plain variable. |
| `CULTURA_VISION_REPO_PARK_GUELL` | Override the Park Güell classifier repo |
| `CULTURA_VISION_REPO_SAGRADA_FAMILIA` | Override the Sagrada Família classifier repo |
| `CULTURA_SLM_REPO` / `CULTURA_SLM_FILENAME` | Override the GGUF (default: `Qwen/Qwen2.5-0.5B-Instruct-GGUF`, `qwen2.5-0.5b-instruct-q4_k_m.gguf` — matching the board's `config.py`) |
| `CULTURA_LOG_LEVEL` | Console log level (default `INFO`) |
| `DEMO_USERS` | **Required.** Logins as `user:pass,user:pass`. Usernames are free-form, so use one email per person and revoke by editing the secret. |
| `DEMO_PUBLIC` | Set to `1` to serve with no login. Without either this or `DEMO_USERS`, the app refuses to start — a dropped secret takes the demo offline rather than silently publishing it. |

`bootstrap.py` discovers the ONNX filename inside each repo rather than assuming
one, so an export that produced `onnx/model.onnx` or a quantisation suffix works
without a config change. Each file lands at `models/vision/<site>/model.onnx`,
next to the `labels.json` vendored from the board repo — that `labels.json` stays
the authority on class order, `image_size` and normalisation, because it is the
one the device reads.

Without a working token the Space still boots: vision returns `None` and the
guide answers from `knowledge_base.json` monument-level context, which is the
same degradation path the board takes when its model files are absent. The
"Pipeline status" accordion in the UI shows which stages came up.

> If you would rather not hand the Space a token, making the two classifier
> repos public removes the requirement entirely — the weights are already
> reachable by anyone who can open the Space, since it runs inference on them.

## Gradio version

`sdk_version` above pins 5.x. The app is written to build identically on 6.x
(verified locally on 6.27) — it avoids the `Blocks(css=...)` argument that moved
to `launch()` in Gradio 6 — so bumping the pin is safe.

## Running locally

```bash
cd space
pip install -r requirements.txt
HF_TOKEN=<read-token> python app.py                # http://localhost:7860
```

## Deploying

The demo is deployed as a Docker service on a plain VPS — see **[DEPLOY.md](DEPLOY.md)**, which documents two paths: Fly.io via
`fly.toml` (fastest to a live HTTPS URL, no DNS needed) and a plain VPS via
`compose.yaml` plus Caddy (cheaper per month, you run the server).

Two constraints worth knowing before changing the build:

- **`llama-cpp-python` is compiled in a builder stage.** PyPI ships source only,
  and the project's prebuilt CPU wheel index is musl-linked despite its
  `linux_x86_64` tag — `libllama.so` fails to load on any glibc image with
  `libc.musl-x86_64.so.1: cannot open shared object file`. This was verified on
  `python:3.12-slim`, and it would break a Hugging Face Gradio Space the same
  way. Do not reintroduce that index.
- **The image must not be built with `GGML_NATIVE=ON`.** cmake would target the
  build machine's exact CPU and the container would die with SIGILL on a host of
  a different generation.

## Deploying to Hugging Face Spaces instead

The Space belongs in the **`culturaviva` org**, alongside the two classifier
repos — `culturaviva/demo`, served from `https://culturaviva-demo.hf.space`,
which is the URL `SPACE_URL` in `src/pages/index.astro` points at. Pick a
different name and both have to change together.

Still supported — the frontmatter at the top of this file configures it — but
note that **Gradio and Docker Spaces now require a paid plan** (PRO for an
individual, Team for an org); only Static and ZeroGPU Spaces are free, and a
Static Space has no Python process to run the models. If you do go this route,
the **Docker** SDK and the `Dockerfile` here are a better bet than the Gradio
SDK, because the Gradio SDK would compile llama.cpp on every rebuild.

Create the Space at [huggingface.co/new-space](https://huggingface.co/new-space):
owner `culturaviva`, blank template, CPU basic, public.

`space/` lives inside the landing repo so the page and the demo version
together, so publishing means pushing just that subdirectory to the Space's git
remote:

```bash
SPACE=https://huggingface.co/spaces/culturaviva/demo

git subtree split --prefix space -b space-deploy
git push --force "$SPACE" space-deploy:main
git branch -D space-deploy
```

The force push is expected the first time and every time after: creating a Space
seeds it with its own initial commit, and `git subtree split` produces a history
unrelated to it, so an ordinary push is rejected as a non-fast-forward. The Space
repo is a publishing target, not a place to edit — everything in it comes from
`space/` here, and the README you are reading replaces the one the form
scaffolds.

After the first push, set `HF_TOKEN` under **Settings → Variables and secrets**
as a *secret*, and let it rebuild.

Putting the Space in the same org as the models does **not** grant it access to
them — a Space has no identity of its own on the Hub. It still needs `HF_TOKEN`;
being org-local only means the token can be a fine-grained one scoped to read
`culturaviva`, rather than one tied to a personal namespace.
