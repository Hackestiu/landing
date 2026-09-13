"""
Cultura Viva — browser build of the Arduino UNO Q audio guide.

This is the board's pipeline with its hardware ends replaced by the browser.
`core/` and `hw/` are vendored from the device repo byte-for-byte, so the four
inference stages below are the same code, the same weights and the same prompts
that run on the UNO Q:

    photo  -> VisionClassifier.classify()      (ONNX, per-site classifier)
    voice  -> MicrophoneManager.transcribe()   (faster-whisper base.en, int8)
    answer -> ModelRegistry.generate_response()(Qwen2.5 GGUF via llama.cpp)
    speech -> AudioPlayer.synthesize()         (Piper, one voice per personality)

What the browser stands in for: the Brio webcam (upload or webcam capture), its
microphone (browser recording), the GPS (a site selector), the three Modulino
buttons (a radio group) and the headphone jack (an audio player). Nothing in the
reasoning path is stubbed.

One honest caveat, surfaced in the UI: a Space runs on x86 server cores, while
the UNO Q runs four Cortex-A53s with no dot-product extensions. This page shows
that the pipeline works; it does not show how long it takes on the board. For
that, see the per-stage figures from `python/benchmark.py` run on the device.
"""

import time
from pathlib import Path

import gradio as gr

import bootstrap
from config import DEFAULT_LOCATION, logger
from core.model_module import ModelRegistry
from core.vision_module import VisionClassifier
from hw.audio_playback_module import AudioPlayer
from hw.microphone_module import MicrophoneManager

# The three Modulino buttons on the device, in order. name_for() resolves each
# to its personality exactly as the sketch does when a button is pressed.
BUTTON_IDS = ("A", "B", "C")

SITES = {
    "Sagrada Família": "sagrada_familia",
    "Park Güell": "park_guell",
}

PERSONALITY_BLURB = {
    "artistic": "Beauty, symbolism and organic form — evocative, metaphor-led.",
    "technical": "Construction, materials and structure — precise, figures first.",
    "child": "Simple analogies and curious facts, for ages 6–12.",
}


# ---------------------------------------------------------------------------
# Startup: fetch weights, then warm every stage so the first visitor does not
# pay the cold-start cost — the same preload() calls main.py makes on boot.
# ---------------------------------------------------------------------------

STATUS = bootstrap.ensure_all()

vision = VisionClassifier()
models = ModelRegistry()
microphone = MicrophoneManager()
player = AudioPlayer()

logger.info("Preloading stages ...")
STATUS["vision"] = vision.preload(DEFAULT_LOCATION) and STATUS["vision"]
STATUS["slm"] = models.preload()
STATUS["stt"] = microphone.preload()
STATUS["tts"] = player.preload() > 0
logger.success("Stage readiness after preload: {}", STATUS)


def _readiness_markdown() -> str:
    rows = [
        ("Vision", "ONNX classifier", STATUS["vision"]),
        ("Speech-to-text", "faster-whisper base.en (int8)", STATUS["stt"]),
        ("Language model", "Qwen2.5 GGUF (llama.cpp)", STATUS["slm"]),
        ("Text-to-speech", "Piper", STATUS["tts"]),
    ]
    lines = ["| Stage | Model | Status |", "|---|---|---|"]
    for stage, model, ok in rows:
        lines.append(f"| {stage} | {model} | {'ready' if ok else 'unavailable'} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The pipeline, in main.py's order.
# ---------------------------------------------------------------------------


def _elapsed(start: float) -> str:
    return f"{time.perf_counter() - start:.2f}s"


def run_guide(photo_path, audio_path, typed_question, site_label, button_id):
    """Runs photo -> question -> answer -> speech and streams each stage's result
    into the UI as it completes, so a visitor watches the pipeline advance rather
    than waiting on one opaque call."""
    site = SITES.get(site_label, DEFAULT_LOCATION)
    personality = models.name_for(button_id)

    element_out = ""
    question_out = ""
    answer_out = ""
    context_out = ""
    timings: list[str] = []

    def snapshot(audio=None):
        return (
            element_out,
            question_out,
            answer_out,
            context_out,
            "\n".join(timings),
            audio,
        )

    if not photo_path and not audio_path and not (typed_question or "").strip():
        element_out = "Add a photo and a question to start."
        yield snapshot()
        return

    # 1. Vision — identify the element in the photo.
    if photo_path:
        start = time.perf_counter()
        element = vision.classify(site, photo_path)
        timings.append(f"vision    {_elapsed(start)}")
        if element is None:
            element_out = (
                "No classifier available for this site — answering from the "
                "monument-level knowledge base instead."
            )
        elif element == "unknown":
            element_out = "unknown — not a recognised element of this monument"
        else:
            element_out = element
        yield snapshot()
    else:
        element = None
        element_out = "No photo — answering from general monument context."
        yield snapshot()

    # 2. Speech-to-text — transcribe the spoken question.
    if audio_path:
        start = time.perf_counter()
        question = microphone.transcribe(audio_path)
        timings.append(f"stt       {_elapsed(start)}")
        question_out = question or "(nothing transcribed)"
    else:
        question = (typed_question or "").strip()
        question_out = question

    if not question:
        answer_out = "No question captured. Record one, or type it instead."
        yield snapshot()
        return
    yield snapshot()

    # 3. Knowledge graph — retrieve the facts for this element and personality.
    start = time.perf_counter()
    kg_context = models.get_kg_context(element or "", personality=personality)
    timings.append(f"knowledge {_elapsed(start)}")
    context_out = kg_context or "(no sheet matched — the model answers unassisted)"
    yield snapshot()

    # 4. SLM — generate the spoken answer.
    start = time.perf_counter()
    answer = models.generate_response(
        question=question,
        element=element,
        personality=personality,
        kg_context=kg_context,
    )
    timings.append(f"slm       {_elapsed(start)}")
    answer_out = answer or "(no answer generated)"
    yield snapshot()

    if not answer:
        return

    # 5. TTS — speak it in the personality's voice.
    start = time.perf_counter()
    wav_path = player.synthesize(answer, personality=personality)
    timings.append(f"tts       {_elapsed(start)}")
    yield snapshot(audio=str(wav_path) if wav_path else None)


def describe_personality(button_id: str) -> str:
    name = models.name_for(button_id)
    return f"**{name}** — {PERSONALITY_BLURB.get(name, '')}"


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

# No custom CSS: Gradio 6 moved the Blocks `css=` argument to launch(), and this
# page has to build identically under the 5.x pinned in README.md and under 6.x.
with gr.Blocks(title="Cultura Viva — live pipeline") as demo:
    with gr.Column():
        gr.Markdown(
            "# Cultura Viva — the UNO Q pipeline, in your browser\n"
            "Photograph a Gaudí element, ask a question, hear the answer. "
            "The vision, speech, language and voice models below are the same "
            "files the Arduino UNO Q runs offline at the monument; only the "
            "camera, microphone, GPS and headphones are replaced by the browser."
        )

        with gr.Row():
            with gr.Column():
                site = gr.Dropdown(
                    choices=list(SITES),
                    value="Sagrada Família",
                    label="Site",
                    info="On the device this comes from the GPS module.",
                )
                photo = gr.Image(
                    label="Photo",
                    type="filepath",
                    sources=["upload", "webcam"],
                    height=300,
                )
                button = gr.Radio(
                    choices=list(BUTTON_IDS),
                    value="A",
                    label="Guide personality",
                    info="The three Modulino buttons on the device.",
                )
                personality_note = gr.Markdown(describe_personality("A"))

            with gr.Column():
                voice_question = gr.Audio(
                    label="Your question",
                    sources=["microphone", "upload"],
                    type="filepath",
                )
                typed = gr.Textbox(
                    label="…or type it",
                    placeholder="Why is this façade so different from the other one?",
                    info="Only used when no recording is given.",
                )
                run = gr.Button("Ask the guide", variant="primary")
                answer_audio = gr.Audio(
                    label="Spoken answer", autoplay=True, type="filepath"
                )

        element_box = gr.Textbox(label="Element detected", interactive=False)
        question_box = gr.Textbox(label="Transcribed question", interactive=False)
        answer_box = gr.Textbox(label="Answer", interactive=False, lines=4)

        with gr.Accordion("What the model was given", open=False):
            context_box = gr.Textbox(
                label="Knowledge-graph context",
                interactive=False,
                lines=12,
                info="Retrieved from element_sheets.json, filtered by personality.",
            )
            timing_box = gr.Textbox(
                label="Stage latency on this server",
                interactive=False,
                lines=5,
                info=(
                    "Server timings, not device timings. The UNO Q's four "
                    "Cortex-A53 cores are considerably slower — see the "
                    "benchmark in the device repo for its real figures."
                ),
            )

        with gr.Accordion("Pipeline status", open=False):
            gr.Markdown(_readiness_markdown())

    button.change(describe_personality, inputs=button, outputs=personality_note)
    run.click(
        run_guide,
        inputs=[photo, voice_question, typed, site, button],
        outputs=[
            element_box,
            question_box,
            answer_box,
            context_box,
            timing_box,
            answer_audio,
        ],
    )


if __name__ == "__main__":
    demo.queue(max_size=12).launch(server_name="0.0.0.0", server_port=7860)
