"""
Downloads the model weights the pipeline needs into `models/`, mirroring the
layout documented in the board repo's `python/models/README.md`.

The weights are never committed — neither here nor on the board. On the UNO Q
they are fetched once with `huggingface-cli` during setup; here the same files
are fetched on Space startup and land in the Space's HF cache, so a restart that
keeps the cache is fast and only a cold rebuild pays the full download.

Every download is best-effort. A stage whose weights are missing degrades the
way it does on the board — `vision_module` returns None, `model_module` returns
its "model not available" fallback — instead of taking the whole app down.
"""

import shutil
from pathlib import Path

from config import (
    MODELS_DIR,
    SLM_MODEL_FILENAME,
    SLM_MODEL_PATH,
    SLM_REPO_ID,
    STT_MODEL_PATH,
    STT_REPO_ID,
    TTS_MODEL_DIR,
    TTS_REPO_ID,
    VISION_MODEL_DIR,
    VISION_REPOS,
    logger,
)

# Piper voice pairs, as listed in models/README.md §4. Spike and Prudence share
# the en_GB-semaine-medium pair and differ only by speaker_id at synthesis time.
_PIPER_FILES = [
    "en/en_US/libritts_r/medium/en_US-libritts_r-medium.onnx",
    "en/en_US/libritts_r/medium/en_US-libritts_r-medium.onnx.json",
    "en/en_GB/semaine/medium/en_GB-semaine-medium.onnx",
    "en/en_GB/semaine/medium/en_GB-semaine-medium.onnx.json",
]



def _fetch(repo_id: str, filename: str, target: Path) -> bool:
    """Downloads one file from the Hub and copies it to `target`, unless it is
    already there. Returns True if the file is present afterwards."""
    if target.exists():
        logger.debug("Already present, skipping: {}", target)
        return True

    from huggingface_hub import hf_hub_download

    try:
        cached = hf_hub_download(repo_id=repo_id, filename=filename)
    except Exception as exc:
        logger.warning("Could not download {}/{}: {}", repo_id, filename, exc)
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cached, target)
    logger.success("Fetched {} -> {}", filename, target)
    return True


def ensure_slm() -> bool:
    return _fetch(SLM_REPO_ID, SLM_MODEL_FILENAME, SLM_MODEL_PATH)


def ensure_stt() -> bool:
    """Pulls the CTranslate2 faster-whisper model into the directory config.py
    points at. `vocabulary.txt` carries the tokenizer, so no tokenizer.json."""
    if (STT_MODEL_PATH / "model.bin").exists():
        return True

    from huggingface_hub import snapshot_download

    try:
        snapshot_download(repo_id=STT_REPO_ID, local_dir=str(STT_MODEL_PATH))
    except Exception as exc:
        logger.warning("Could not download STT model {}: {}", STT_REPO_ID, exc)
        return False

    logger.success("Fetched STT model -> {}", STT_MODEL_PATH)
    return True


def ensure_tts() -> int:
    """Returns the number of Piper files available after the attempt (4 = both
    voice pairs complete)."""
    return sum(
        _fetch(TTS_REPO_ID, remote, TTS_MODEL_DIR / Path(remote).name)
        for remote in _PIPER_FILES
    )


def _find_onnx(repo_id: str) -> str | None:
    """Returns the path of the ONNX weights inside a classifier repo.

    The filename is discovered rather than assumed: an exported ViT lands as
    `model.onnx` at the root under some toolchains and under `onnx/` or with a
    quantisation suffix under others, and guessing wrong here would look
    identical to the repo being unreachable. Prefers a file literally named
    model.onnx, then the shallowest remaining candidate.
    """
    from huggingface_hub import list_repo_files

    try:
        files = list_repo_files(repo_id)
    except Exception as exc:
        logger.warning("Could not list {}: {}", repo_id, exc)
        return None

    candidates = [f for f in files if f.endswith(".onnx")]
    if not candidates:
        logger.warning("No .onnx file in {} (saw: {})", repo_id, ", ".join(files))
        return None

    exact = [f for f in candidates if Path(f).name == "model.onnx"]
    chosen = min(exact or candidates, key=lambda f: (f.count("/"), len(f)))
    if len(candidates) > 1:
        logger.info("{} has {} ONNX files; using {}", repo_id, len(candidates), chosen)
    return chosen


def ensure_vision() -> int:
    """Downloads one classifier per site, each from its own repo, into the
    per-site layout vision_module.py expects (models/vision/<site>/model.onnx;
    labels.json for each site is vendored from the board repo alongside it).

    Returns the number of sites whose classifier is available. Zero is
    survivable: without a classifier the guide falls back to monument-level
    context, exactly as the board does when its model files are missing.
    """
    ready = 0
    for site, repo_id in VISION_REPOS.items():
        target = VISION_MODEL_DIR / site / "model.onnx"
        if target.exists():
            ready += 1
            continue
        if not repo_id:
            logger.warning("No classifier repo configured for site {!r}.", site)
            continue

        remote = _find_onnx(repo_id)
        if remote is None:
            logger.warning(
                "Classifier for {!r} unavailable — {} could not be read. Both "
                "classifier repos are private, so the Space needs HF_TOKEN set "
                "as a secret with read access to them.",
                site,
                repo_id,
            )
            continue
        ready += _fetch(repo_id, remote, target)
    return ready


def ensure_all() -> dict:
    """Fetches every model. Returns a per-stage readiness map, which app.py
    surfaces in the UI so a visitor can see which stages are live."""
    logger.info("Preparing models in {} ...", MODELS_DIR)
    status = {
        "slm": ensure_slm(),
        "stt": ensure_stt(),
        "tts": ensure_tts() == len(_PIPER_FILES),
        "vision": ensure_vision() == len(VISION_REPOS),
    }
    logger.info("Model readiness: {}", status)
    return status


if __name__ == "__main__":
    ensure_all()
