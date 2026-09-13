"""
Personality selection and response generation.

Maps the three Modulino buttons (A/B/C) to named "guide personalities"
(artistic, technical, child by default, overridable via models/models.json),
retrieves factual context for a detected element from the knowledge base
files, and generates the spoken response using a local SLM (Qwen2.5-1.5B via
llama-cpp-python).
"""

import json

try:
    from logging_setup import logger
except ImportError:  # module used standalone, without the app root on sys.path
    from loguru import logger

try:
    from config import MODELS_CONFIG_FILE, MODELS_DIR, VISION_UNKNOWN_LABEL
except ImportError:
    from config import MODELS_CONFIG_FILE, MODELS_DIR

    VISION_UNKNOWN_LABEL = "unknown"

_DEFAULT_NAMES = {"A": "artistic", "B": "technical", "C": "child"}

# System prompts for each Personality (Cultura Viva pipeline).
# Keys must match the values in models/models.json ("artistic", "technical", "child").
PERSONALITY_PROMPTS: dict[str, str] = {
    "artistic": (
        "You are an enthusiastic tour guide passionate about art and symbolism. "
        "You explain Gaudí's works emphasizing beauty, organic shapes, and inspiration. "
        "Directly and strictly answer ONLY what the user asks—do not give unsolicited background or extra explanations. "
        "You speak with passion and use evocative metaphors. "
        "Keep your response strictly under 3 short sentences (maximum 50 words). "
        "Always respond in the same language as the user's question."
    ),
    "technical": (
        "You are a tour guide specialized in architecture and engineering. "
        "You explain Gaudí's works focusing on construction techniques, materials, "
        "Directly and strictly answer ONLY what the user asks—do not give unsolicited background or extra explanations. "
        "and structural innovations. You are precise, rigorous, and cite facts and dimensions. "
        "Keep your response strictly under 3 short sentences (maximum 50 words). "
        "Always respond in the same language as the user's question."
    ),
    "child": (
        "You are a friendly tour guide for children aged 6 to 12. "
        "You explain Gaudí's works in a simple, fun, and engaging way full of curious facts. "
        "Directly and strictly answer ONLY what the user asks—do not give unsolicited background or extra explanations. "
        "You use simple analogies and an animated tone. Avoid complicated words. "
        "Keep your response strictly under 3 short sentences (maximum 50 words). "
        "Always respond in the same language as the user's question."
    ),
}


class ModelRegistry:
    def __init__(self):
        """Initializes the button-to-personality mapping from defaults, then applies any overrides found in models/models.json."""
        self._names = dict(_DEFAULT_NAMES)
        self._load_overrides()

    def _load_overrides(self) -> None:
        """Applies personality-name overrides from models/models.json onto the default A/B/C mapping, ignoring unrecognized keys or malformed entries and leaving defaults untouched if the file is absent or unreadable."""
        if not MODELS_CONFIG_FILE.exists():
            return
        try:
            with open(MODELS_CONFIG_FILE, "r", encoding="utf-8") as f:
                overrides = json.load(f)
            for button, name in overrides.items():
                key = button.strip().upper()
                if key in self._names and isinstance(name, str) and name.strip():
                    self._names[key] = name.strip()
        except (OSError, ValueError) as exc:
            logger.warning("Could not read {}: {}", MODELS_CONFIG_FILE, exc)

    def name_for(self, button_id: str) -> str:
        """Returns the personality name assigned to a hardware button ('A', 'B', or 'C'), or the button id itself if it has no mapping."""
        return self._names.get(button_id.strip().upper(), button_id)

    @property
    def models_dir(self):
        """Exposes the root directory containing model weights, prompts, and configuration files."""
        return MODELS_DIR

    def _load_kg(self) -> None:
        """Lazily loads element_sheets.json into an index by element id and an alias index (alias, lowercased, mapped to id); a no-op once already loaded, and leaves both indices empty if the file is missing."""
        if hasattr(self, "_kg_index"):
            return
        from config import KG_PATH

        self._kg_index: dict = {}
        self._kg_alias_index: dict = {}  # alias.lower() -> id

        if not KG_PATH.exists():
            logger.warning(
                "element_sheets.json not found at {}. Returning empty KG context.",
                KG_PATH,
            )
            return

        try:
            with open(KG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            sheets = data.get("sheets", [])
            for sheet in sheets:
                sid = sheet.get("id", "")
                if sid:
                    self._kg_index[sid] = sheet
                    for alias in [sheet.get("name", "")] + sheet.get("aliases", []):
                        if alias:
                            self._kg_alias_index[alias.lower()] = sid
            logger.success(
                "Knowledge sheets loaded: {} elements", len(self._kg_index)
            )
        except (OSError, ValueError) as exc:
            logger.exception("Could not read element_sheets.json: {}", exc)

    def _load_kg_base(self) -> dict:
        """Lazily loads and caches knowledge_base.json, which provides monument-level overview context used as a fallback when a specific element sheet is unavailable."""
        if hasattr(self, "_kg_base"):
            return self._kg_base
        from config import KG_BASE_PATH

        self._kg_base: dict = {}
        if not KG_BASE_PATH.exists():
            return self._kg_base
        try:
            with open(KG_BASE_PATH, "r", encoding="utf-8") as f:
                self._kg_base = json.load(f)
            logger.success("Knowledge base loaded.")
        except (OSError, ValueError) as exc:
            logger.exception("Could not read knowledge_base.json: {}", exc)
        return self._kg_base

    def get_kg_context(self, element: str, personality: str = "artistic") -> str:
        """Retrieves and formats factual context for an architectural element, prioritizing fields according to the requested guide personality. Falls back to monument-level overview data if no specific element sheet is found, and returns an empty string if the element is empty, unknown, or absent from both knowledge files."""
        if not element or element == VISION_UNKNOWN_LABEL:
            return ""

        self._load_kg()

        # look up sheet by id, then by alias
        sheet = self._kg_index.get(element)
        if sheet is None:
            sid = self._kg_alias_index.get(element.lower())
            if sid:
                sheet = self._kg_index.get(sid)

        # fallback: monument level from knowledge_base.json
        if sheet is None:
            base = self._load_kg_base()
            entry = base.get(element, {})
            if entry:
                logger.info(
                    "KG: element {!r} served from knowledge_base.json", element
                )
                return self._render_kb_entry(entry)
            logger.warning(
                "KG: element {!r} not found in any knowledge file.", element
            )
            return ""

        return self._build_element_context(sheet, personality)

    def _build_element_context(self, sheet: dict, personality: str) -> str:
        """Assembles the full context block for an element sheet: its own facts, a short summary of its parent monument if any, and up to two related-element notes, following the shape used by benchmark.py's context builder but trimmed for the on-device SLM's small context window."""
        parts = [self._format_sheet(sheet, personality)]

        parent_id = sheet.get("parent")
        if parent_id and parent_id != sheet.get("id"):
            parent_sheet = self._kg_index.get(parent_id)
            if parent_sheet is not None:
                parts.append(self._render_parent_summary(parent_sheet))

        related = sheet.get("similarities", [])[:2]
        for note in related:
            parts.append(f"Related: {note}")

        return "\n---\n".join(p for p in parts if p)

    def _render_parent_summary(self, parent_sheet: dict) -> str:
        """Produces a compact 2-3 line summary of the parent monument or area a sub-element belongs to (e.g. Park Güell for the Dragon Stairway). Unlike benchmark.py's full-sheet renderer used for offline eval grounding, this stays short since it rides alongside the element's own facts."""
        name = parent_sheet.get("name", "")
        lines = [f"Part of: {name}"] if name else []
        if parent_sheet.get("creator"):
            lines.append(f"Creator: {parent_sheet['creator']}")
        if parent_sheet.get("inspiration"):
            lines.append(f"Context: {parent_sheet['inspiration']}")
        return "\n".join(lines)

    def _format_sheet(self, sheet: dict, personality: str) -> str:
        """Formats a knowledge sheet into a compact factual string for the SLM prompt, selecting technical, child-friendly, or artistic fields depending on the active personality."""
        lines: list[str] = []
        name = sheet.get("name", "")
        if name:
            lines.append(f"Element: {name}")

        # Fields shared by all personalities
        if sheet.get("creator"):
            lines.append(f"Creator: {sheet['creator']}")
        if sheet.get("timeline"):
            lines.append(f"Timeline: {sheet['timeline']}")

        if personality == "technical":
            for key in ("materials", "construction_process", "technical_figures"):
                val = sheet.get(key)
                if val:
                    if isinstance(val, list):
                        lines.append(f"{key}: {'; '.join(str(v) for v in val)}")
                    elif isinstance(val, dict) and val:
                        for k2, v2 in val.items():
                            lines.append(f"{k2}: {v2}")
                    else:
                        lines.append(f"{key}: {val}")
            for fact in sheet.get("technical_facts", []):
                lines.append(f"- {fact}")

        elif personality == "child":
            if sheet.get("inspiration"):
                lines.append(f"Inspiration: {sheet['inspiration']}")
            for fact in sheet.get("artistic_facts", [])[:3]:
                lines.append(f"- {fact}")
            for fact in sheet.get("general_knowledge_facts", [])[:2]:
                lines.append(f"- {fact}")

        else:  # artistic (default)
            if sheet.get("inspiration"):
                lines.append(f"Inspiration: {sheet['inspiration']}")
            for fact in sheet.get("artistic_facts", []):
                lines.append(f"- {fact}")
            for fact in sheet.get("general_knowledge_facts", []):
                lines.append(f"- {fact}")
            sims = sheet.get("similarities", [])
            if sims:
                lines.append(f"Connections: {'; '.join(sims)}")

        return "\n".join(lines)

    def _render_kb_entry(self, entry: dict) -> str:
        """Formats a monument-level knowledge_base.json entry into a labeled string, deriving each label from its JSON key so new fields appear automatically without code changes. The 'name' field is surfaced first; list-of-strings fields render as bullets, nested dicts are flattened one level, and empty or falsy values are skipped."""
        lines: list[str] = []
        if name := entry.get("name"):
            lines.append(f"Name: {name}")

        for key, val in entry.items():
            if key == "name" or not val:
                continue
            label = key.replace("_", " ").title()

            if isinstance(val, list):
                if all(isinstance(v, str) for v in val):
                    lines.append(f"{label}:\n- " + "\n- ".join(val))
                else:
                    lines.append(f"{label}: {'; '.join(str(v) for v in val)}")
            elif isinstance(val, dict):
                for k2, v2 in val.items():
                    lines.append(f"{k2.replace('_', ' ').title()}: {v2}")
            else:
                lines.append(f"{label}: {val}")

        return "\n".join(lines).strip()

    def _ensure_llm(self):
        """Loads and caches the Llama instance on first call, returning it (or None if the
        model file is missing or llama-cpp-python is not installed). Subsequent calls are
        a no-op, so this is safe to call both from preload() and from generate_response()."""
        if hasattr(self, "_llm"):
            return self._llm

        from config import SLM_MODEL_PATH

        if not SLM_MODEL_PATH.exists():
            logger.warning(
                "SLM model not found at {}. Download it following the instructions "
                "in models/README.md.",
                SLM_MODEL_PATH,
            )
            self._llm = None
            return None

        try:
            from llama_cpp import Llama  # type: ignore[import]

            self._llm = Llama(
                model_path=str(SLM_MODEL_PATH),
                n_ctx=1024,  # context window
                n_threads=4,  # Cortex-A53 has 4 cores
                n_threads_batch=4,  # parallelise prefill across the 4 cores
                n_batch=128,  # larger prefill batches amortise per-batch overhead
                # No n_gpu_layers: llama-cpp-python is built CPU-only here (the build in
                # models/README.md passes no GGML_OPENCL/GGML_VULKAN backend flag), so
                # requesting GPU offload was silently ignored. Inference runs on the
                # Cortex-A53 cores; add a backend at build time before offloading.
                use_mlock=True,  # lock weights in RAM
                flash_attn=True,  # reduces memory bandwidth during attention
                verbose=False,
            )
            logger.success("SLM model loaded: {}", SLM_MODEL_PATH.name)
        except ImportError:
            logger.warning(
                "llama-cpp-python is not installed. Add 'llama-cpp-python' to "
                "requirements.txt and reinstall. Response will be an error fallback."
            )
            self._llm = None
        except Exception as exc:
            logger.exception("Could not load SLM model: {}", exc)
            self._llm = None

        return self._llm

    def preload(self) -> bool:
        """Eagerly loads the SLM weights and the knowledge-base indices so the first user
        question does not pay the cold-start cost. Returns True if the SLM is ready.

        Safe to call more than once and safe to skip entirely: generate_response() still
        falls back to loading on demand if this was never called or failed.
        """
        self._load_kg()
        self._load_kg_base()
        return self._ensure_llm() is not None

    def generate_response(
        self,
        question: str,
        element: str | None,
        personality: str,
        kg_context: str,
        is_active_fn=None,
    ) -> str | None:
        """Generates a spoken audio-guide reply from the on-device SLM using token streaming,
        adopting the given personality and conditioning on the user's question, the detected
        element (if any), and retrieved factual context. Lazily loads and caches the Llama model
        on first call.

        If is_active_fn is provided, it is called between each generated token: if it returns
        False the generation loop is interrupted immediately and None is returned, allowing the
        caller to skip TTS and reset the pipeline without waiting for the full response.

        Returns the generated answer string, None if cancelled mid-generation, or a descriptive
        fallback string if the model file or the llama-cpp-python dependency is missing, or if
        inference fails.
        """
        from config import SLM_MODEL_PATH

        if not SLM_MODEL_PATH.exists():
            logger.warning(
                "SLM model not found at {}. Download it following the instructions "
                "in models/README.md. Response will be an error fallback.",
                SLM_MODEL_PATH,
            )
            return "(model not available — download the SLM to get responses)"

        if self._ensure_llm() is None:
            return "(llama-cpp-python not installed — install it to get responses)"

        system_prompt = PERSONALITY_PROMPTS.get(
            personality, PERSONALITY_PROMPTS.get("artistic", "You are a tour guide.")
        )

        user_content = question or "(no question provided)"
        if element == VISION_UNKNOWN_LABEL:
            user_content += (
                "\n\n[Visual recognition: The photo does not match any architectural element of this monument. "
                "Politely and concisely tell the user (in your assigned guide personality) that the photo does not seem "
                "to show a recognized monument element, and invite them to capture an architectural element if they'd like details.]"
            )
        elif element:
            user_content += f"\n\n[Detected element in photo: {element}]"
        if kg_context:
            user_content += (
                f"\n\n[Factual information about the element:\n{kg_context}]"
            )

        try:
            # stream=True lets us check for cancellation between each token so the
            # generation loop can be interrupted immediately when the user presses
            # the cancel button, instead of blocking for the full synchronous call.
            stream = self._llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=60,  # fewer decode steps → faster
                temperature=0.1,  # low temperature = more factual
                repeat_penalty=1.1,  # slight penalty helps model hit <eos> sooner
                stop=[
                    "\n\n",
                    "<|im_end|>",
                ],  # early-stop on double newline or chat end token
                stream=True,
            )

            tokens: list[str] = []
            for chunk in stream:
                # Check cancellation between every generated token
                if is_active_fn is not None and not is_active_fn():
                    logger.info(
                        "SLM generation cancelled by user after {} token(s).",
                        len(tokens),
                    )
                    return None

                delta = chunk["choices"][0].get("delta", {})
                token_text = delta.get("content", "")
                if token_text:
                    tokens.append(token_text)

            answer = "".join(tokens).strip()
            logger.success(
                "SLM response generated ({} characters).", len(answer)
            )
            return answer
        except Exception as exc:
            logger.exception("SLM failed to generate response: {}", exc)
            return "(error generating response)"
