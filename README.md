# Creative Automation Pipeline

A multi-agent ADK pipeline that turns a YAML campaign brief into brand-compliant social ad creatives across three aspect ratios, two products, and three locales — with brand/legal compliance checks and a structured run report.

## What this is

A global consumer-goods brand launches hundreds of localized social campaigns per month. Manual creative production is slow, expensive, and inconsistent. This proof-of-concept demonstrates a working pipeline that ingests a campaign brief + brand guidelines, reuses cached hero images when available, generates new ones via GenAI when missing, composes them into 1:1 / 9:16 / 16:9 creatives with localized text overlays + market-specific disclaimers, runs deterministic brand and legal compliance checks, and emits a structured JSON report — all locally, all behind a swappable LLM and image-gen provider matrix.

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
  │           └── LegalCheckerAgent      disclaimer-rendered post-check
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
- **Text overlay is rule-based, not vision-aware.** Busy hero images may still cause legibility issues despite the 40% scrim. A real production system would use saliency detection or LLM vision to pick a clean text region.
- **Brand check is heuristic.** Palette distance (RGB Euclidean) + cv2 template matching pass-warn-fail thresholds are tuned for the demo brand; not human-grade.
- **Localization is data-driven**, not LLM-translated. The brief carries `campaign_message` keyed by locale; `MARKET_TO_LOCALE` picks the right one per market with `en` fallback.
- **Single-tenant; no auth.** Local runtime only.
- **Brief topology is frozen at module import.** Changing `CAMPAIGN_BRIEF_PATH` or the brief's `products` list requires restarting `adk web` — the per-product `ParallelAgent` is built once.
- **`adk web` chat input is ignored.** The root is a `SequentialAgent`, so any user message kicks the pipeline regardless of content. Reviewers should not expect natural-language routing.

## Running Tests

```bash
uv run pytest tests/
```

Covers Pillow composition + smart-crop, palette + logo detection, locale fallback, prohibited-word regex, and the full provider/image-provider dispatch matrix (mocked — no API calls).

## Demo Video

Recorded against `adk web`; covers cold-start image generation, cache reuse on re-run, and a provider-swap segment. *(Link to be embedded after recording.)*
