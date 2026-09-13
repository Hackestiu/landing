"""
Vision classifier for Gaudí architectural elements.

Given the path of a photo taken by CameraManager and the location name
returned by LocationRegistry.current(), returns the name of the detected
element, 'unknown' for a non-recognizable element or a low-confidence
prediction, or None if no photo or model is available.

Current models:
  - Sagrada Família: models/vision/sagrada_familia/model.onnx + labels.json
    Classes: cupula, facana_naixement, facana_passio, laterals, posterior, torres, unknown
  - Park Güell:      models/vision/park_guell/model.onnx + labels.json
    Classes: 3_viaductes, casa_museu, escalinata_drac, pavellons_consergeria,
             placa_natura, sala_hipostila, turo_3_creus, unknown

Runtime dependencies: onnxruntime, Pillow, numpy (no torch / transformers needed).
    pip install onnxruntime pillow numpy
"""

import json
from pathlib import Path

import numpy as np

try:
    from logging_setup import logger
except ImportError:  # module used standalone, without the app root on sys.path
    from loguru import logger


try:
    from config import (
        MODELS_DIR,
        VISION_NON_RECOGNIZABLE_LABELS,
        VISION_UNKNOWN_LABEL,
    )

    VISION_MODEL_DIR = MODELS_DIR / "vision"
except ImportError:
    VISION_MODEL_DIR = Path(__file__).parent / "models" / "vision"
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


# Minimum softmax probability to accept a prediction as valid.
# Increase if the model produces false positives; decrease if it fails to detect.
CONFIDENCE_THRESHOLD = 0.5

# Recognized location folder names — each must contain model.onnx + labels.json.
LOCATION_MODEL_DIRS: dict[str, Path] = {
    "sagrada_familia": VISION_MODEL_DIR / "sagrada_familia",
    "park_guell": VISION_MODEL_DIR / "park_guell",
}


def _preprocess(image_path: Path, size: int, mean: list, std: list) -> np.ndarray:
    """Prepares an image for ONNX Runtime inference: resizes it preserving aspect ratio, takes a center crop of size×size, normalizes channels with the given mean and standard deviation, and returns a float32 NCHW array with a batch dimension of 1."""
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    scale = size / min(w, h)
    img = img.resize((round(w * scale), round(h * scale)), Image.BILINEAR)
    w, h = img.size
    left = (w - size) // 2
    top = (h - size) // 2
    img = img.crop((left, top, left + size, top + size))

    arr = np.asarray(img).astype(np.float32) / 255.0
    arr = (arr - np.array(mean, dtype=np.float32)) / np.array(std, dtype=np.float32)
    arr = arr.transpose(2, 0, 1)
    return np.expand_dims(arr, 0).astype(np.float32)


def _softmax(x: np.ndarray) -> np.ndarray:
    """Converts an array of logits into a normalized probability distribution."""
    e = np.exp(x - np.max(x))
    return e / e.sum()


# Main classifier class


class VisionClassifier:
    """Classifies photographs of architectural monuments using ONNX Runtime models."""

    def __init__(self):
        """Initializes the classifier with an empty per-location session cache; models are loaded lazily on first use."""
        self._sessions: dict = {}

    def classify(self, location: str, photo_path) -> str | None:
        """Classifies a photograph to identify the monument element it depicts, for a given site ('park_guell' or 'sagrada_familia'). Returns the detected label, 'unknown' if unrecognized or low-confidence, or None if the photo path is empty, missing, or the model is unavailable."""
        if photo_path is None:
            return None

        path = Path(photo_path)
        if not path.exists():
            logger.warning("Photo not found: {}", path)
            return None

        session_meta = self._load_session(location)
        if session_meta is None:
            return None

        session, meta = session_meta
        return self._run_inference(session, meta, path, location)

    def preload(self, location: str) -> bool:
        """Eagerly loads the ONNX session for one site so the first photo does not pay the
        onnxruntime import plus session-creation cost.

        Only the given site is loaded — the device is realistically at one monument per
        session, and the other site's model still loads on demand if the location changes.
        Returns True if the session is ready.
        """
        return self._load_session(location) is not None

    def _load_session(self, location: str):
        """Retrieves the cached ONNX InferenceSession and label metadata for a site, loading and caching them on first request. Returns None (and caches that outcome) if the location is unrecognized or its model files are missing or fail to load."""
        if location in self._sessions:
            return self._sessions[location]

        model_dir = LOCATION_MODEL_DIRS.get(location)
        if model_dir is None:
            logger.warning("Unknown location {!r}. Returning None.", location)
            self._sessions[location] = None
            return None

        onnx_path = model_dir / "model.onnx"
        labels_path = model_dir / "labels.json"

        if not onnx_path.exists():
            logger.warning(
                "ONNX model not found at {}. Ensure model.onnx and labels.json are "
                "in the model folder. Returning None.",
                onnx_path,
            )
            self._sessions[location] = None
            return None

        if not labels_path.exists():
            logger.warning(
                "labels.json not found at {}. Returning None.", labels_path
            )
            self._sessions[location] = None
            return None

        try:
            import onnxruntime as ort  # type: ignore[import]

            with open(labels_path, "r", encoding="utf-8") as f:
                meta = json.load(f)

            session = ort.InferenceSession(
                str(onnx_path),
                providers=["CPUExecutionProvider"],
            )
            self._sessions[location] = (session, meta)
            logger.success(
                "ONNX model loaded for {!r} from {}", location, model_dir
            )
            return self._sessions[location]

        except ImportError:
            logger.warning(
                "'onnxruntime' is not installed. Run: pip install onnxruntime "
                "pillow numpy. Returning None."
            )
            self._sessions[location] = None
            return None
        except Exception as exc:
            logger.exception("Error loading model for {!r}: {}", location, exc)
            self._sessions[location] = None
            return None

    def _run_inference(
        self, session, meta: dict, photo_path: Path, location: str
    ) -> str | None:
        """Runs the model on a photograph and resolves the winning class into a label, applying the non-recognizable-class check and the confidence threshold. Returns the predicted label if accepted, VISION_UNKNOWN_LABEL if it's a non-monument class or below CONFIDENCE_THRESHOLD, or None if preprocessing or inference fails."""
        try:
            pixel_values = _preprocess(
                photo_path,
                size=meta["image_size"],
                mean=meta["image_mean"],
                std=meta["image_std"],
            )

            outputs = session.run(["logits"], {"pixel_values": pixel_values})
            logits = outputs[0][0]
            probs = _softmax(logits)

            top_idx = int(np.argmax(probs))
            confidence = float(probs[top_idx])
            id2label = meta["id2label"]
            raw_label = str(id2label.get(str(top_idx), top_idx)).strip()
            label_lower = raw_label.lower()

            logger.success(
                "Classified as {!r} (confidence: {:.1%}, location: {})",
                raw_label,
                confidence,
                location,
            )

            # Check if predicted class is the non-recognizable elements class
            if label_lower in VISION_NON_RECOGNIZABLE_LABELS:
                logger.info(
                    "Photo classified as non-recognizable element ({!r} -> {!r}).",
                    raw_label,
                    VISION_UNKNOWN_LABEL,
                )
                return VISION_UNKNOWN_LABEL

            # Check confidence threshold for recognized monument elements
            if confidence < CONFIDENCE_THRESHOLD:
                logger.warning(
                    "Confidence {:.1%} < threshold {:.0%} — returning {!r}.",
                    confidence,
                    CONFIDENCE_THRESHOLD,
                    VISION_UNKNOWN_LABEL,
                )
                return VISION_UNKNOWN_LABEL

            return raw_label

        except ImportError:
            logger.warning(
                "'Pillow' is not installed. Run: pip install pillow. Returning None."
            )
            return None
        except Exception as exc:
            logger.exception("Error during inference: {}", exc)
            return None
