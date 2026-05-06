BUILD SPEC: Creative Automation Pipeline (FDE Take-Home)
Audience for this document: Claude Code (AI coding agent). Author intent: This is the authoritative build specification. Implement what is described here. Do not invent features outside this spec without asking. The verbatim source requirements are reproduced in Appendix A for traceability.

1. Mission
Build a working proof-of-concept that automates creative asset generation for social ad campaigns using Google's Agent Development Kit (ADK) for orchestration and Imagen 3 for image generation. The deliverable runs locally, accepts campaign briefs as YAML, reuses existing assets when present, generates new ones when absent, composes them into three aspect ratios with localized campaign messaging, and emits a structured run report.

Success looks like: running adk web, pasting a trigger message, and watching the outputs/ directory populate with brand-compliant, localized social ad creatives across three aspect ratios for two products, plus a report.json summarizing the run.

2. Non-Goals (Do Not Build)
❌ A custom web frontend. The adk web UI satisfies the demo requirement.
❌ Cloud storage integration. Local filesystem is in scope; cloud is abstracted behind an interface but not implemented beyond a single local adapter.
❌ A database. Session state and the JSON report are sufficient.
❌ User authentication, multi-tenancy, or an API server.
❌ LLM-based translation. Localized messages are provided in the brief as a locale-keyed map.
❌ Vision-based salience detection beyond Pillow's built-in capabilities.
❌ A real human-in-the-loop approval workflow.
❌ Any non-Imagen image generator. Single provider for the PoC.
3. Requirements Coverage Matrix
This is the contract. Every row must be satisfied by the implementation.

#	Requirement	Implementation Location
R1	Accept a campaign brief in JSON/YAML	BriefParserAgent reads inputs/campaign_briefs/*.yaml
R2	Brief includes ≥2 products	products array in brief YAML; ParallelAgent fans out
R3	Brief includes target region/market	region + markets fields
R4	Brief includes target audience	target_audience field
R5	Brief includes campaign message	campaign_message (locale-keyed map)
R6	Accept input assets, reuse when available	AssetManagerAgent checks inputs/assets/{product_id}/
R7	Generate via GenAI when missing	ImageGeneratorAgent calls Imagen 3
R8	Produce ≥3 aspect ratios (1:1, 9:16, 16:9)	CreativeComposerAgent produces all three
R9	Display campaign message on final post	Pillow text overlay in composer
R10	English minimum, localized a plus	Locale map; en required, others optional with fallback
R11	Runs locally	adk run (CLI) and adk web (local UI)
R12	Outputs organized by product + aspect ratio	outputs/{product_id}/{ratio}/{filename}.png
R13	README: how to run	Quick Start section
R14	README: example input/output	Examples section
R15	README: key design decisions	Design Decisions section
R16	README: assumptions/limitations	Assumptions section
R17	Bonus brand compliance checks	BrandCheckerAgent
R18	Bonus legal content checks	LegalCheckerAgent
R19	Bonus logging/reporting	ReportingAgent writes outputs/report_{ts}.json
R20	Public GitHub repo	Standard delivery
R21	2–3 min demo video	Recorded against adk web
4. Architecture
Multi-agent ADK pipeline. Brand guidelines are loaded once at initialization (a standing artifact). The campaign brief drives a parallel fan-out, one branch per product. Each product runs through a sequential sub-pipeline.

root_agent (SequentialAgent)
  ├── BrandLoaderAgent          → loads guidelines.yaml into session state
  ├── BriefParserAgent          → validates brief, resolves brand_id, writes to state
  ├── per_product (ParallelAgent — one branch per product)
  │     └── product_pipeline (SequentialAgent)
  │           ├── AssetManagerAgent      → cache check
  │           ├── ImageGeneratorAgent    → Imagen 3 if needed
  │           ├── CreativeComposerAgent  → 3 ratios × N locales + text overlay + logo
  │           ├── BrandCheckerAgent      → palette + logo position validation
  │           └── LegalCheckerAgent      → prohibited word + disclaimer scan
  └── ReportingAgent             → writes outputs/report_{ts}.json
Key architectural principle: brand guidelines and campaign briefs are separate input artifacts with separate lifetimes. Guidelines change yearly; briefs change per campaign. The brief references guidelines by brand_id. Do not collapse them into a single file.

5. Project Structure
Create exactly this structure. Do not add directories beyond this without justification.

creative-automation-pipeline/
├── README.md
├── pyproject.toml
├── .env.example
├── .gitignore
├── inputs/
│   ├── brand/
│   │   └── guidelines.yaml
│   ├── campaign_briefs/
│   │   └── summer_refresh_2025.yaml
│   └── assets/
│       ├── global/
│       │   └── logo.png                   # placeholder; commit a simple PNG
│       ├── aquavita_sparkling/            # empty → triggers Imagen
│       └── sunguard_spf50/
│           └── hero.png                   # present → reused
├── outputs/                                # gitignored
│   └── .gitkeep
├── creative_pipeline/
│   ├── __init__.py
│   ├── agent.py                           # root_agent definition (ADK convention)
│   ├── schemas.py                         # Pydantic models for brief + guidelines
│   ├── sub_agents/
│   │   ├── __init__.py
│   │   ├── brand_loader/
│   │   │   ├── __init__.py
│   │   │   └── agent.py
│   │   ├── brief_parser/
│   │   │   ├── __init__.py
│   │   │   └── agent.py
│   │   ├── asset_manager/
│   │   │   ├── __init__.py
│   │   │   └── agent.py
│   │   ├── image_generator/
│   │   │   ├── __init__.py
│   │   │   ├── agent.py
│   │   │   └── prompts.py
│   │   ├── creative_composer/
│   │   │   ├── __init__.py
│   │   │   └── agent.py
│   │   ├── brand_checker/
│   │   │   ├── __init__.py
│   │   │   └── agent.py
│   │   ├── legal_checker/
│   │   │   ├── __init__.py
│   │   │   └── agent.py
│   │   └── reporter/
│   │       ├── __init__.py
│   │       └── agent.py
│   └── tools/
│       ├── __init__.py
│       ├── imagen_tool.py
│       ├── pillow_composer.py
│       ├── color_analyzer.py
│       ├── storage_adapter.py
│       └── file_utils.py
├── fonts/
│   ├── Montserrat-Bold.ttf
│   └── OpenSans-Regular.ttf
└── tests/
    ├── __init__.py
    ├── test_pillow_composer.py
    ├── test_color_analyzer.py
    └── test_legal_checker.py
6. Input Artifact Schemas
6.1 Brand Guidelines — inputs/brand/guidelines.yaml
This is a standing artifact loaded once per pipeline run. Every agent that makes creative decisions reads from session state populated from this file.

brand_id: "aquacorp_global"
voice_and_tone:
  personality: ["refreshing", "honest", "energetic"]
  avoid: ["clinical", "aggressive", "hyperbolic"]
visual_identity:
  primary_colors: ["#00B4D8", "#FFFFFF", "#023E8A"]
  accent_colors: ["#90E0EF"]
  logo_path: "inputs/assets/global/logo.png"
  logo_placement: "top-right"          # one of: top-left | top-right | bottom-left | bottom-right
  safe_zone_pct: 0.08                  # logo must not be within 8% of any edge
typography:
  headline_font: "Montserrat-Bold.ttf"
  body_font: "OpenSans-Regular.ttf"
  text_color_on_dark: "#FFFFFF"
  text_color_on_light: "#023E8A"
imagery_style:
  mood: "bright, natural, outdoors"
  avoid: ["dark backgrounds", "artificial lighting", "cluttered scenes"]
  style_prompt_suffix: "photorealistic, vibrant, lifestyle photography, white background"
legal:
  prohibited_words: ["guaranteed", "best", "cure", "clinically proven"]
  required_disclaimers:
    MX: "Aplican términos y condiciones."
    BR: "Consulte os termos e condições."
    CO: "Aplican términos y condiciones."
6.2 Campaign Brief — inputs/campaign_briefs/summer_refresh_2025.yaml
campaign_id: "summer_refresh_2025"
brand_id: "aquacorp_global"            # resolves to guidelines.yaml
region: "LATAM"
markets: ["MX", "BR", "CO"]
target_audience: "Health-conscious adults 25-40"
campaign_message:
  en: "Refresh your summer, naturally."
  es: "Refresca tu verano, naturalmente."
  pt: "Renove seu verão, naturalmente."
products:
  - id: aquavita_sparkling
    name: "AquaVita Sparkling Water"
    category: beverage
    description: "Lightly carbonated mineral water with natural fruit essence"
  - id: sunguard_spf50
    name: "SunGuard SPF 50 Lotion"
    category: skincare
    description: "Reef-safe broad-spectrum sunscreen for active outdoor use"
6.3 Locale → Market Mapping
The composer uses this rule to pick which campaign_message[locale] to render for which market:

Market	Locale	Fallback Chain
MX	es	es → en
BR	pt	pt → en
CO	es	es → en
US	en	en
(other)	en	en
Implement this as a static dict in creative_pipeline/tools/file_utils.py. If the brief omits a locale, fall back to en. If en is missing, raise a validation error in BriefParserAgent.

7. Per-Agent Specifications
7.1 BrandLoaderAgent
Type: Custom function-based agent (or simple LlmAgent with a single tool call).
Inputs: Path to inputs/brand/guidelines.yaml (configurable via env var BRAND_GUIDELINES_PATH, default to that path).
Behavior: Parse the YAML, validate against the BrandGuidelines Pydantic model, write to session state under key brand.
Outputs to state: state["brand"]: BrandGuidelines
Failure mode: If file missing or invalid, halt with a clear error message naming the offending field.
7.2 BriefParserAgent
Inputs: Path to brief YAML (configurable via env var CAMPAIGN_BRIEF_PATH).
Behavior: Parse YAML, validate against CampaignBrief Pydantic model, confirm brief.brand_id == state["brand"].brand_id, write to state.
Outputs to state: state["brief"]: CampaignBrief
Validation: Must enforce ≥1 product (assignment requires ≥2 but the system should accept ≥1). Must enforce en locale present in campaign_message.
7.3 AssetManagerAgent (per-product)
Inputs: Reads state["brief"].products[i] for the current product branch.
Behavior: Look in inputs/assets/{product.id}/ for any of hero.png, hero.jpg, hero.jpeg, hero.webp. First match wins.
Outputs to state: state["products"][product_id]["hero_path"]: str | None, state["products"][product_id]["asset_source"]: "reused" | None
Logging: Emit a structured log line either way ("reused" or "absent → will generate").
7.4 ImageGeneratorAgent (per-product)
Activation: Only runs if state["products"][product_id]["hero_path"] is None.
Tool: imagen_tool.generate_hero_image(prompt: str, out_path: str) -> dict
Prompt construction (in prompts.py):
{product.name} ({product.category}). {product.description}.Target audience: {brief.target_audience}. Region: {brief.region}.Style: {brand.imagery_style.style_prompt_suffix}.Mood: {brand.imagery_style.mood}.Personality: {", ".join(brand.voice_and_tone.personality)}.Avoid: {", ".join(brand.imagery_style.avoid)}.
Model: imagen-3.0-generate-002 via google-genai SDK.
Output path: inputs/assets/{product_id}/hero.png — write back to inputs so subsequent runs reuse it (idempotency).
Outputs to state: state["products"][product_id]["hero_path"]: str, state["products"][product_id]["asset_source"]: "generated", plus latency in ms.
Pre-call hook: A before_model_callback invokes the legal checker on brief.campaign_message to prevent prohibited words from reaching Imagen.
7.5 CreativeComposerAgent (per-product)
This is the workhorse. Implement as a custom function agent calling pillow_composer extensively. No LLM call.

Inputs: Hero image path, brand guidelines, campaign brief, list of markets.
Behavior — for each (market, ratio) pair:
Open hero image.
Smart-crop to canvas size for the ratio (1080×1080, 1080×1920, or 1920×1080).
Determine backdrop luminance in the text region (bottom third by default); choose text_color_on_dark or text_color_on_light accordingly.
Add a semi-transparent scrim (rgba black at 40% opacity) behind the text region for legibility.
Render the campaign message in headline_font per the locale-mapping rule.
Render the disclaimer in body_font if brand.legal.required_disclaimers.get(market) exists.
Stamp the logo per logo_placement and safe_zone_pct.
Save to outputs/{product_id}/{ratio_label}/{market}_{locale}.png.
Ratio labels: 1x1, 9x16, 16x9 (use x not : — colons are problematic in filesystems).
Outputs to state: Per product, a list of every output path with its (market, locale, ratio) tuple.
7.6 BrandCheckerAgent (per-product, post-composition)
Tool: color_analyzer.dominant_palette(image_path, n=5) -> list[hex]
Behavior — per output file:
Extract dominant 5-color palette.
For each color, find nearest brand color (primary + accent) by Lab distance (use colormath or simple RGB Euclidean for the PoC). Score = mean distance.
Detect logo presence via OpenCV template matching against logo_path. Pass if match score > 0.7 and detected position is in the configured logo_placement quadrant.
Emit pass, warn, or fail with reason strings.
Outputs to state: state["products"][product_id]["brand_check"]: list[dict]
7.7 LegalCheckerAgent
Runs in two contexts:

As a pre-Imagen callback on brief.campaign_message (all locales). If any prohibited word matches (case-insensitive, word-boundary regex), halt the pipeline for that product with a clear violation message.
As a post-composition agent verifying that for every (product, market) output, the corresponding required_disclaimer text was actually rendered. (Tracked via a flag set by CreativeComposerAgent rather than re-OCRing.)
Outputs to state: state["products"][product_id]["legal_check"]: dict
7.8 ReportingAgent
Behavior: Walk session state, aggregate everything into a single JSON document, write to outputs/report_{ISO8601-timestamp}.json.
Schema:
{  "campaign_id": "...",  "brand_id": "...",  "started_at": "ISO-8601",  "completed_at": "ISO-8601",  "duration_ms": 12345,  "products": [    {      "product_id": "...",      "asset_source": "reused" | "generated",      "imagen_latency_ms": null | int,      "outputs": [        {"market": "MX", "locale": "es", "ratio": "1x1", "path": "outputs/...", "brand_check": "pass", "legal_check": "pass"}      ],      "brand_check_summary": "pass" | "warn" | "fail",      "legal_check_summary": "pass" | "fail",      "warnings": ["..."]    }  ]}
8. Tool Specifications
8.1 tools/imagen_tool.py
def generate_hero_image(prompt: str, out_path: str, aspect_ratio: str = "1:1") -> dict:
    """
    Calls Imagen 3 via google-genai SDK. Writes PNG to out_path.
    Returns: {"path": str, "latency_ms": int, "model": "imagen-3.0-generate-002"}
    """
Use the google-genai SDK (the new unified SDK), client.models.generate_images(). API key from env var GOOGLE_API_KEY (Gemini API path) — keep it simple, do not require Vertex AI auth for the PoC. Document the env var clearly in .env.example.

8.2 tools/pillow_composer.py
Pure Pillow, no LLM. Functions:

def smart_crop(img: Image, target_w: int, target_h: int) -> Image: ...
def add_text_overlay(img: Image, text: str, font_path: str, color: str,
                     position: str = "bottom-third", scrim: bool = True) -> Image: ...
def stamp_logo(img: Image, logo_path: str, placement: str, safe_zone_pct: float) -> Image: ...
def compose_creative(hero_path: str, ratio: str, message: str, disclaimer: str | None,
                     guidelines: BrandGuidelines, out_path: str) -> dict: ...
Smart crop strategy: center crop with bias toward upper third (where product hero shots usually live). If hero image aspect already matches target, just resize.

8.3 tools/color_analyzer.py
def dominant_palette(image_path: str, n: int = 5) -> list[str]: ...     # returns hex strings
def palette_distance(palette: list[str], brand_colors: list[str]) -> float: ...
def detect_logo(image_path: str, logo_path: str) -> dict:
    """Returns {"found": bool, "position": "top-right"|..., "match_score": float}"""
Use Pillow's quantize() for palette, OpenCV cv2.matchTemplate() for logo detection.

8.4 tools/storage_adapter.py
Define an interface and one local implementation. Do not implement cloud adapters. This exists to demonstrate the abstraction boundary.

from abc import ABC, abstractmethod

class StorageAdapter(ABC):
    @abstractmethod
    def write(self, path: str, content: bytes) -> str: ...
    @abstractmethod
    def read(self, path: str) -> bytes: ...
    @abstractmethod
    def exists(self, path: str) -> bool: ...

class LocalStorageAdapter(StorageAdapter):
    # implement against local filesystem
    ...
8.5 tools/file_utils.py
MARKET_TO_LOCALE: dict[str, list[str]] — the fallback chain mapping.
pick_locale(market: str, available_locales: list[str]) -> str — returns first match in fallback chain.
output_path(product_id: str, ratio: str, market: str, locale: str) -> str — canonical filename builder.
9. Pydantic Schemas — creative_pipeline/schemas.py
Define exhaustive Pydantic v2 models matching the YAML structures in §6. Required fields raise on missing; optional fields documented with defaults. Add validators for:

brand_id non-empty
campaign_message contains en
products has ≥1 entry
markets non-empty
color fields are valid hex strings (#RRGGBB)
logo_placement is one of the four allowed enum values
safe_zone_pct is between 0 and 0.5
10. Source Material — Borrow From These ADK Samples
These are in https://github.com/google/adk-samples/tree/main/python/agents:

Sample	What to Lift
image-scoring	Imagen tool wrapper pattern; LoopAgent for per-asset iteration
marketing-agency	Multi-agent orchestration skeleton; session state passing
brand-aligner	Brand compliance check prompts and approach
safety-plugins	before_model_callback pattern for legal check
Do not vendor entire sample directories. Adapt the relevant modules and credit the source in code comments where non-trivial logic is borrowed.

11. Build Order (de-risk the hard parts first)
Execute in this order. Verify each phase works before starting the next.

Scaffold — pyproject.toml, directory structure, empty Pydantic models, empty agent stubs, .env.example, .gitignore. Verify adk web starts and shows the agent in the dropdown.
Brief + brand loaders — implement BrandLoaderAgent and BriefParserAgent. Validate YAMLs load and write correctly to session state.
Asset manager — implement cache check, write tests verifying both branches (found / not found).
Pillow composer (de-risk the hardest piece early) — get 1:1 aspect ratio working end-to-end with a hardcoded hero image, before anything else. Then add 9:16 and 16:9. Then add localization and disclaimer rendering.
Imagen tool — adapt from image-scoring. Test in isolation with a hardcoded prompt before wiring to the agent.
ImageGeneratorAgent — wire Imagen tool into agent, verify the activation gate (only runs when asset missing).
End-to-end happy path — run the full pipeline for one product, then two products in parallel. Verify outputs structure.
BrandCheckerAgent — palette extraction first, then logo detection.
LegalCheckerAgent — both contexts (pre-Imagen callback + post-composition disclaimer check).
ReportingAgent — final aggregation.
README — write per §12 below.
Demo recording — per §13.
12. README Requirements
The README must contain these sections, in order:

Title + one-line description
What this is — one paragraph: problem, approach, outcome.
Quick Start
Prerequisites: Python 3.11+, uv (or pip), Google API key
git clone …
uv sync (or pip install -e .)
cp .env.example .env and fill in GOOGLE_API_KEY
Run with adk web (opens UI at localhost:8000) or adk run creative_pipeline
Trigger message: "Run the summer refresh campaign" (or similar)
Example Input — show snippets of guidelines.yaml and summer_refresh_2025.yaml. Reference the files in inputs/.
Example Output — show the outputs/ tree. Embed one rendered creative per aspect ratio. Show a report.json excerpt.
Architecture — paste the agent diagram from §4 here.
Key Design Decisions — five bullets covering:
Brand guidelines as a standing artifact, not brief fields
Parallel-per-product fan-out for scale
Asset caching for idempotency and cost control
Compliance separated from generation (legal pre-check, brand post-check)
Storage abstracted behind an interface for future cloud adapters
Assumptions and Limitations
Local storage only (cloud is one-class swap)
Imagen 3 only (other providers require new tool wrapper)
Text overlay is rule-based, not vision-aware (busy hero images may have legibility issues)
Brand check is heuristic (palette distance + template matching), not human-grade
Localization is data-driven (provided in brief), not LLM-translated
Single-tenant; no auth
Demo Video — embedded link to the 2–3 min walkthrough.
13. Demo Video Script (2–3 min)
For when the implementation is done. Recording target: under 3 minutes.

(0:00–0:15) Open outputs/ folder — empty. Show inputs/brand/guidelines.yaml and inputs/campaign_briefs/summer_refresh_2025.yaml briefly.
(0:15–0:30) adk web starts. Show the agent dropdown. Select pipeline.
(0:30–1:30) Paste trigger. Narrate the agent trace as it streams: BrandLoader → BriefParser → parallel product branches. Show outputs folder populating live.
(1:30–2:15) Open one rendered creative per aspect ratio. Open report.json, scroll the structure.
(2:15–2:45) Re-run the trigger. Highlight that Imagen does not re-fire (cache hit) — same output, near-instant.
(2:45–3:00) Brief mention of where cloud storage / human approval / additional providers would slot in.
14. Acceptance Criteria
The implementation is complete when all of the following hold:

[ ] adk web starts and the pipeline appears in the dropdown.
[ ] A fresh run with inputs/assets/aquavita_sparkling/ empty triggers Imagen and produces a hero.png.
[ ] A second run reuses the cached hero (verify with mtime or by intercepting Imagen call).
[ ] outputs/aquavita_sparkling/ contains 1x1/, 9x16/, 16x9/ subdirectories, each populated with one PNG per market.
[ ] At least one PNG renders the localized message (e.g., outputs/aquavita_sparkling/1x1/MX_es.png shows the Spanish text).
[ ] At least one PNG renders the required disclaimer for that market.
[ ] The logo appears in the configured corner with safe-zone respected.
[ ] outputs/report_*.json exists with the schema in §7.8.
[ ] Inserting a prohibited word into the brief halts the pipeline with a clear error before Imagen is called.
[ ] pytest tests/ passes.
[ ] README contains all sections from §12.
[ ] Repository is public on GitHub.
15. Tech Stack
Python 3.11+
Package manager: uv (preferred) or pip
Agent framework: google-adk (latest stable)
LLM provider: google-genai SDK with GOOGLE_API_KEY (Gemini API, not Vertex)
Image model: imagen-3.0-generate-002
Image processing: Pillow, opencv-python, numpy
Schema validation: pydantic v2
YAML: pyyaml
Testing: pytest
Linting: ruff (optional but encouraged)
pyproject.toml should pin major versions but not minor/patch.

16. Environment Variables — .env.example
# Required
GOOGLE_API_KEY=your_gemini_api_key_here

# Optional overrides
BRAND_GUIDELINES_PATH=inputs/brand/guidelines.yaml
CAMPAIGN_BRIEF_PATH=inputs/campaign_briefs/summer_refresh_2025.yaml
OUTPUT_DIR=outputs
IMAGEN_MODEL=imagen-3.0-generate-002
LOG_LEVEL=INFO
17. Things to Ask the Human Before Diverging From This Spec
If during implementation any of the following come up, stop and ask before deciding:

The Imagen API requires Vertex auth instead of the simple API key path.
A required Pillow operation can't be implemented without an additional dependency.
The ADK version's API shape differs significantly from what this spec assumes.
The agent trace UI in adk web doesn't show parallel branches the way the spec implies.
Logo detection via template matching produces unreliable results on the test image.
Any acceptance criterion in §14 cannot be met without spec changes.
Otherwise, follow the spec.

Appendix A: Verbatim Source Requirements
The following is the original assignment text reproduced verbatim for traceability.

FDE Take-Home Exercise: Creative Automation for Social Campaigns

Scenario: Creative Automation for Scalable Social Ad Campaigns

Client: A global consumer goods company launching hundreds of localized social ad campaigns monthly.

Business Goals:

Accelerate campaign velocity
Ensure brand consistency
Maximize relevance & personalization
Optimize marketing ROI
Gain actionable insights
Pain Points:

Manual content creation overload
Inconsistent quality & messaging
Slow approval cycles
Difficulty analyzing performance at scale
Resource drain
Objective: Design a creative automation pipeline that enables the creative team to generate variations for campaign assets.

Data Sources:

User inputs: Campaign briefs and assets uploaded manually
Storage: Storage to save generated or transient assets (Can be Azure, AWS or Dropbox)
GenAI: Best-fit APIs available for generating hero images, resized and localized variations
Task: Build a Creative Automation Pipeline (Proof of Concept)

Requirements (minimum):

Accept a campaign brief (in JSON, YAML, or another reasonable format) with:
Product(s) – at least two different products
Target region/market
Target audience
Campaign message
Accept input assets (can be in a local folder or mock storage) and reuse them when available
When assets are missing, generate new ones using a GenAI image model
Produce creatives for at least three aspect ratios (e.g., 1:1, 9:16, 16:9)
Display campaign message on the final campaign posts (English at least, localized is a plus)
Run locally (command-line tool or simple local app; your choice of language/framework)
Save generated outputs to a folder, clearly organized by product and aspect ratio
Include basic documentation (README) explaining:
How to run it
Example input and output
Key design decisions
Any assumptions or limitations
Nice to Have (optional for bonus points):

Brand compliance checks (e.g., presence of logo, use of brand colors)
Simple legal content checks (e.g., flagging prohibited words)
Logging or reporting of results
Deliverables:

A 2–3-minute video of the exercise working
A public GitHub repository containing the creative automation pipeline code and a comprehensive README file