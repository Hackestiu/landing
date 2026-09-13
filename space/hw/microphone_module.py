"""
Audio recording via the Logitech Brio 105 microphone, triggered by the D7 toggle button.
Recordings are saved to RECORDINGS_DIR tagged with the selected personality.

STT: faster-whisper with Gaudí domain optimizations (see transcribe()).
"""

import time
import wave
import re

import numpy as np

from config import MIC_DEVICE, MIC_SAMPLE_RATE, RECORD_CHUNK_SECONDS, RECORD_MAX_SECONDS, RECORDINGS_DIR
from logging_setup import logger

try:
    import sounddevice as sd  # type: ignore[import]
except ModuleNotFoundError:
    sd = None

try:
    from arduino.app_peripherals.microphone import Microphone  # type: ignore[import]
except ModuleNotFoundError:
    Microphone = None


# Encoder window, in seconds. See transcribe().
STT_CHUNK_LENGTH_S = 15

# Gaudí domain vocabulary — used by faster-whisper to recognize terms that appear frequently in the audioguide context.

DOMAIN_PROMPT = (
    "Cultura Viva audio guide in Barcelona about Antoni Gaudí, Sagrada Família basilica, "
    "Nativity, Passion, and Glory facades, Catalan modernisme architecture, Casa Batlló, "
    "Casa Milà, Park Güell, dragon and salamander sculptures, trencadís mosaics, "
    "viaductes, Casa Museu Gaudí, escalinata del drac, pavellons de consergeria, "
    "Plaça de la Natura, Sala Hipòstila, Turó de les Tres Creus, cúpula, and torres."
)

DOMAIN_KEYWORD_ALIASES = [
    "Antoni Gaudí",
    "Gaudí",
    "Barcelona",
    "Passeig de Gràcia",
    "Temple Expiatori",
    "Sagrada Família",
    "basilica",
    "facade",
    "Nativity facade",
    "Passion facade",
    "Glory facade",
    "modernisme",
    "Catalan",
    "Casa Batlló",
    "Casa Milà",
    "La Pedrera",
    "Park Güell",
    "Eixample",
    "trencadís",
    "salamander",
    "dragon",
    "catenary arch",
    "Viaductes",
    "Casa Museu",
    "Escalinata del drac",
    "Pavellons de consergeria",
    "Plaça de la Natura",
    "Placa de la Natura",
    "Sala Hipòstila",
    "Sala Hipostila",
    "Turó de les Tres Creus",
    "Turó de les 3 Creus",
    "Cúpula",
    "Façana del Naixement",
    "Façana de la Passió",
    "Torres",
]

# Post-transcription corrections: ASR commonly misspells these nouns.
_CORRECTIONS = {
    r"\bgaudi\b": "Gaudí",
    r"\bgaudy\b": "Gaudí",
    r"\bcasa batl[óo]\b": "Casa Batlló",
    r"\bcasa batio\b": "Casa Batlló",
    r"\bcasa batlow\b": "Casa Batlló",
    r"\bcasa bortlow\b": "Casa Batlló",
    r"\bcasa mila\b": "Casa Milà",
    r"\bpark guell\b": "Park Güell",
    r"\bparkway\b": "Park Güell",
    r"\btrencadis\b": "trencadís",
    r"\btrincadis\b": "trencadís",
    r"\bmodernism\b": "modernisme",
    r"\bcatalonian\b": "Catalan",
    r"\bdrag on\b": "dragon",
    r"\bplaca (de la )?natura\b": "Plaça de la Natura",
    r"\bsala hipostila\b": "Sala Hipòstila",
    r"\bturo (de les )?tres creus\b": "Turó de les Tres Creus",
    r"\bturo 3 creus\b": "Turó de les 3 Creus",
    r"\bfacana (del )?naixement\b": "Façana del Naixement",
    r"\bfacana (de la )?passio\b": "Façana de la Passió",
    r"\bcupula\b": "cúpula",
    r"\bescalinata (del )?drac\b": "Escalinata del drac",
    r"\bpavellons (de )?consergeria\b": "Pavellons de consergeria",
    r"\bcasa museu\b": "Casa Museu",
}


def build_hotwords() -> str:
    """Returns the domain keyword aliases joined into a single space-separated string for Whisper hotword biasing."""
    return " ".join(DOMAIN_KEYWORD_ALIASES)


def canonicalize_domain_entities(text: str) -> str:
    """Normalizes known ASR misspellings of domain proper nouns and architectural terms in transcribed text (e.g. 'gaudi' to 'Gaudí', 'park guell' to 'Park Güell'), leaving text with no matching pattern unchanged."""
    for pattern, replacement in _CORRECTIONS.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text.strip()


class MicrophoneManager:
    def __init__(self):
        """Initializes the manager, configuring the Arduino Microphone peripheral at 16kHz mono if the app_peripherals module is available; recording otherwise falls back to sounddevice."""
        self._mic = None
        if Microphone is not None:
            self._mic = Microphone(
                MIC_DEVICE,
                sample_rate=Microphone.RATE_16K,
                channels=Microphone.CHANNELS_MONO,
                buffer_size=Microphone.BUFFER_SIZE_SAFE,
                shared=False,
            )

    @property
    def available(self) -> bool:
        return sd is not None or self._mic is not None

    def start(self) -> None:
        if sd is None and self._mic is not None:
            self._mic.start()

    def record_until_stopped(self, is_recording):
        """Captures audio continuously — via sounddevice if available, otherwise via successive
        Microphone.record_wav() chunks — checking the is_recording predicate between chunks and
        stopping once it returns false or RECORD_MAX_SECONDS is reached.

        When sounddevice is used the stream is opened at the device's native sample rate
        (MIC_SAMPLE_RATE) and the result is resampled to 16 kHz so that faster-whisper
        always receives audio at its expected rate, regardless of the hardware.

        Returns the captured samples as a 1D int16 numpy array at 16 kHz, or None on error.
        """
        TARGET_RATE = 16000
        chunks = []
        if sd is not None:
            capture_rate = MIC_SAMPLE_RATE
            block_size = int(capture_rate * 0.05)   # ~50 ms blocks
            max_frames = int(RECORD_MAX_SECONDS * capture_rate)
            frames_read = 0

            try:
                with sd.InputStream(
                    device=MIC_DEVICE,
                    samplerate=capture_rate,
                    channels=1,
                    dtype="int16",
                    blocksize=block_size,
                    latency="high",
                ) as stream:
                    while frames_read < max_frames:
                        chunk, overflowed = stream.read(
                            min(block_size, max_frames - frames_read)
                        )
                        if overflowed:
                            logger.warning("ALSA input overflow while recording")
                        samples = np.asarray(chunk, dtype=np.int16).reshape(-1)
                        if len(samples) > 0:
                            chunks.append(samples.copy())
                            frames_read += len(samples)
                        if not is_recording():
                            break
            except Exception as exc:
                logger.exception("Continuous microphone capture failed: {}", exc)
                return None

            if not chunks:
                return None

            audio = np.concatenate(chunks)

            # Resample to 16 kHz if the hardware runs at a different rate
            if capture_rate != TARGET_RATE:
                try:
                    import soxr  # type: ignore[import]
                    audio_f = audio.astype(np.float32) / 32768.0
                    resampled_f = soxr.resample(audio_f, capture_rate, TARGET_RATE)
                    audio = (resampled_f * 32768.0).astype(np.int16)
                    logger.success(
                        "Resampled mic audio {} Hz -> {} Hz (soxr)",
                        capture_rate,
                        TARGET_RATE,
                    )
                except ImportError:
                    # soxr not available — try scipy
                    try:
                        from scipy.signal import resample_poly  # type: ignore[import]
                        import math as _math
                        g = _math.gcd(capture_rate, TARGET_RATE)
                        audio_f = audio.astype(np.float32)
                        audio_f = resample_poly(audio_f, TARGET_RATE // g, capture_rate // g)
                        audio = np.clip(audio_f, -32768, 32767).astype(np.int16)
                        logger.success(
                            "Resampled mic audio {} Hz -> {} Hz (scipy)",
                            capture_rate,
                            TARGET_RATE,
                        )
                    except ImportError:
                        # Last resort: integer decimation with a simple anti-alias FIR
                        # Works correctly when capture_rate is an exact integer multiple of TARGET_RATE
                        # (e.g. 48000 / 16000 = 3).  For other ratios it still works but is
                        # less accurate — good enough for speech recognition.
                        import math as _math
                        ratio = capture_rate / TARGET_RATE
                        if ratio == int(ratio):
                            n = int(ratio)
                            # Simple n-tap moving-average anti-alias filter before decimation
                            kernel = np.ones(n, dtype=np.float32) / n
                            audio_f = np.convolve(audio.astype(np.float32), kernel, mode="same")
                            audio = audio_f[::n].astype(np.int16)
                        else:
                            # Non-integer ratio: use numpy linear interpolation (crude but functional)
                            old_len = len(audio)
                            new_len = int(old_len * TARGET_RATE / capture_rate)
                            x_old = np.arange(old_len)
                            x_new = np.linspace(0, old_len - 1, new_len)
                            audio = np.interp(x_new, x_old, audio.astype(np.float32)).astype(np.int16)
                        logger.success(
                            "Resampled mic audio {} Hz -> {} Hz (numpy fallback)",
                            capture_rate,
                            TARGET_RATE,
                        )
            return audio

        elif self._mic is not None:
            elapsed = 0.0
            while elapsed < RECORD_MAX_SECONDS:
                chunk = self._mic.record_wav(duration=RECORD_CHUNK_SECONDS)
                chunks.append(np.asarray(chunk).reshape(-1))
                elapsed += RECORD_CHUNK_SECONDS
                if not is_recording():
                    break
        else:
            return None

        if not chunks:
            return None
        return np.concatenate(chunks)

    @staticmethod
    def save(button_id: str, model_name: str, audio: np.ndarray):
        """Writes raw audio samples to RECORDINGS_DIR as a 16-bit PCM, 16kHz mono WAV file, tagging the filename with a timestamp, button_id, and model_name. Normalizes unsigned 8-bit, floating-point, and signed 16-bit input formats to 16-bit PCM before writing. Returns the path to the written file."""
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        out_file = (
            RECORDINGS_DIR / f"recording_{timestamp}_{button_id}-{model_name}.wav"
        )

        audio = np.asarray(audio).reshape(-1)

        min_v = float(np.min(audio)) if len(audio) > 0 else 0.0
        max_v = float(np.max(audio)) if len(audio) > 0 else 0.0
        mean_v = float(np.mean(audio)) if len(audio) > 0 else 0.0
        logger.debug(
            "Mic buffer: dtype={}, length={}, min={:.2f}, max={:.2f}, mean={:.2f}",
            audio.dtype,
            len(audio),
            min_v,
            max_v,
            mean_v,
        )

        # Handle various audio formats
        if audio.dtype == np.uint8:
            samples = (audio.astype(np.int16) - 128) << 8
        elif np.issubdtype(audio.dtype, np.floating):
            if max(abs(min_v), abs(max_v)) <= 1.5:
                samples = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
            else:
                samples = np.clip(audio, -32768, 32767).astype(np.int16)
        else:
            samples = audio.astype(np.int16)

        max_sample = int(np.max(np.abs(samples))) if len(samples) > 0 else 0
        with wave.open(str(out_file), "wb") as wf:
            wf.setnchannels(1)  # Microphone.CHANNELS_MONO
            wf.setsampwidth(2)  # 16-bit = 2 bytes
            wf.setframerate(16000)  # Microphone.RATE_16K
            wf.writeframes(samples.tobytes())
        logger.success(
            "Audio saved to: {} (button {}, model {!r}, max amplitude: {}/32767, "
            "duration: {:.1f}s)",
            out_file,
            button_id,
            model_name,
            max_sample,
            len(samples) / 16000,
        )
        return out_file

    def _ensure_whisper(self):
        """Loads and caches the WhisperModel on first call, returning it (or None if the
        model directory is missing or faster-whisper is not installed). Subsequent calls
        are a no-op, so this is safe to call from both preload() and transcribe()."""
        if hasattr(self, "_whisper"):
            return self._whisper

        from config import STT_MODEL_PATH

        if not STT_MODEL_PATH.exists():
            logger.warning(
                "faster-whisper model not found at {}. Download it following the "
                "instructions in models/stt/README.md.",
                STT_MODEL_PATH,
            )
            self._whisper = None
            return None

        try:
            from faster_whisper import WhisperModel  # type: ignore[import]

            # int8 quantization + 4 threads: benchmark-validated for Cortex-A53 (UNO Q)
            self._whisper = WhisperModel(
                str(STT_MODEL_PATH),
                device="cpu",
                compute_type="int8",
                cpu_threads=4,
            )
            logger.success(
                "faster-whisper model loaded: {}", STT_MODEL_PATH.name
            )
        except ImportError:
            logger.warning(
                "faster-whisper is not installed. Add 'faster-whisper>=1.0.0' to "
                "requirements.txt and reinstall. Returning empty transcription string."
            )
            self._whisper = None
        except Exception as exc:
            logger.exception("Could not load faster-whisper model: {}", exc)
            self._whisper = None

        return self._whisper

    def preload(self) -> bool:
        """Eagerly loads the STT weights so the first transcription does not pay the
        cold-start cost. Returns True if the model is ready. Safe to call more than once;
        transcribe() still loads on demand if this was never called or failed."""
        return self._ensure_whisper() is not None

    def transcribe(self, audio_path) -> str:
        """Transcribes a WAV file to text with faster-whisper, biasing recognition toward Gaudí domain vocabulary via an initial prompt and hotwords, and canonicalizing known misspellings in the result. Lazily loads the WhisperModel on first call. Returns the transcribed text, or an empty string if the model file is missing, faster-whisper isn't installed, or transcription fails."""
        from config import STT_MODEL_PATH

        if not STT_MODEL_PATH.exists():
            logger.warning(
                "faster-whisper model not found at {}. Download it following the "
                "instructions in models/stt/README.md. Returning empty "
                "transcription string.",
                STT_MODEL_PATH,
            )
            return ""

        if self._ensure_whisper() is None:
            return ""

        try:
            segments, _ = self._whisper.transcribe(
                str(audio_path),
                language="en",
                beam_size=1,
                temperature=0.0,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
                condition_on_previous_text=False,
                initial_prompt=DOMAIN_PROMPT,
                hotwords=build_hotwords(),
                # Whisper pads every window to chunk_length seconds before the
                # encoder runs, so a 4 s question otherwise costs the same as a
                # 30 s one. Halving the window halves the encoder input
                # (3000 -> 1500 mel frames). Questions longer than 15 s are not
                # truncated -- they are processed as successive windows.
                chunk_length=STT_CHUNK_LENGTH_S,
                # Timestamp tokens are interleaved with text tokens and then
                # discarded below, so decoding them is wasted work.
                without_timestamps=True,
            )
            raw_text = " ".join(s.text for s in segments).strip()
            text = canonicalize_domain_entities(raw_text)
            logger.success(
                "Transcription: {!r}",
                text[:80] + ("..." if len(text) > 80 else ""),
            )
            return text
        except Exception as exc:
            logger.exception(
                "faster-whisper failed transcribing {}: {}", audio_path, exc
            )
            return ""
