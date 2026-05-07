# Creative Automation Pipeline

A multi-agent ADK pipeline that turns a YAML campaign brief into brand-compliant social ad creatives across three aspect ratios, two products, and three locales — with brand/legal compliance checks and a structured run report.

## What this is

A global consumer-goods brand launches hundreds of localized social campaigns per month. Manual creative production is slow, expensive, and inconsistent. This proof-of-concept demonstrates a working pipeline that ingests a campaign brief + brand guidelines, reuses cached hero images when available, generates new ones via GenAI when missing, composes them into 1:1 / 9:16 / 16:9 creatives with localized text overlays + market-specific disclaimers, runs deterministic brand and legal compliance checks, and emits a structured JSON report — all locally, all behind a swappable LLM and image-gen provider matrix.


## Assignment Requirement Coverage

| Requirement | Status |
|---|---|
| Accepts campaign brief | Yes — YAML campaign brief |
| Supports at least two products | Yes — AquaVita and SunGuard |
| Uses or generates assets | Yes — cached or generated hero assets |
| Creates at least three aspect ratios | Yes — 1x1, 9x16, 16x9 |
| Displays campaign message | Yes — localized campaign headline rendered on output |
| Saves outputs by product/aspect ratio | Yes — outputs/{product}/{ratio}/ |
| Documentation | Yes — README |
| Demo video | To be recorded |
| Nice-to-have: brand/legal checks | Yes |
| Nice-to-have: logging/reporting | Yes — JSON report |
| Extra: QC contrast check | Yes — final-render WCAG-style contrast QC |


## Quick Start

**Prerequisites:** Python 3.11+, [uv](https://docs.astral.sh/uv/) (or pip), and an API key for whichever provider you choose.

```bash
git clone <repo-url> creative-pipeline
cd creative-pipeline
uv sync                              # or: pip install -e .[dev]
bash scripts/fetch_fonts.sh          # downloads Montserrat-Bold + OpenSans-Regular
cp .env.example .env                 # then fill in OPENAI_API_KEY (default provider)
uv run adk web .                     # opens http://127.0.0.1:8000
```

In the agent dropdown, pick **`creative_pipeline`** and send any message — the message text is ignored, the pipeline kicks off regardless. Watch [outputs/](outputs/) populate. The first run on missing heroes calls the image-gen API; subsequent runs reuse the cached `inputs/assets/{product_id}/hero.png` and the pipeline finishes near-instantly.

CLI alternative: `uv run adk run creative_pipeline`.

### Provider Swap

The agent LLM (driven through LiteLLM) and the image-gen backend are independently swappable via env vars:

```
PROVIDER=openai          # google | openai | anthropic
MODEL=gpt-4o-mini        # any chat + tool-calling model for the chosen provider
IMAGE_PROVIDER=openai    # google (Imagen 3) | openai (gpt-image-1)
                         # defaults to PROVIDER for google/openai;
                         # required explicitly when PROVIDER=anthropic
IMAGE_MODEL=             # override default for the image backend
```

Strict coupling: Imagen 3 fires **only** when `IMAGE_PROVIDER=google`; gpt-image-1 fires **only** when `IMAGE_PROVIDER=openai`. There is no silent fallback.

| `PROVIDER` | `IMAGE_PROVIDER` (default) | Required keys |
|---|---|---|
| `openai` (default) | `openai` | `OPENAI_API_KEY` only |
| `google` | `google` | `GOOGLE_API_KEY` only |
| `anthropic` + `IMAGE_PROVIDER=google` | `google` (explicit) | `ANTHROPIC_API_KEY` + `GOOGLE_API_KEY` |
| `anthropic` + `IMAGE_PROVIDER=openai` | `openai` (explicit) | `ANTHROPIC_API_KEY` + `OPENAI_API_KEY` |

If `PROVIDER=anthropic` and `IMAGE_PROVIDER` is unset, the pipeline runs fine when all hero images are pre-cached but errors clearly the moment image generation is needed.

## Example Input

[inputs/brand/guidelines.yaml](inputs/brand/guidelines.yaml) — standing brand artifact (loaded once per run):

```yaml
brand_id: "aquacorp_global"
visual_identity:
  primary_colors: ["#00B4D8", "#FFFFFF", "#023E8A"]
  logo_path: "inputs/assets/global/logo.png"
  logo_placement: "top-right"
  safe_zone_pct: 0.08
typography:
  headline_font: "Montserrat-Bold.ttf"
  text_color_on_dark: "#FFFFFF"
  text_color_on_light: "#023E8A"
legal:
  prohibited_words: ["guaranteed", "best", "cure", "clinically proven"]
  required_disclaimers:
    MX: "Aplican términos y condiciones."
    BR: "Consulte os termos e condições."
```

[inputs/campaign_briefs/summer_refresh_2025.yaml](inputs/campaign_briefs/summer_refresh_2025.yaml) — per-campaign brief:

```yaml
campaign_id: "summer_refresh_2025"
brand_id: "aquacorp_global"
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
```

## Example Output

After a run, [outputs/](outputs/) is organized by product and aspect ratio:

```
outputs/
├── aquavita_sparkling/
│   ├── 1x1/  MX_es.png  BR_pt.png  CO_es.png
│   ├── 9x16/ MX_es.png  BR_pt.png  CO_es.png
│   └── 16x9/ MX_es.png  BR_pt.png  CO_es.png
├── sunguard_spf50/
│   ├── 1x1/  MX_es.png  BR_pt.png  CO_es.png
│   ├── 9x16/ MX_es.png  BR_pt.png  CO_es.png
│   └── 16x9/ MX_es.png  BR_pt.png  CO_es.png
└── report_20260506T213655Z.json
```

A sample creative — `outputs/sunguard_spf50/1x1/MX_es.png` — shows the Spanish headline "Refresca tu verano, naturalmente." with the Mexico disclaimer "Aplican términos y condiciones." and the brand logo stamped in the top-right with the configured 8% safe-zone margin.

`report_*.json` excerpt:

```json
{
  "campaign_id": "summer_refresh_2025",
  "brand_id": "aquacorp_global",
  "duration_ms": 4586,
  "products": [
    {
      "product_id": "aquavita_sparkling",
      "asset_source": "reused",
      "image_model": null,
      "image_gen_latency_ms": null,
      "outputs": [
        {"market": "MX", "locale": "es", "ratio": "1x1",
         "path": "outputs/aquavita_sparkling/1x1/MX_es.png",
         "brand_check": "pass", "legal_check": "pass"}
      ],
      "brand_check_summary": "pass",
      "legal_check_summary": "pass"
    }
  ]
}
```

## Quality Gates: Brand, Legal, and QC Checks

The pipeline does not stop at image generation. After each creative is composed, the system runs automated quality gates on the final rendered PNG outputs.

Current quality gates include:

- **Brand Check** — evaluates brand palette alignment, logo/accent usage, and visual consistency.
- **Legal Check** — confirms required disclaimer/legal copy is present.
- **QC Contrast Check** — measures text readability against the actual rendered background using WCAG-style contrast calculations.

The QC contrast check runs after composition, meaning it evaluates the final creative image rather than the prompt, source asset, or intended layout.

For each output, the report records:

- `qc_check`
- `contrast_ratio`
- `wcag_level`
- `text_color`
- `background_color`
- `qc_rules`

The QC system is modular. Rules are built from brand configuration in YAML, so stricter brands can adjust thresholds without changing Python code.

Example brand-side configuration:

```yaml
qc:
  min_contrast_ratio: 4.5            # WCAG AA normal text
  large_text_min_ratio: 3.0          # WCAG AA for large text
  large_text_size_threshold_px: 24

required_brand_checks:
  contrast_ratio: true
```

And the campaign brief decides whether a QC failure should halt the run:

```yaml
halt_on_qc_failure: false   # set true to abort on the first QC failure
```

### Natural copy-space composition + readability fallback order

Headline readability is delivered upstream by the photography itself, not by stamping a visible white panel behind every render. The brand's [`image_composition_guidance`](inputs/brand/guidelines.yaml) block tells the image-gen prompt what the photo needs to support — per-aspect product position, per-aspect negative-space location, things to avoid behind copy (striped towels, hands, labels, busy fabric, sharp horizon lines), things to prefer (soft ocean gradient, clean sky, smooth sand, blurred beach). For `premium_product_hero`, [`build_prompt`](creative_pipeline/sub_agents/image_generator/prompts.py) injects those fields into the gpt-image-1 / Imagen 3 request along with explicit "no text, no logos" guards.

The composer then runs a small ordered escalation when its contrast estimate falls below the brand threshold. The chain is configured in YAML (`layout.readability_fallback_order`) and walked in order until one step pushes contrast over the bar:

1. `choose_best_brand_text_color` — already runs in `_choose_text_treatment` before the chain.
2. `subtle_text_shadow` — perceptual readability bump from the brand's text shadow when configured.
3. `reposition_within_text_safe_area` — reserved for future placement re-routing; not yet implemented.
4. `subtle_local_gradient` — feathered, much softer than a hard panel.
5. `soft_panel_last_resort` — the visible panel, only painted as the last resort.

`readability_fallback_used` in the report records which step ended the walk (`natural_composition` when no escalation was needed). With the natural-negative-space prompt in place, most renders end at `natural_composition` and the visible panel never appears. QCCheckerAgent independently verifies the final PNG regardless of which step was reached. The QC check is a **WCAG-style final-render readability gate**, not a full accessibility certification.

### Defending the SunGuard AA-large finding

In the demo run, `aquavita_sparkling` outputs render white headline text on a darkened gradient and score **5.4:1** (WCAG AA pass). `sunguard_spf50` outputs render the navy headline on a tan beach-towel background and score **3.24:1**. The QC system reports this as **AA-large pass** rather than failure: WCAG 2.1 sets the AA threshold at **3.0:1 for large text** (≥18pt or ≥14pt bold) and our headline runs ~60–70px at 1080-tall canvas — well within the demo's configured large-text threshold. The result is technically WCAG-compliant for headline copy, but it sits well below the 4.5:1 normal-text bar, so the system surfaces it as a *visible signal* (`wcag_level: "AA-large"`, `contrast_ratio: 3.24`) rather than a silent pass. A brand that wants stricter copy for body or sub-headline use just bumps `min_contrast_ratio` to 7.0 (AAA) or sets `halt_on_qc_failure: true` to refuse to ship borderline creatives — no code change required.

## Architecture

```
root_agent (SequentialAgent)
  ├── BrandLoaderAgent          loads guidelines.yaml → state["brand"]
  ├── BriefParserAgent          loads brief, validates → state["brief"], state["product:{pid}"]
  ├── per_product (ParallelAgent — one branch per product)
  │     └── product_pipeline (SequentialAgent)
  │           ├── AssetManagerAgent      cache check
  │           ├── ImageGeneratorAgent    LlmAgent (LiteLLM) + image_gen tool
  │           │                          + before_model_callback for legal pre-check
  │           ├── CreativeComposerAgent  Pillow only (no LLM)
  │           ├── BrandCheckerAgent      palette + cv2 template match
  │           ├── LegalCheckerAgent      disclaimer-rendered post-check
  │           └── QCCheckerAgent         final-render contrast/readability QC
  └── ReportingAgent             aggregates state → outputs/report_*.json
```

## Key Design Decisions

1. **Brand guidelines as a standing artifact, not brief fields.** Guidelines change yearly; briefs change per campaign. Splitting them lets the brand stay the source of truth while briefs stay concise.
2. **Parallel-per-product fan-out.** Each product runs its own sequential sub-pipeline inside a `ParallelAgent`, so two products generate concurrently. Per-product state lives at flat keys `state["product:{pid}"]` to avoid races on a shared nested dict.
3. **Asset caching for idempotency and cost control.** `inputs/assets/{pid}/hero.png` is the cache; both the asset manager and the image generator's activation gate respect it. Re-running with cached heroes skips the LLM and the image API entirely.
4. **Compliance separated from generation.** Legal pre-check fires as a `before_model_callback` on the image generator's LLM call so prohibited words can never reach the image-gen API. Brand check is deterministic palette + template-match (not an LLM judgement) — fast, predictable, auditable.
5. **Storage abstracted behind an interface.** [storage_adapter.py](creative_pipeline/tools/storage_adapter.py) defines the boundary. The PoC ships only `LocalStorageAdapter`; cloud adapters (S3/GCS/Azure/Dropbox) slot in here.
6. **LiteLLM provider swap + parallel image-gen track.** The agent LLM and the image-gen backend are decoupled and individually swappable via `PROVIDER` / `IMAGE_PROVIDER` — see the matrix above.

## Assumptions and Limitations

- **Local storage only.** Cloud adapters are a one-class swap behind the existing interface but not implemented.
- **Image-gen backends:** Imagen 3 (`google`) and gpt-image-1 (`openai`). Anthropic has no native image API, so it requires `IMAGE_PROVIDER` to be set explicitly.
- **Text placement is rule-based, not saliency- or vision-aware.** The composer picks per-aspect headline boxes from the layout template, records the resulting headline box and rendered text color in the report, and `QCCheckerAgent` then measures actual final-render contrast against the WCAG-style threshold. A production version could swap the rule-based placement for saliency detection or LLM vision to pick a clean text region — but the QC gate would continue to verify the result regardless of how the placement was chosen.
- **Brand check is heuristic.** Palette distance (RGB Euclidean) + cv2 template matching pass-warn-fail thresholds are tuned for the demo brand; not human-grade.
- **Localization is data-driven**, not LLM-translated. The brief carries `campaign_message` keyed by locale; `MARKET_TO_LOCALE` picks the right one per market with `en` fallback.
- **Single-tenant; no auth.** Local runtime only.
- **Brief topology is frozen at module import.** Changing `CAMPAIGN_BRIEF_PATH` or the brief's `products` list requires restarting `adk web` — the per-product `ParallelAgent` is built once.
- **`adk web` chat input is ignored.** The root is a `SequentialAgent`, so any user message kicks the pipeline regardless of content. Reviewers should not expect natural-language routing.

### Composition Intelligence

The composer evaluates candidate headline regions using deterministic visual signals such as object clearance, texture, edge density, local contrast, and available text area. It then scales the headline to use clean safe space more confidently while preserving product/focal-object clearance. The final report records the selected headline box, font size, line count, prominence score, contrast result, and QC status.

## Running Tests

```bash
uv run pytest tests/
```

Covers Pillow composition + smart-crop, palette + logo detection, locale fallback, prohibited-word regex, the full provider/image-provider dispatch matrix (mocked — no API calls), the WCAG-style contrast math (`relative_luminance`, `contrast_ratio`, `wcag_level`, `passes_wcag_aa`), QC rule execution end-to-end against synthetic high- and low-contrast images via `QCCheckerAgent`, and the `halt_on_qc_failure` policy (failures recorded silently when false; `QCFailure` raised when true).

## Demo Video

Recorded against `adk web`; covers cold-start image generation, cache reuse on re-run, and a provider-swap segment. *(Link to be embedded after recording.)*

## Demo Talking Points

- **YAML-driven controls.** Every campaign-level choice (language, localization toggles, layout template, creative quality, palette guidance, force-generate / regenerate-cache flags, halt-on-QC-failure) lives in the brief; every brand-rule choice (colors, typography, logo treatment, layout templates, overlay style, accent style, contrast thresholds) lives in the brand guidelines. Switching campaigns or brands does not require touching Python.
- **AI generation plus deterministic composition.** Hero photography is generated by gpt-image-1 (or Imagen 3) under explicit force/regenerate flags and saved with sidecar provenance; everything else — smart crop, gradient overlay, headline placement, brand accent, logo badge, disclaimer placement — runs through deterministic Pillow code so the same brief produces the same final composition every time.
- **Audit-quality reporting.** Each run emits a single timestamped `report_*.json` whose per-output rows carry every field a reviewer would ask for: source asset path, image provider/model, latency, used-cache flag, headline/disclaimer/logo bounding boxes, overlay style and opacity, accent color, brand palette and element scores with reasons, and the full QC rule trace.
- **WCAG-style final-render QC.** `QCCheckerAgent` opens the rendered PNG, samples the actual background under the headline box (filtering out text pixels), and computes a WCAG 2.x-style contrast ratio against the rendered text color. This is a final-render readability check, not a full accessibility certification — but it catches the failure modes (busy backgrounds, color drift) that visual review would otherwise have to find by eye.
- **The SunGuard AA-large nuance.** Navy headline on tan beach-towel background scored 3.24:1. Rather than silently passing or hard-failing, the system surfaced `wcag_level: "AA-large"` so a reviewer can see exactly what they are accepting. A brand that wants stricter copy bumps `min_contrast_ratio` or sets `halt_on_qc_failure: true` — no code change.
- **Modular future QC rules.** `tools/qc_rules.py:build_rules(brand)` builds the active rule list from brand flags. Adding a new rule (minimum font size, focal-area collision, brand-color saturation in headline region, etc.) is a new `QCRule` subclass plus a flag in `RequiredBrandChecks` — `QCCheckerAgent` already iterates whatever `build_rules` returns.