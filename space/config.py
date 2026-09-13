"""
Configuration for the Hugging Face Space build of the Cultura Viva pipeline.

This file replaces the board's `python/config.py`. It deliberately exports the
same names, with the same meanings, so that `core/vision_module.py` and
`core/model_module.py` can be vendored from the board byte-for-byte (see
`scripts/sync_from_board.sh`) and run here unmodified.

What it drops, relative to the board's config: everything hardware. There is no
V4L2 camera index, no ALSA mic or playback device, no device discovery, no
Bridge RPC to the STM32. The Space receives a photo and a recording from the
browser instead, and hands back a WAV file.

What it keeps identical: the model paths and the vision label semantics, so the
answers a visitor gets come out of exactly the pipeline that runs on the UNO Q.
"""

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent

CORE_DIR = APP_DIR / "core"
MODELS_DIR = APP_DIR / "models"
DATA_DIR = APP_DIR / "data"

PHOTOS_DIR = DATA_DIR / "photos"
RECORDINGS_DIR = DATA_DIR / "recordings"
RESPONSES_DIR = DATA_DIR / "responses"

MODELS_CONFIG_FILE = MODELS_DIR / "models.json"

DEFAULT_LOCATION = "sagrada_familia"

for _dir in (
    PHOTOS_DIR,
    RECORDINGS_DIR,
    RESPONSES_DIR,
    MODELS_DIR / "stt",
    MODELS_DIR / "slm",
    MODELS_DIR / "tts",
    MODELS_DIR / "vision",
):
    _dir.mkdir(parents=True, exist_ok=True)

from logging_setup import logger, setup_logging  # noqa: E402

setup_logging()

# ---------------------------------------------------------------------------
# Model paths — same layout and same filenames as the board.
# ---------------------------------------------------------------------------

STT_MODEL_PATH = (
    MODELS_DIR / "stt"
    if (MODELS_DIR / "stt" / "model.bin").exists()
    else MODELS_DIR / "stt" / "faster-whisper-base.en"
)

SLM_MODEL_FILENAME = os.environ.get(
    "CULTURA_SLM_FILENAME", "qwen2.5-0.5b-instruct-q4_k_m.gguf"
)
SLM_MODEL_PATH = MODELS_DIR / "slm" / SLM_MODEL_FILENAME
KG_PATH = MODELS_DIR / "knowledge" / "element_sheets.json"
KG_BASE_PATH = MODELS_DIR / "knowledge" / "knowledge_base.json"
# The board keeps this at python/minimapa/; here it sits with the other vendored
# data. Same name, same meaning: the per-site tile maps and landmark registries.
MINIMAP_DIR = MODELS_DIR / "minimap"
TTS_MODEL_DIR = MODELS_DIR / "tts"
VISION_MODEL_DIR = MODELS_DIR / "vision"

VISION_NON_RECOGNIZABLE_LABELS = {
    "unknown",
    "altres",
    "desconegut",
    "other",
    "no_element",
    "background",
    "non_monument",
    "none",
    "fons",
}
VISION_UNKNOWN_LABEL = "unknown"

# ---------------------------------------------------------------------------
# Inert hardware constants.
#
# hw/microphone_module.py and hw/audio_playback_module.py are vendored from the
# board byte-for-byte, and both read these at import time. Nothing in the Space
# uses them: there is no ALSA device here, `sounddevice` is not installed (the
# module already guards that import), and `aplay` is never invoked — the browser
# does the capture and the playback. They exist so the vendored files import
# cleanly and their STT and Piper code paths stay identical to the board's.
# ---------------------------------------------------------------------------

MIC_DEVICE: str = "unused"
MIC_SAMPLE_RATE: int = 16000
RECORD_CHUNK_SECONDS = 0.5
RECORD_MAX_SECONDS = 60.0
PLAYBACK_DEVICE: str = "default"
DEFAULT_VOLUME_PERCENT = 70

# ---------------------------------------------------------------------------
# Hugging Face repositories the weights are pulled from at startup.
# ---------------------------------------------------------------------------

# The two Gaudí classifiers are ViT models trained for this project, one repo
# per site. Both are private, so the Space needs HF_TOKEN set as a secret to
# read them. Without a usable token the Space still runs: vision returns None
# and the guide answers from the monument-level knowledge base.
VISION_REPOS = {
    "park_guell": os.environ.get(
        "CULTURA_VISION_REPO_PARK_GUELL", "culturaviva/park_guell-vit"
    ),
    "sagrada_familia": os.environ.get(
        "CULTURA_VISION_REPO_SAGRADA_FAMILIA", "culturaviva/sagrada_familia-vit"
    ),
    "casa_batllo": os.environ.get(
        "CULTURA_VISION_REPO_CASA_BATLLO", "culturaviva/casa_batllo"
    ),
    "pedrera": os.environ.get(
        "CULTURA_VISION_REPO_PEDRERA", "culturaviva/pedrera"
    ),
}

SLM_REPO_ID = os.environ.get(
    "CULTURA_SLM_REPO", "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
)
STT_REPO_ID = os.environ.get("CULTURA_STT_REPO", "Systran/faster-whisper-base.en")
TTS_REPO_ID = os.environ.get("CULTURA_TTS_REPO", "rhasspy/piper-voices")

__all__ = [
    "APP_DIR",
    "MODELS_DIR",
    "MODELS_CONFIG_FILE",
    "DATA_DIR",
    "PHOTOS_DIR",
    "RECORDINGS_DIR",
    "RESPONSES_DIR",
    "DEFAULT_LOCATION",
    "STT_MODEL_PATH",
    "SLM_MODEL_PATH",
    "KG_PATH",
    "KG_BASE_PATH",
    "TTS_MODEL_DIR",
    "VISION_MODEL_DIR",
    "VISION_NON_RECOGNIZABLE_LABELS",
    "VISION_UNKNOWN_LABEL",
    "MIC_DEVICE",
    "MIC_SAMPLE_RATE",
    "RECORD_CHUNK_SECONDS",
    "RECORD_MAX_SECONDS",
    "PLAYBACK_DEVICE",
    "DEFAULT_VOLUME_PERCENT",
    "VISION_REPOS",
    "SLM_REPO_ID",
    "STT_REPO_ID",
    "TTS_REPO_ID",
    "logger",
]
