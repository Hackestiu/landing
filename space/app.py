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

import os
import time
from pathlib import Path

import gradio as gr

import bootstrap
import minimap_render
from config import APP_DIR, DEFAULT_LOCATION, logger
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
    "artistic": "Bellesa, simbolisme i forma orgànica — evocador, ple de metàfores.",
    "technical": "Construcció, materials i estructura — precís, amb xifres.",
    "child": "Analogies senzilles i curiositats, per a nens de 6 a 12 anys.",
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


def run_guide(photo_path, audio_path, typed_question, site_label, button_id, visited):
    """Runs photo -> question -> answer -> speech and streams each stage's result
    into the UI as it completes, so a visitor watches the pipeline advance rather
    than waiting on one opaque call.

    `visited` is this visitor's set of discovered landmark codes, carried in a
    gr.State. It has to live per session: one Space process serves everybody, so
    a module-level set would light one visitor's map with another's photos.
    """
    site = SITES.get(site_label, DEFAULT_LOCATION)
    personality = models.name_for(button_id)
    visited = set(visited or ())

    element_out = ""
    question_out = ""
    answer_out = ""
    context_out = ""
    map_svg = minimap_render.render(site, visited)
    timings: list[str] = []

    def snapshot(audio=None):
        return (
            element_out,
            question_out,
            answer_out,
            context_out,
            "\n".join(timings),
            audio,
            map_svg,
            visited,
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

        # Where main.py calls minimap.mark_detected(site, element) on the board.
        # `unknown` and unmapped labels resolve to no codes and light nothing.
        fresh = minimap_render.codes_for(site, element or "")
        if fresh:
            visited |= set(fresh)
            map_svg = minimap_render.render(site, visited, current=fresh)
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

# ---------------------------------------------------------------------------
# Styling — carries the landing page's design system across, so the embedded
# demo does not read as a third-party widget dropped into the page.
#
# Tokens are taken from tailwind.config.mjs and src/styles/global.css at the
# repo root: paper/ink, the blue-green-ochre accents, Fraunces for headings,
# Inter for body, JetBrains Mono for the uppercase eyebrow labels, and the
# grain overlay. Change them there and mirror the change here.
# ---------------------------------------------------------------------------

PAPER = "#FBF8F2"
INK = "#1C2B30"
BLUE = "#2A6F97"
BLUE_DEEP = "#1D4E6E"
BLUE_SOFT = "#D9E9F2"
OCHRE = "#E2954A"

# Same fractalNoise overlay as the landing's `bg-grain` utility.
GRAIN = (
    "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' "
    "width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence "
    "type='fractalNoise' baseFrequency='0.9' numOctaves='2' "
    "stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' "
    "height='100%25' filter='url(%23n)' opacity='0.035'/%3E%3C/svg%3E\")"
)

# Trencadís mark, ported from src/components/TrencadisRow.astro. The tile list
# and the three geometry formulas are copied as formulas, not as the pixel
# values they produce, so the Astro and Gradio renders stay identical if either
# is tweaked. Purely decorative, hence aria-hidden.
TRENCADIS_TILES = (
    "#2A6F97", "#3F8362", "#E2954A", "#D4A72C", "#1D4E6E", "#2C5F47",
    "#B96F2C", "#2A6F97", "#3F8362", "#E2954A", "#D4A72C", "#2A6F97",
)


def _trencadis_html() -> str:
    tiles = "".join(
        f'<span style="background:{colour};'
        f"width:{6 + ((i * 7) % 9)}px;"
        f"height:{6 + ((i * 5) % 11)}px;"
        f'transform:rotate({(-1 if i % 2 == 0 else 1) * ((i * 3) % 8)}deg)"></span>'
        for i, colour in enumerate(TRENCADIS_TILES)
    )
    return f'<div class="cv-trencadis" role="presentation" aria-hidden="true">{tiles}</div>'


# The trencadís "C" — the same mark the landing serves as its favicon, used
# here both in the browser tab and in the header lockup.
MARK = APP_DIR / "assets" / "favicon.png"


def _mark_data_uri() -> str:
    """Returns the mark as a data: URI, or "" if the file is missing.

    Inlined rather than served as a static file: the mark is ~9 KB, and this
    keeps it out of Gradio's static-file allowlist, which otherwise has to be
    configured for anything the app serves from disk.
    """
    if not MARK.exists():
        logger.warning("Brand mark not found at {} — header will omit it.", MARK)
        return ""
    import base64

    return "data:image/png;base64," + base64.b64encode(MARK.read_bytes()).decode()

FONT_LINKS = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@500;600&display=swap">
"""

# Every token below is set for both light and dark: the landing commits to one
# light palette, and a visitor whose OS prefers dark should still see the same
# page rather than a half-inverted version of it.
# The first entry of each stack has to be a gr.themes.Font, not a bare string:
# Blocks() compares this theme against every built-in one, and those lead with a
# GoogleFont, so a str in slot 0 makes Font.__eq__ read .name off a str and the
# app dies at import with AttributeError. The families themselves are already
# fetched by FONT_LINKS, so plain Font (not GoogleFont) keeps the request count
# unchanged.
THEME = gr.themes.Base(
    font=[gr.themes.Font("Inter"), "ui-sans-serif", "system-ui", "sans-serif"],
    font_mono=[gr.themes.Font("JetBrains Mono"), "ui-monospace", "monospace"],
).set(
    body_background_fill=PAPER,
    body_background_fill_dark=PAPER,
    body_text_color=INK,
    body_text_color_dark=INK,
    body_text_color_subdued="rgba(28, 43, 48, 0.62)",
    body_text_color_subdued_dark="rgba(28, 43, 48, 0.62)",
    background_fill_primary=PAPER,
    background_fill_primary_dark=PAPER,
    background_fill_secondary="rgba(28, 43, 48, 0.02)",
    background_fill_secondary_dark="rgba(28, 43, 48, 0.02)",
    block_background_fill="#FFFFFF",
    block_background_fill_dark="#FFFFFF",
    block_border_color="rgba(28, 43, 48, 0.10)",
    block_border_color_dark="rgba(28, 43, 48, 0.10)",
    block_label_text_color=BLUE_DEEP,
    block_label_text_color_dark=BLUE_DEEP,
    block_title_text_color=BLUE_DEEP,
    block_title_text_color_dark=BLUE_DEEP,
    block_radius="16px",
    border_color_primary="rgba(28, 43, 48, 0.10)",
    border_color_primary_dark="rgba(28, 43, 48, 0.10)",
    input_background_fill="#FFFFFF",
    input_background_fill_dark="#FFFFFF",
    input_border_color="rgba(28, 43, 48, 0.14)",
    input_border_color_dark="rgba(28, 43, 48, 0.14)",
    input_radius="12px",
    button_large_radius="9999px",
    button_small_radius="9999px",
    button_primary_background_fill=INK,
    button_primary_background_fill_dark=INK,
    button_primary_background_fill_hover=BLUE_DEEP,
    button_primary_background_fill_hover_dark=BLUE_DEEP,
    button_primary_text_color=PAPER,
    button_primary_text_color_dark=PAPER,
    button_secondary_background_fill="rgba(28, 43, 48, 0.04)",
    button_secondary_background_fill_dark="rgba(28, 43, 48, 0.04)",
    button_secondary_text_color=INK,
    button_secondary_text_color_dark=INK,
    link_text_color=BLUE,
    link_text_color_dark=BLUE,
    color_accent=BLUE,
    color_accent_soft=BLUE_SOFT,
    color_accent_soft_dark=BLUE_SOFT,
)

CSS = f"""
body, gradio-app {{
  background-color: {PAPER} !important;
  background-image: {GRAIN};
}}

.gradio-container {{
  max-width: 1040px !important;
  margin: 0 auto !important;
  background: transparent !important;
}}

/* Fraunces for headings, matching the landing's h1-h4 rule. */
.cv-head h1, .cv-head h2, .gradio-container h1, .gradio-container h2, .gradio-container h3 {{
  font-family: Fraunces, ui-serif, Georgia, serif !important;
  font-weight: 500;
  color: {INK};
  letter-spacing: -0.01em;
}}

.cv-head h1 {{ font-size: 2rem; line-height: 1.15; margin: 0 0 .6rem; }}
.cv-head p {{ color: rgba(28, 43, 48, 0.72); max-width: 60ch; line-height: 1.65; }}

/* The landing's .eyebrow: mono, uppercase, very wide tracking. */
.cv-eyebrow {{
  font-family: "JetBrains Mono", ui-monospace, monospace;
  text-transform: uppercase;
  font-size: .72rem;
  letter-spacing: .28em;
  color: rgba(29, 78, 110, 0.8);
  margin-bottom: .9rem;
}}

/* Component labels get the same eyebrow treatment. */
.gradio-container .block > label > span,
.gradio-container span[data-testid="block-info"] {{
  font-family: "JetBrains Mono", ui-monospace, monospace !important;
  text-transform: uppercase;
  font-size: .66rem !important;
  letter-spacing: .16em;
}}

.gradio-container .block {{
  box-shadow: 0 12px 30px -14px rgba(28, 43, 48, 0.28);
}}

.gradio-container button.primary {{
  font-family: "JetBrains Mono", ui-monospace, monospace !important;
  text-transform: uppercase;
  letter-spacing: .16em;
  font-size: .72rem !important;
}}

/* Matches global.css's focus ring, which the landing sets for accessibility. */
.gradio-container :focus-visible {{
  outline: 3px solid {BLUE} !important;
  outline-offset: 3px;
}}

.cv-brand {{
  display: flex;
  align-items: center;
  gap: .7rem;
  margin-bottom: 1.6rem;
}}

.cv-mark {{
  width: 34px;
  height: 34px;
  display: block;
  border-radius: 4px;
}}

.cv-trencadis {{ display: flex; align-items: flex-end; gap: 3px; }}
/* In its second role the row is a divider under the header, so it needs the
   breathing room the gradient rule it replaced used to provide. */
.cv-head + .cv-trencadis {{ margin: 1.4rem 0 .4rem; }}
.cv-trencadis span {{ display: inline-block; border-radius: 2px; }}

.cv-wordmark {{
  font-family: Fraunces, ui-serif, Georgia, serif;
  font-weight: 600;
  font-size: 1.15rem;
  letter-spacing: -.01em;
  color: {INK};
}}
/* The landing sets "Viva" in blue; <em> carries that without italics. */
.cv-wordmark em {{ color: {BLUE}; font-style: normal; }}

.cv-note {{ font-size: .8rem; color: rgba(28, 43, 48, 0.55); line-height: 1.6; }}
.cv-note b {{ color: {OCHRE}; font-weight: 600; }}

/* --- The step flow ------------------------------------------------------ */

/* One tile per step, in the trencadís vocabulary of the row above it. Steps
   already passed stay lit, so the row reads as progress and not as tabs. */
.cv-steps {{
  display: flex;
  flex-wrap: wrap;
  gap: 1.1rem;
  margin: 1.6rem 0 1.2rem;
}}

.cv-step-pip {{
  display: inline-flex;
  align-items: center;
  gap: .5rem;
  font-family: "JetBrains Mono", ui-monospace, monospace;
  text-transform: uppercase;
  font-size: .64rem;
  letter-spacing: .18em;
  color: rgba(28, 43, 48, 0.35);
  transition: color 200ms ease;
}}

.cv-step-pip i {{
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 2px;
  opacity: .25;
  transform: rotate(-4deg);
  transition: opacity 200ms ease;
}}

.cv-step-pip.is-on {{ color: {BLUE_DEEP}; }}
.cv-step-pip.is-on i {{ opacity: 1; }}

.cv-nav {{ margin-top: 1rem; gap: .6rem; }}
.cv-nav button {{
  font-family: "JetBrains Mono", ui-monospace, monospace !important;
  text-transform: uppercase;
  letter-spacing: .16em;
  font-size: .7rem !important;
}}

/* --- The minimap -------------------------------------------------------- */

/* The board's screen, scaled up rather than redrawn: nearest-neighbour all the
   way, so the 4px tiles stay square and the pins keep their hand-cut edges. */
.cv-map svg {{
  width: 100%;
  height: auto;
  max-width: 480px;
  display: block;
  border-radius: 6px;
  image-rendering: pixelated;
  image-rendering: crisp-edges;
  box-shadow: 0 12px 30px -14px rgba(28, 43, 48, 0.45);
}}

.cv-map-col {{ min-width: 220px; }}

/* Below the two-column breakpoint the map goes first: on a phone it is the
   thing that orients you, and burying it under a webcam frame hides it. */
@media (max-width: 720px) {{
  .cv-stage {{ flex-direction: column-reverse !important; }}
  .cv-map svg {{ max-width: 320px; margin: 0 auto; }}
}}

footer {{ display: none !important; }}

@media (prefers-reduced-motion: reduce) {{
  .gradio-container *, .gradio-container *::before, .gradio-container *::after {{
    animation-duration: .01ms !important;
    transition-duration: .01ms !important;
  }}
}}
"""

# The flow, in the order a visitor works through it. The board asks the same
# four things — GPS fixes the site, the camera the element, a Modulino button
# the personality, the microphone the question — but it asks them one at a time,
# as you walk. Putting all four on one screen was the thing that made this read
# as a control panel.
STEPS = (
    ("Lloc", "On ets?"),
    ("Fotografia", "Què estàs mirant?"),
    ("Personalitat", "Qui t'ho explica?"),
    ("Pregunta", "Què vols saber?"),
)


def _steps_html(current: int) -> str:
    """The progress row: one trencadís tile per step, the current one lit."""
    tiles = "".join(
        f'<span class="cv-step-pip{" is-on" if i <= current else ""}">'
        f'<i style="background:{TRENCADIS_TILES[i * 2]}"></i>'
        f"<b>{name}</b></span>"
        for i, (name, _) in enumerate(STEPS)
    )
    return f'<div class="cv-steps" role="list">{tiles}</div>'


def go_to(step: int):
    """Shows one step and hides the rest.

    Every navigation goes through here, so the panels, the progress row and the
    two nav buttons can never disagree about which step is current.
    """
    step = max(0, min(step, len(STEPS) - 1))
    return (
        step,
        _steps_html(step),
        *[gr.update(visible=i == step) for i in range(len(STEPS))],
        gr.update(visible=step > 0),
        gr.update(visible=step < len(STEPS) - 1),
    )


def show_map(site_label: str, visited):
    """Redraws the map for whichever site is selected, keeping what's discovered.

    This is the board's set_location(): it swaps which map is on screen without
    touching visited state, so switching site and switching back does not wipe
    the landmarks already found.
    """
    return minimap_render.render(SITES.get(site_label, DEFAULT_LOCATION), visited or ())


with gr.Blocks(
    title="Cultura Viva — live pipeline", theme=THEME, css=CSS, head=FONT_LINKS
) as demo:
    # Per-session, never module-level: one process serves every visitor.
    # One set covers both maps — the two sites share no landmark code — so
    # switching site and back leaves everything already discovered still lit.
    step_state = gr.State(0)
    visited_state = gr.State(set())

    with gr.Column():
        gr.HTML(
            f"""
            <div class="cv-brand">
              <img class="cv-mark" src="{_mark_data_uri()}" alt="Cultura Viva">
              <span class="cv-wordmark">Cultura <em>Viva</em></span>
            </div>
            <div class="cv-head">
              <p class="cv-eyebrow">Demo en directe</p>
              <h1>El mateix pipeline, al teu navegador</h1>
              <p>Fes una foto d'un element de Gaudí, pregunta-li el que vulguis i
              escolta la resposta. Els models de visió, veu, llenguatge i síntesi
              són els mateixos fitxers que l'Arduino UNO Q executa sense connexió
              al monument; només la càmera, el micròfon, el GPS i els auriculars
              els fa el navegador.</p>
            </div>
            {_trencadis_html()}
            """
        )

        steps_bar = gr.HTML(_steps_html(0))

        with gr.Row(elem_classes="cv-stage"):
            with gr.Column(scale=3):
                with gr.Group(visible=True) as step_site:
                    site = gr.Radio(
                        choices=list(SITES),
                        value="Sagrada Família",
                        label="Lloc",
                        info="Al dispositiu, això ve del mòdul GPS.",
                    )

                with gr.Group(visible=False) as step_photo:
                    photo = gr.Image(
                        label="Fotografia",
                        type="filepath",
                        sources=["upload", "webcam"],
                        height=280,
                    )

                with gr.Group(visible=False) as step_personality:
                    personality_choice = gr.Radio(
                        choices=list(BUTTON_IDS),
                        value="A",
                        label="Personalitat del guia",
                        info="Els tres botons Modulino del dispositiu.",
                    )
                    personality_note = gr.Markdown(describe_personality("A"))

                with gr.Group(visible=False) as step_question:
                    voice_question = gr.Audio(
                        label="La teva pregunta",
                        sources=["microphone", "upload"],
                        type="filepath",
                    )
                    typed = gr.Textbox(
                        label="…o escriu-la",
                        placeholder="Why is this facade so different from the other one?",
                        info="Només s'utilitza si no hi ha cap gravació.",
                    )
                    gr.HTML(
                        '<p class="cv-note">Pregunta <b>en anglès</b>: el model de '
                        "transcripció del dispositiu és <code>faster-whisper "
                        "base.en</code>, que només entén anglès. És una limitació "
                        "real del maquinari, no de la demo.</p>"
                    )
                    run = gr.Button("Pregunta al guia", variant="primary")

                with gr.Row(elem_classes="cv-nav"):
                    back = gr.Button("Endarrere", visible=False)
                    # Primary until the last step, where "Pregunta al guia" is
                    # the action and this is hidden.
                    forward = gr.Button("Següent", variant="primary")

            # The map is not part of any step: it is the one thing on screen the
            # whole way through, previewing the site while you pick it and
            # keeping what you have found after that.
            with gr.Column(scale=2, elem_classes="cv-map-col"):
                minimap = gr.HTML(
                    minimap_render.render("sagrada_familia", ()),
                    elem_classes="cv-map",
                )

        # Hidden until there is something in them: four empty boxes under step 1
        # is most of what made the old single screen feel like a control panel,
        # and the demo is embedded in a fixed-height frame on the landing.
        with gr.Group(visible=False) as results:
            answer_audio = gr.Audio(
                label="Resposta en veu", autoplay=True, type="filepath"
            )
            element_box = gr.Textbox(label="Element detectat", interactive=False)
            question_box = gr.Textbox(label="Pregunta transcrita", interactive=False)
            answer_box = gr.Textbox(label="Resposta", interactive=False, lines=4)

        with gr.Accordion("Què ha rebut el model", open=False):
            context_box = gr.Textbox(
                label="Context del graf de coneixement",
                interactive=False,
                lines=12,
                info="Extret d'element_sheets.json, filtrat per personalitat.",
            )
            timing_box = gr.Textbox(
                label="Latència per etapa en aquest servidor",
                interactive=False,
                lines=5,
                info=(
                    "Temps del servidor, no del dispositiu. Els quatre nuclis "
                    "Cortex-A53 de l'UNO Q són força més lents — les xifres "
                    "reals són al benchmark del repositori del dispositiu."
                ),
            )

        with gr.Accordion("Estat del pipeline", open=False):
            gr.Markdown(_readiness_markdown())

    nav_outputs = [
        step_state,
        steps_bar,
        step_site,
        step_photo,
        step_personality,
        step_question,
        back,
        forward,
    ]
    back.click(lambda step: go_to(step - 1), inputs=step_state, outputs=nav_outputs)
    forward.click(lambda step: go_to(step + 1), inputs=step_state, outputs=nav_outputs)

    site.change(show_map, inputs=[site, visited_state], outputs=minimap)
    personality_choice.change(
        describe_personality, inputs=personality_choice, outputs=personality_note
    )
    run.click(lambda: gr.update(visible=True), outputs=results).then(
        run_guide,
        inputs=[photo, voice_question, typed, site, personality_choice, visited_state],
        outputs=[
            element_box,
            question_box,
            answer_box,
            context_box,
            timing_box,
            answer_audio,
            minimap,
            visited_state,
        ],
    )


def _credentials():
    """Reads the allowed logins from DEMO_USERS, formatted `user:pass,user:pass`.

    Usernames are free-form, so an email per person gives a whitelist where
    access can be revoked individually by editing one secret.

    Fails closed: with no credentials configured the app refuses to start, so a
    dropped secret takes the demo offline rather than silently publishing it.
    Set DEMO_PUBLIC=1 to run without a login on purpose — local development, or
    a deliberately open deployment.
    """
    raw = os.environ.get("DEMO_USERS", "").strip()
    if raw:
        pairs = [
            (u.strip(), pw)
            for entry in raw.split(",")
            if ":" in entry
            for u, pw in [entry.split(":", 1)]
            if u.strip() and pw
        ]
        if pairs:
            logger.info("Login required. {} account(s) configured.", len(pairs))
            return pairs
        raise SystemExit(
            "DEMO_USERS is set but no valid user:pass entries were parsed. "
            "Expected `alice@example.org:secret,bob@example.org:other`."
        )

    if os.environ.get("DEMO_PUBLIC") == "1":
        logger.warning("DEMO_PUBLIC=1 — serving with NO login.")
        return None

    raise SystemExit(
        "Refusing to start without a login. Set DEMO_USERS "
        "(`fly secrets set DEMO_USERS='you@example.org:password'`), or "
        "DEMO_PUBLIC=1 to serve the demo openly on purpose."
    )


if __name__ == "__main__":
    demo.queue(max_size=12).launch(
        server_name="0.0.0.0",
        server_port=7860,
        favicon_path=str(MARK) if MARK.exists() else None,
        auth=_credentials(),
        auth_message=(
            "Cultura Viva — the Arduino UNO Q pipeline, running live. "
            "Ask whoever shared this link for access."
        ),
    )
