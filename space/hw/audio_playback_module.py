"""
TTS playback for synthesized voice responses from the Cultura Viva pipeline,
played through 3.5mm jack headphones (or an ALSA speaker), with volume controlled
dynamically via the Modulino Knob.

TTS engine: Piper (piper-tts Python library, in-process synthesis via
PiperVoice). Three personality voices are supported:

    artistic  -> libriTTS_r_medium  (en-US, neutral American English)
    technical -> semaine_spike      (en-GB, male British English)
    child     -> semaine_prudence   (en-GB, female British English)

Voice models (.onnx + .onnx.json) must be placed in python/models/tts/.
See models/tts/README.md for download instructions.

Uses 'aplay' (alsa-utils) and 'amixer' via subprocess for playback and volume.
"""

import os
import subprocess
import time
import wave
from io import BytesIO
from pathlib import Path
from typing import Optional

from config import DEFAULT_VOLUME_PERCENT, MODELS_DIR, PLAYBACK_DEVICE, RESPONSES_DIR
from logging_setup import logger


# Voice registry — maps voice keys to (onnx_stem, speaker_id)
_VOICE_REGISTRY: dict[str, tuple[str, Optional[int]]] = {
    "libriTTS_r_medium": ("en_US-libritts_r-medium", None),
    "semaine_spike": ("en_GB-semaine-medium", 1),
    "semaine_prudence": ("en_GB-semaine-medium", 0),
}

PERSONALITY_VOICE: dict[str, str] = {
    "artistic": "semaine_spike",
    "technical": "libriTTS_r_medium",
    "child": "semaine_prudence",
}

DEFAULT_VOICE = "libriTTS_r_medium"

_DEFAULT_TTS_MODELS_DIR = MODELS_DIR / "tts"


# AudioPlayer
class AudioPlayer:
    def __init__(self):
        """Initializes playback state at the configured device and default volume, with an empty voice cache (keyed by ONNX model stem) populated on preload or first synthesis."""
        self._device = PLAYBACK_DEVICE or "default"
        self._current_volume = DEFAULT_VOLUME_PERCENT
        self._tts_models_dir = _DEFAULT_TTS_MODELS_DIR
        self._voices: dict[str, object] = {}

    def _resolve_device(self):
        """Returns the ALSA device string to pass to aplay's -D flag."""
        return self._device or "default"

    def set_volume(self, volume_percent: int) -> bool:
        """Sets the system output volume to a percentage clamped within [0, 100], attempting to apply it across the common ALSA mixer controls ('Master', 'Headphone', 'Speaker', 'PCM') and tracking the level in software regardless of whether any control accepted it. Always returns True."""
        clamped = max(0, min(100, int(volume_percent)))
        self._current_volume = clamped

        mixer_controls = ["Master", "Headphone", "Speaker", "PCM"]
        success = False

        for ctl in mixer_controls:
            cmd = ["amixer", "-q", "set", ctl, f"{clamped}%"]
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=2)
                if res.returncode == 0:
                    success = True
            except Exception:
                pass

        logger.success(
            "Volume set to: {}%{}",
            clamped,
            "" if success else " (software tracked)",
        )
        return True

    @property
    def current_volume(self) -> int:
        return self._current_volume

    def play(self, audio_path, bridge=None) -> bool:
        """Plays a WAV file through ALSA's aplay. If a bridge is supplied, polls Bridge.call('get_volume') during playback so the Modulino knob can adjust volume in real time. Returns True on successful completion, and False if the file is missing, playback exceeds a 60-second safety timeout, aplay fails or isn't installed, or another playback error occurs."""
        path = Path(audio_path)
        if not path.exists():
            logger.error("Audio file not found: {}", path)
            return False

        device = self._resolve_device()
        cmd = ["aplay", "-q"]
        if device and device != "default":
            cmd += ["-D", device]
        cmd.append(str(path))

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            last_vol = self._current_volume
            start_time = time.time()
            while proc.poll() is None:
                if time.time() - start_time > 60:
                    proc.kill()
                    logger.error(
                        "aplay timed out playing {} (>60s) -- interrupted", path
                    )
                    return False
                if bridge is not None:
                    try:
                        vol = bridge.call("get_volume")
                        if vol is not None and vol != last_vol:
                            last_vol = vol
                            self.set_volume(vol)
                    except Exception:
                        pass
                time.sleep(0.05)

            if proc.returncode != 0:
                stderr = (
                    proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                )
                logger.error(
                    "aplay failed playing {}: {}", path, stderr.strip()
                )
                return False

            logger.success(
                "Played: {}{}", path, f" (device: {device})" if device else ""
            )
            return True
        except FileNotFoundError:
            logger.error("'aplay' not found on system -- install alsa-utils")
            return False
        except Exception as exc:
            logger.exception("aplay error playing {}: {}", path, exc)
            return False

    @staticmethod
    def save_response(audio_bytes: bytes) -> Path:
        """Writes raw WAV bytes to a timestamped file in RESPONSES_DIR and returns the resulting path."""
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        out_file = RESPONSES_DIR / f"response_{timestamp}.wav"
        out_file.write_bytes(audio_bytes)
        logger.success("TTS response saved to: {}", out_file)
        return out_file

    def synthesize(self, text: str, personality: str | None = None) -> Optional[Path]:
        """Synthesizes text into a WAV file using the voice assigned to the given personality ('artistic', 'technical', 'child'), falling back to the default voice if no personality is given, and writes the result into RESPONSES_DIR. Returns the output path, or None if the text is empty/whitespace or synthesis fails."""
        if not text or not text.strip():
            logger.warning("synthesize called with empty text — skipping.")
            return None

        voice_key = (
            PERSONALITY_VOICE.get(personality, DEFAULT_VOICE)
            if personality
            else DEFAULT_VOICE
        )
        wav_bytes = self._synthesize(text, voice_key)
        if wav_bytes is None:
            return None

        return self.save_response(wav_bytes)

    def synthesize_and_play(
        self, text: str, personality: str | None = None, bridge=None
    ) -> bool:
        """Synthesizes text with the personality's voice and plays it back on the configured output device, updating bridge UI state (processing/playback flags) around the transition if a bridge is provided. Aborts without playing if the bridge reports generation was cancelled beforehand. Returns True on successful playback, False if synthesis, cancellation, or playback fails."""
        out_file = self.synthesize(text, personality=personality)
        if out_file is None:
            return False

        if bridge is not None:
            try:
                if not bridge.call("is_processing_active"):
                    logger.info("Generation was cancelled before playback.")
                    return False
                bridge.call("set_processing_active", False)
                bridge.call("set_playback_active", True)
            except Exception as exc:
                logger.warning(
                    "Failed to set playback active on bridge: {}", exc
                )

        try:
            return self.play(out_file, bridge=bridge)
        finally:
            if bridge is not None:
                try:
                    bridge.call("set_playback_active", False)
                except Exception:
                    pass

    def _synthesize(self, text: str, voice_key: str) -> Optional[bytes]:
        """Runs Piper synthesis for the given text using the voice identified by voice_key, applying that voice's configured speaker id and prosody settings. Returns the resulting WAV bytes, or None if the voice cannot be loaded, synthesis produces no usable audio, or an error occurs."""
        voice_obj = self._load_voice(voice_key)
        if voice_obj is None:
            return None

        _, speaker_id = _VOICE_REGISTRY[voice_key]
        try:
            from piper import SynthesisConfig  # type: ignore[import]

            cfg = getattr(voice_obj, "config", None)
            length_scale = getattr(cfg, "length_scale", None)
            noise_scale = getattr(cfg, "noise_scale", None)
            noise_w_scale = getattr(cfg, "noise_w_scale", None) or getattr(
                cfg, "noise_w", None
            )

            syn_config = SynthesisConfig(
                speaker_id=speaker_id,
                length_scale=length_scale,
                noise_scale=noise_scale,
                noise_w_scale=noise_w_scale,
            )

            buf = BytesIO()
            with wave.open(buf, "wb") as wf:
                # synthesize_wav sets channels/width/rate itself (set_wav_format=True)
                voice_obj.synthesize_wav(text, wf, syn_config=syn_config)

            wav_bytes = buf.getvalue()
            if len(wav_bytes) <= 44:
                logger.error(
                    "Synthesis generated empty audio ({} bytes) for voice {!r}",
                    len(wav_bytes),
                    voice_key,
                )
                return None

            logger.success(
                "Synthesised {} bytes (voice {!r}).", len(wav_bytes), voice_key
            )
            return wav_bytes
        except Exception as exc:
            logger.exception(
                "Synthesis failed for voice {!r}: {}", voice_key, exc
            )
            return None

    def preload(self, voice_keys=None) -> int:
        """Eagerly loads the Piper voices so neither the first spoken response nor the first
        press of a not-yet-used personality button stalls on a cold voice load.

        Defaults to every voice in PERSONALITY_VOICE (two distinct ONNX files across the
        three personalities). Returns the number of voices successfully loaded; synthesis
        still falls back to loading on demand for anything that failed here.
        """
        if voice_keys is None:
            voice_keys = sorted(set(PERSONALITY_VOICE.values()))
        return sum(1 for key in voice_keys if self._load_voice(key) is not None)

    def _load_voice(self, voice_key: str) -> Optional[object]:
        """Returns the cached or newly loaded PiperVoice instance for voice_key, or None if the key is unrecognized, its model files are missing, or piper-tts is not installed."""
        entry = _VOICE_REGISTRY.get(voice_key)
        if entry is None:
            logger.warning(
                "Unknown voice {!r}. Available: {}",
                voice_key,
                ", ".join(_VOICE_REGISTRY),
            )
            return None

        onnx_stem, _ = entry

        # Cached per ONNX file, not per voice key: several personalities can share one
        # multi-speaker model (spike and prudence are both en_GB-semaine-medium) and only
        # differ by the speaker_id applied at synthesis time, so one load serves both.
        if onnx_stem in self._voices:
            return self._voices[onnx_stem]

        onnx_file = self._tts_models_dir / f"{onnx_stem}.onnx"
        json_file = self._tts_models_dir / f"{onnx_stem}.onnx.json"

        if not onnx_file.is_file():
            logger.warning(
                "ONNX model not found at {}. Download it following the "
                "instructions in models/tts/README.md. Returning empty audio.",
                onnx_file,
            )
            return None
        if not json_file.is_file():
            logger.warning("ONNX config not found at {}.", json_file)
            return None

        if not hasattr(self, "_piper_available"):
            try:
                from piper import PiperVoice  # type: ignore[import]

                self._piper_available = True
            except ImportError:
                logger.warning(
                    "piper-tts is not installed. Add 'piper-tts>=1.2.0' to "
                    "requirements.txt and reinstall. Returning empty audio."
                )
                self._piper_available = False

        if not self._piper_available:
            return None

        try:
            from piper import PiperVoice  # type: ignore[import]

            logger.info("Loading voice {!r} ...", voice_key)
            voice_obj = PiperVoice.load(str(onnx_file), str(json_file))
            self._voices[onnx_stem] = voice_obj
            logger.success(
                "Voice {!r} loaded ({} Hz).",
                voice_key,
                voice_obj.config.sample_rate,
            )
            return voice_obj
        except Exception as exc:
            logger.exception("Failed to load voice {!r}: {}", voice_key, exc)
            return None
