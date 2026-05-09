# Creative Automation Pipeline

A multi-agent ADK pipeline that turns a YAML campaign brief into brand-compliant social ad creatives across three aspect ratios, two products, and three rendered language variants — with brand/legal compliance checks and a structured run report. The default brief produces 18 final creatives (2 products × 3 aspect ratios × 1 distribution market × 3 output locales: English, Spanish, Portuguese). One source hero is generated per product and shared across every locale variant; localization happens on the rendered text overlay (headline + disclaimer), not on the imagery.

## What this is

A global consumer-goods brand launches hundreds of localized social campaigns per month. Manual creative production is slow, expensive, and inconsistent. This proof-of-concept demonstrates a working pipeline that ingests a campaign brief + brand guidelines, reuses cached hero images when available, generates new ones via GenAI when missing, composes them into 1:1 / 9:16 / 16:9 creatives with localized text overlays + market-specific disclaimers, runs deterministic brand and legal compliance checks, and emits a structured JSON report — all locally, all behind a swappable LLM and image-gen provider matrix.

## Future Roadmap
While the current implementation primarily focuses on the core multi-agent orchestration pipeline and creative composition logic, future development is planned across several enterprise-focused areas required for production deployment. These enhancements include:

Security Enhancements
Role-Based Access Control (RBAC), Multi-Factor Authentication (MFA), encryption of data at rest and in transit, API security, audit logging, and enterprise compliance planning.
→ /future-roadmap/security-roadmap.md

Scalability Enhancements
Stateless deployment architecture, multiple app instances, load balancing, queue-based processing, and rollback/fallback deployment strategies.
→ /future-roadmap/scalability-roadmap.md

Observability Enhancements
Real-time dashboards, JSON and markdown audit integration, monitoring, alerting, logging, tracing, and automated operational notifications.
→ /future-roadmap/observability-roadmap.md

Computer Vision Enhancements
AI-assisted composition analysis, object detection, intelligent layout guidance, automated quality control, and brand compliance validation.
→ /future-roadmap/computer-vision-roadmap.md

User Interface Enhancements
Streamlit or React-based interfaces for non-technical users, asset upload workflows, campaign configuration forms, and potential Adobe API integrations for automated image preprocessing and creative operations.
→ /future-roadmap/ui-roadmap.md

These roadmap items represent planned future development intended to evolve the current proof-of-concept system into a scalable, secure, observable, and production-ready enterprise creative workflow platform.

## Quick Start

**Prerequisites:** Python 3.11+, [uv](https://docs.astral.sh/uv/) (or pip), an API key for whichever image-gen provider you'll use (default OpenAI), and a Dropbox developer app + token. Setup details below.

### One-time setup

```bash
# 1. Clone & install (with the Dropbox upload extra; ~30 s).
git clone <repo-url> creative-pipeline
cd creative-pipeline
uv sync --extra upload               # core deps + dropbox SDK
                                     # (drop --extra upload if you'll never upload)

# 2. Download the brand fonts (~5 s).
bash scripts/fetch_fonts.sh          # Montserrat-Bold + OpenSans-Regular

# 3. Create your .env from the template and fill in real values.
cp .env.example .env
# then edit .env — see § "Environment setup" below for every key.
```

### Standard demo run — recommended path

The full demo is one command. It reads the default brief + brand guidelines, generates or rerenders all creatives, writes `outputs/report_*.json`, and uploads the finished batch to Dropbox (assuming your `.env` has `DROPBOX_UPLOAD_ENABLED=true` and a valid `DROPBOX_ACCESS_TOKEN`).

```bash
.venv/bin/python scripts/run_pipeline.py
```

What happens:
- Reads `inputs/brand/guidelines.yaml` and `inputs/campaign_briefs/summer_refresh_2025.yaml`.
- Runs the agent graph (asset cache check → image gen → composition → brand / legal / QC checks → reporter → Dropbox uploader).
- Writes per-product PNGs under `outputs/{product_id}/{ratio}/{market}_{locale}.png`.
- Writes the run report as a paired set: `outputs/report_<ts>.json` (full audit) and `outputs/report_<ts>.md` (concise reviewer-facing summary). See [§ Reports](#reports).
- Refreshes `outputs/latest_report.json` and `outputs/latest_report.md` so you can `open outputs/latest_report.md` regardless of timestamp.
- Snapshots the finished `outputs/` tree to Dropbox.
- Prints a compact summary at the end:

```
Run complete.
Report JSON:     outputs/report_20260507T184413Z.json
Report Markdown: outputs/report_20260507T184413Z.md
Latest copies:   outputs/latest_report.json, outputs/latest_report.md
Dropbox:         uploaded 23 files to /runs/summer_refresh_2025/20260507T184413Z (failures=0)

Overall:
  Brand: warn
  Legal: pass
  QC:    pass

Products:
  - aquavita_sparkling: 9 outputs, QC pass
  - sunguard_spf50: 9 outputs, QC pass
```

In your Dropbox the run lands at:

```
Dropbox / Apps / TD-Creative-Pipeline-POC / runs / summer_refresh_2025 / <run_timestamp> /
```

(The `Apps/TD-Creative-Pipeline-POC/` segment is the App Folder sandbox; the `/runs/` segment comes from `DROPBOX_ROOT_FOLDER` in your `.env`. See [§ Optional Dropbox Upload](#optional-dropbox-upload) if you're using a Full-Dropbox app instead.)

The first run hits the image-gen API (~30-90 s per hero). Subsequent runs reuse the cached source heroes and finish in ~10 s of compute + ~6 s of parallel upload.

### Fresh run / force new hero images

When you want to demo *fresh* image generation rather than re-rendering against cached heroes — e.g. before a stakeholder demo, or after changing the brief / brand prompt — clear the local artifact caches first.

**Shortcut form** (uses the built-in CLI flag):

```bash
.venv/bin/python scripts/run_pipeline.py --clean-outputs
```

`--clean-outputs` removes everything under `outputs/` (preserving `.gitkeep`) before the run, equivalent to `rm -rf outputs/* && touch outputs/.gitkeep`.

**Long form** (explicit, also clears the ADK artifact cache):

```bash
cd ~/Code/TD-Creative-Pipeline-POC

rm -rf outputs/*
touch outputs/.gitkeep
rm -rf .adk/artifacts/*

# Verify the brief is configured to actually force regeneration:
grep -n "force_generate_hero\|regenerate_cached_assets" inputs/campaign_briefs/summer_refresh_2025.yaml
# Expected output:
#   7:force_generate_hero: true
#   8:regenerate_cached_assets: true

# Now run normally — image gen will fire because both flags are true.
.venv/bin/python scripts/run_pipeline.py
```

**Optional retention** — keep only the newest N report pairs:

```bash
.venv/bin/python scripts/run_pipeline.py --keep-reports 5
```

After the run, prunes older `report_*.json/.md` pairs and matching `dropbox_upload_*.json` manifests so only the newest 5 timestamps remain. The `latest_report.{json,md}` convenience copies are never touched. Default behavior (no flag): keep everything.

Why each step:
- `rm -rf outputs/*` removes prior renders, source heroes, sidecar JSON, and any stale Dropbox upload manifests.
- `touch outputs/.gitkeep` keeps the directory tracked by git after the wipe.
- `rm -rf .adk/artifacts/*` clears the ADK runtime's per-session artifact cache (separate from `outputs/`).
- The `grep` confirms the brief's two re-generation flags are on. If either reads `false`, the asset manager will reuse `inputs/assets/<product>/hero.png` instead of calling the image-gen API. Toggling them in the YAML is how you choose between "demo with cached heroes" and "demo fresh image gen".

### Environment setup

`.env` lives at the project root and is read with `override=True`, so values there beat anything in your shell. **`.env` is in `.gitignore` — do not commit it.** `.env.example` is the committed template; it contains only placeholders.

Minimum keys the standard run needs:

```ini
# 1. Image-gen provider — pick ONE provider and fill in its key.
PROVIDER=openai
MODEL=gpt-5
IMAGE_PROVIDER=openai
IMAGE_MODEL=gpt-image-1
OPENAI_API_KEY=sk-...

# 2. Dropbox upload — required for the standard demo run.
DROPBOX_ACCESS_TOKEN=your_token_here
DROPBOX_UPLOAD_ENABLED=true
DROPBOX_ROOT_FOLDER=/runs
DROPBOX_CREATE_SHARED_LINKS=false
DROPBOX_UPLOAD_PARALLELISM=8
```

`DROPBOX_ACCESS_TOKEN` and the scopes on your Dropbox app are the most common failure points — see [§ Optional Dropbox Upload](#optional-dropbox-upload) for setup, scope requirements, and troubleshooting. The pipeline does a preflight check at startup that catches expired tokens or missing scopes before image generation begins.

Other providers (Google / Anthropic) are configured in [§ Provider Swap](#provider-swap).

### Swapping Brand Guidelines and Campaign Briefs

Explain:

## Current demo inputs

Current brand rules:

inputs/brand/guidelines.yaml

This controls:
- brand name/id
- color palette
- logo path and logo placement
- typography
- layout templates
- aspect-ratio rules
- text sizing rules
- legal fallback copy
- required brand checks
- QC thresholds
- composition rules

Current campaign brief:

inputs/campaign_briefs/summer_refresh_2025.yaml

This controls:
- campaign id/name
- language
- localization settings
- markets
- target audience
- campaign message
- creative quality
- layout template
- force_generate_hero / regenerate_cached_assets
- products
- product descriptions
- prompt_keywords
- prompt_avoid
- campaign-specific disclaimer overrides

How to:
- To create a new campaign today, edit or copy the campaign brief YAML.
- To create a new brand today, edit or copy the brand guidelines YAML.
- The pipeline reads those files at runtime.
- No Python changes are required for normal campaign/brand changes.


### More Run modes

| Mode | Command | Use |

| **ADK Web** (advanced/debug) | `uv run adk web .` | Visual ADK orchestration in a browser. Use when debugging the multi-agent flow or doing a live walkthrough. **Not the recommended demo path.** See [§ Advanced / Debugging](#advanced--debugging). |
| **ADK interactive terminal** (advanced) | `uv run adk run creative_pipeline` | Same orchestration as ADK Web, terminal output. |

When `DROPBOX_UPLOAD_ENABLED=true` is set in `.env`, every mode above uploads to Dropbox automatically (the upload runs as the final step in the agent graph). For one-off forcing without editing `.env`, `scripts/run_pipeline.py --upload-dropbox` overrides any `DROPBOX_UPLOAD_ENABLED=false` and uploads anyway. The reverse one-off (skip upload despite `.env`) is a manual `unset DROPBOX_UPLOAD_ENABLED` before the run.

### Verify your install

```bash
.venv/bin/python -m pytest -q        # 281 tests; ~25 s
```

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

`.env` is authoritative. The package init ([creative_pipeline/__init__.py](creative_pipeline/__init__.py)) loads `.env` from a pinned path next to the package and passes `override=True`, so:

- the file is found even when `adk web` / `pytest` / scripts are launched from a different working directory,
- values in `.env` win over any inherited shell env (`export PROVIDER=...` in your `~/.zshrc` no longer silently shadows the file).

To verify which model the system actually resolved, look for this line on the first agent step:

```
creative_pipeline.tools.llm_factory INFO LLM resolved: provider=google (env) model=gemini-2.5-flash (env)
```

If you see `(default(openai))` or `(default(gpt-4o-mini))`, that means `.env` wasn't found and the in-code fallbacks fired — fix the file path or contents rather than re-running.

| `PROVIDER` | `IMAGE_PROVIDER` (default) | Required keys |
|---|---|---|
| `openai` (default) | `openai` | `OPENAI_API_KEY` only |
| `google` | `google` | `GOOGLE_API_KEY` only |
| `anthropic` + `IMAGE_PROVIDER=google` | `google` (explicit) | `ANTHROPIC_API_KEY` + `GOOGLE_API_KEY` |
| `anthropic` + `IMAGE_PROVIDER=openai` | `openai` (explicit) | `ANTHROPIC_API_KEY` + `OPENAI_API_KEY` |

If `PROVIDER=anthropic` and `IMAGE_PROVIDER` is unset, the pipeline runs fine when all hero images are pre-cached but errors clearly the moment image generation is needed.

#### Image-model name notes

- `IMAGE_PROVIDER=google` calls Google's `predict` endpoint, which accepts **Imagen** models only (`imagen-3.0-generate-002`, `imagen-4.0-generate-001`, …). The Gemini-image model `gemini-2.5-flash-image` ("Nano Banana") uses a different API surface (`generate_content` with `response_modalities=["IMAGE"]`) and is **not** supported by this backend yet — setting `IMAGE_MODEL=gemini-2.5-flash-image` will return a 404 from the Imagen endpoint.
- `IMAGE_PROVIDER=openai` defaults to `gpt-image-1`. Override via `IMAGE_MODEL`.

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
language: en                  # base/fallback language + image-gen prompt cue
localized_copy: true          # use campaign_message_localized[locale] per output
localized_legal_copy: true    # use disclaimer_text_localized[locale] per output
markets: ["US"]               # DISTRIBUTION market(s) — kept separate from language
output_locales: ["en", "es", "pt"]   # explicit language variants to render per market
target_audience: "Health-conscious adults 25-40"

campaign_message: "Refresh your summer, naturally."
campaign_message_localized:
  en: "Refresh your summer, naturally."
  es: "Refresca tu verano, naturalmente."
  pt: "Renove seu verão, naturalmente."

# Per-locale disclaimer override (used when localized_legal_copy=true).
# Falls back through brand fallbacks when a locale is missing here.
disclaimer_text: null
disclaimer_text_localized:
  en: "Terms and conditions apply."
  es: "Aplican términos y condiciones."
  pt: "Consulte os termos e condições."

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

### Language and localization

> **All language behavior is configured in the brief:** [inputs/campaign_briefs/summer_refresh_2025.yaml](inputs/campaign_briefs/summer_refresh_2025.yaml). Edit that file, save, re-run. The brief's header block summarizes the same model as this section.

**Three knobs, three jobs.** Distribution market and rendered language are
two separate axes — that decoupling is the contract that prevents both the
"all outputs are English" surprise and the "I got 54 files instead of 18"
surprise.

| Field | Job | Effect on output | Demo value |
|---|---|---|---|
| [`markets`](inputs/campaign_briefs/summer_refresh_2025.yaml) | **Distribution market(s).** Where the campaign ships. | Filename prefix (`US_*.png`). | `[US]` |
| [`output_locales`](inputs/campaign_briefs/summer_refresh_2025.yaml) | **Rendered language variants.** Which languages render per market. | Filename suffix (`*_en.png`, `*_es.png`, `*_pt.png`). | `[en, es, pt]` |
| [`language`](inputs/campaign_briefs/summer_refresh_2025.yaml) | **Image-gen prompt cue + fallback.** Tags the Imagen prompt for the source hero (one per product, shared across all locale variants); also the fallback if a locale entry is missing from the localized maps. | None on text overlay; only on the underlying hero photography's mood/style cue. | `en` |
| [`localized_copy`](inputs/campaign_briefs/summer_refresh_2025.yaml) | When `true`, headlines come from `campaign_message_localized[locale]` per output. When `false`, every output uses the single `campaign_message`. | Headline text per output. | `true` |
| [`localized_legal_copy`](inputs/campaign_briefs/summer_refresh_2025.yaml) | When `true`, disclaimers walk a localized chain (`disclaimer_text_localized[locale]` → brand fallbacks) per output. When `false`, every output uses `brand.legal.default_disclaimer`. | Disclaimer text per output. | `true` |

`target_region` is a related but **prompt-only** cue — it labels the hero's
cultural feel ("Region: LATAM") inside the image-gen prompt and does not
control market or locale fan-out.

**Output count = `products × ratios × markets × output_locales`.**
Demo brief: `2 × 3 × 1 × 3 = 18 final creatives`.

#### Common edits

All examples are diffs against the current demo brief.

```yaml
# Drop Portuguese — render only English + Spanish (12 creatives).
output_locales:
  - en
  - es
# (Keep the `pt` entries in campaign_message_localized / disclaimer_text_localized
# as a translation library — they're harmless when not referenced.)
```

```yaml
# Add a second distribution market — every locale renders for both markets
# (36 creatives: US_en/es/pt + CA_en/es/pt × 3 ratios × 2 products).
markets:
  - US
  - CA
```

```yaml
# Single-language English campaign — simplest path (6 creatives).
markets: ["US"]
output_locales: null            # or omit the field entirely
localized_copy: false
```

```yaml
# Per-market locale resolution (legacy mode) — Spanish in MX/CO, Portuguese
# in BR, driven by brand.market_locales instead of an explicit locale list.
markets: ["MX", "BR", "CO"]
output_locales: null            # absent = fall back to brand.market_locales
localized_copy: true
# Filenames: MX_es.png, BR_pt.png, CO_es.png. = 18 creatives at default 3 × 2.
```

**Per-locale hero generation is intentionally not implemented.** One source
hero per product is shared across every configured locale variant — the
headline + disclaimer overlay does the language work, and the hero
photography (a product on a backdrop) is generic enough to work across
languages. Generating one hero per locale would multiply Imagen API cost
by `len(output_locales)`.

**Disclaimer text** follows the same brief-first principle. Set
`disclaimer_text` (single-language override) or `disclaimer_text_localized`
(per-locale override) for campaign-specific legal copy — "Promotion ends
August 31, 2025." or similar. When both are unset, the composer falls back
to `brand.legal.default_disclaimer` / `brand.legal.required_disclaimers`,
keeping compliance boilerplate as the safety net. Resolution order is
brief-localized → brief-default → brand-localized → brand-default. All
fields are validated by the schema in [creative_pipeline/schemas.py](creative_pipeline/schemas.py).

## Example Output

After a run, [outputs/](outputs/) is organized by product and aspect ratio. With the default brief (2 products × 3 aspect ratios × 1 distribution market × 3 output locales), this is **18 final creatives** — one per locale variant, all rendered against the same source hero per product:

```
outputs/
├── aquavita_sparkling/
│   ├── 1x1/  US_en.png  US_es.png  US_pt.png
│   ├── 9x16/ US_en.png  US_es.png  US_pt.png
│   └── 16x9/ US_en.png  US_es.png  US_pt.png
├── sunguard_spf50/
│   ├── 1x1/  US_en.png  US_es.png  US_pt.png
│   ├── 9x16/ US_en.png  US_es.png  US_pt.png
│   └── 16x9/ US_en.png  US_es.png  US_pt.png
├── report_20260506T213655Z.json    # full machine-readable audit (verbose)
├── report_20260506T213655Z.md      # concise human-readable summary (recommended for review)
├── latest_report.json              # convenience copy of the newest run
├── latest_report.md
└── dropbox_upload_20260506T213655Z.json   # upload manifest (only when DROPBOX_UPLOAD_ENABLED=true)
```

Each `US_*.png` triplet shares the same hero imagery (one Imagen call per product) and differs only in the rendered headline + disclaimer overlay. To add Canada or Mexico as a *distribution market*, append to `markets:` in the brief — every output locale will then render against that market too (`MX_en.png`, `MX_es.png`, `MX_pt.png`, etc.). To swap which *languages* render per market, edit `output_locales`. See [§ Language and localization](#language-and-localization).

A sample creative — `outputs/sunguard_spf50/1x1/US_es.png` — shows the Spanish headline "Refresca tu verano, naturalmente." with the Spanish disclaimer "Aplican términos y condiciones." and the brand logo stamped in the top-right with the configured 8% safe-zone margin.

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
        {"market": "US", "locale": "es", "ratio": "1x1",
         "path": "outputs/aquavita_sparkling/1x1/US_es.png",
         "brand_check": "pass", "legal_check": "pass"}
      ],
      "brand_check_summary": "pass",
      "legal_check_summary": "pass"
    }
  ]
}
```

## Reports

Every run produces a **paired report** so machines and humans each have the right view:

| File | Audience | Purpose |
|---|---|---|
| `outputs/report_<ts>.json` | Machines / debugging | Full audit trail. Every score, every candidate the composer evaluated, every shift attempted, every QC rule's full result. Intentionally verbose (~10k+ lines on a typical run); not designed for human reading. |
| `outputs/report_<ts>.md` | Humans / review | Concise summary. Run metadata, overall pass/warn/fail, per-product output table, the per-output fields a reviewer actually scans (headline size, color, composition score, contrast / WCAG, logo position, disclaimer position). The verbose internals are *intentionally* omitted; if you need them, open the JSON. **Recommended starting point for reviewers.** |
| `outputs/latest_report.json` | Either | Convenience copy of the newest run's JSON, refreshed each run. Lets `open outputs/latest_report.md` work regardless of timestamp. |
| `outputs/latest_report.md` | Either | Same for the Markdown. |
| `outputs/dropbox_upload_<ts>.json` | Machines | Upload manifest (only when `DROPBOX_UPLOAD_ENABLED=true`). Lists every uploaded file's local + Dropbox path, any per-file failures, and shared links if requested. |

### Quick-open the latest run

```bash
open outputs/latest_report.md
```

(macOS opens it in your default Markdown viewer; on Linux, `xdg-open` or pipe through `glow`.)

### How the JSON and Markdown stay in sync

The Markdown is **rendered from the same in-memory dict** that becomes the JSON — they can't drift apart. The reporter writes them as a pair (`_write_report_pair` in [creative_pipeline/sub_agents/reporter/agent.py](creative_pipeline/sub_agents/reporter/agent.py)). When `DROPBOX_UPLOAD_ENABLED=true`, the `DropboxUploaderAgent` regenerates the Markdown after upload completes so the "Dropbox upload" line in the summary reflects the actual result (`uploaded N files → /path/`) instead of the placeholder "configured (pending upload)".

### Markdown report structure

The Markdown follows this skeleton (real example shipped in `outputs/latest_report.md`):

```
# Creative Pipeline Run Report

## Run Summary               — campaign / language / providers / Dropbox status

## Overall Status            — Brand / Legal / QC / Dropbox table

## Products                  — one section per product
### `aquavita_sparkling`
  - source origin, image provider/model, used cache, summary statuses, warnings
  - per-output table: market | ratio | output | brand | legal | QC | WCAG levels | composition score
### `sunguard_spf50`
  - same shape

## Composition Notes         — per-output: headline size/color/box/prominence,
                                logo position, disclaimer position/contrast,
                                composition score and warnings only

## Failures and Warnings     — every brand/legal/QC/composition warning surfaced;
                                "No blocking failures." when clean

## Files                     — JSON path, Markdown path, latest copies, manifest
```

Verbose internals (every box scored, every shift attempted, full QC rule responses) are **deliberately omitted** from the Markdown. If you need them, the JSON has every field for the same run.

### Retention

`outputs/` accumulates files. Two flags on `scripts/run_pipeline.py` keep the directory tidy:

- `--clean-outputs` — delete everything under `outputs/` (preserving `.gitkeep`) **before** the run. Use when you want a fresh image-generation demo.
- `--keep-reports N` — **after** the run, prune older `report_*.json/.md` pairs and matching `dropbox_upload_*.json` manifests so only the newest N timestamps remain. Default: keep everything. The `latest_report.{json,md}` convenience copies are never touched.

Combined: `.venv/bin/python scripts/run_pipeline.py --clean-outputs --keep-reports 5` gives you a fresh-from-scratch run with retention going forward.

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

## Optional Dropbox Upload

The standard demo run uploads to Dropbox automatically when `.env` has `DROPBOX_UPLOAD_ENABLED=true` and a valid `DROPBOX_ACCESS_TOKEN`. This section is the deep-dive reference: app setup, scope requirements, the App Folder vs Full Dropbox distinction, and troubleshooting. For the minimum-config runbook, see [§ Quick Start — Environment setup](#environment-setup).

The pipeline writes all artifacts to local `outputs/` first — **that remains the source of truth.** Dropbox is a post-run snapshot. The upload step is the final agent in the graph (`DropboxUploaderAgent`, after `ReportingAgent`), so `scripts/run_pipeline.py`, `adk run`, and `adk web` all upload through one code path. A missing token / SDK / scope / network issue never affects the local run — the agent records the failure in state and yields a "skipped" event instead of raising.

You can still **force upload from the CLI** even when `DROPBOX_UPLOAD_ENABLED=false` (or unset) in `.env`:

```bash
.venv/bin/python scripts/run_pipeline.py --upload-dropbox
```

The flag wins over the env var for that one invocation. There's also `--dropbox-shared-links` (best-effort public links for `report_*.json` and any gallery files) and `--dropbox-root <path>` (override `DROPBOX_ROOT_FOLDER`).

### Setup

1. **Create a Dropbox app** at <https://www.dropbox.com/developers/apps/create>. The "Permission type" you pick at creation time matters — see [App Folder vs Full Dropbox](#app-folder-vs-full-dropbox--pick-the-right-dropbox_root_folder) below.

2. **Enable the required scopes** on the App Console → your app → **Permissions** tab:

   | Scope | Purpose | Required? |
   |---|---|---|
   | `account_info.read` | Token validation (preflight) | **Yes** |
   | `files.content.write` | Upload + delete files | **Yes — this is the one most people forget** |
   | `sharing.write` | Public shared links | Only if you'll use `--dropbox-shared-links` |
   | `files.content.read` | Read uploaded files (future-proofing) | Recommended |

   Click **Submit** at the bottom. **Critical:** changing scopes invalidates existing tokens, so do this BEFORE generating a token.

3. **Generate an access token** (Settings tab → "Generated access token" → Generate). Tokens starting with `sl.u.A…` are short-lived (~4 hours) and work fine for POC demos. For unattended runs, use the OAuth refresh-token flow (not implemented in this POC).

4. **Add it to `.env`** along with the upload toggle:

   ```ini
   DROPBOX_ACCESS_TOKEN=sl.u.xxxx...
   DROPBOX_UPLOAD_ENABLED=true
   DROPBOX_ROOT_FOLDER=/             # see App Folder vs Full Dropbox below
   ```

   `.env` is already in `.gitignore` — don't commit it.

5. **Install the SDK** (optional `[upload]` extra; local runs don't need it):

   ```bash
   uv sync --extra upload
   # or
   pip install -e .[upload]
   ```

#### Preflight check at startup

When `DROPBOX_UPLOAD_ENABLED=true`, both `scripts/run_pipeline.py` and the `DropboxUploaderAgent` (used by `adk web`) run a preflight check **before** the pipeline burns minutes on image generation:

- ping `users/get_current_account` → catches expired or invalid tokens
- write a 1-byte sentinel file to `<DROPBOX_ROOT_FOLDER>/_preflight_<hex>.txt` and immediately delete it → catches missing `files.content.write` scope (the most common Dropbox-app misconfiguration)

On success you'll see:

```
DROPBOX_PREFLIGHT: valid (account: you@example.com); write scope OK
```

On failure you get a loud warning naming the exact missing scope or the expired-token error, with a link to the App Console. The pipeline still runs — the local `outputs/` are unaffected — and the per-file error is recorded in the upload manifest.

#### App Folder vs Full Dropbox — pick the right `DROPBOX_ROOT_FOLDER`

This is the most common gotcha. The path your files land at in Dropbox depends on the **Permission type** you picked when creating the app. You can see it in the App Console under your app's Settings tab.

| Permission type | Where files actually land in Dropbox | Set `DROPBOX_ROOT_FOLDER` to | Trade-off |
|---|---|---|---|
| **Scoped App (App Folder)** | `/Apps/<your_app_name>/<paths from API>` — sandboxed | **`/`** (so paths start at the sandbox root, no doubled folder name) | Tightest scope; app can only access its own folder |
| **Scoped App (Full Dropbox)** | `<API path>` — at the root of the user's Dropbox | **`/TD-Creative-Pipeline-POC`** (or any absolute path you choose) | Broader scope; app can read/write the user's whole Dropbox |

**Why this matters:** if you have an App Folder app AND set `DROPBOX_ROOT_FOLDER=/TD-Creative-Pipeline-POC`, every upload lands at `/Apps/TD-Creative-Pipeline-POC/TD-Creative-Pipeline-POC/<rest>` — the app-folder name shows up **twice** because the sandbox already prefixes it. Setting `DROPBOX_ROOT_FOLDER=/` avoids that.

**Which to pick:** App Folder is the safer default for a POC. If you go this route, find your files at <https://www.dropbox.com/home/Apps> in the Dropbox web UI — they won't appear at the dropbox root because the sandbox isn't there.

### Usage

The `DropboxUploaderAgent` reads three env vars. The CLI flags on `scripts/run_pipeline.py` are convenience shortcuts — they just translate into the same env vars before the agent graph runs, so `adk web` and `scripts/run_pipeline.py` share one upload code path.

| Env var | CLI shortcut on `run_pipeline.py` | Default |
|---|---|---|
| `DROPBOX_UPLOAD_ENABLED=true` | `--upload-dropbox` | off |
| `DROPBOX_ROOT_FOLDER=<path>` | `--dropbox-root <path>` | `/TD-Creative-Pipeline-POC` |
| `DROPBOX_CREATE_SHARED_LINKS=true` | `--dropbox-shared-links` | off |
| `DROPBOX_UPLOAD_PARALLELISM=<n>` | *(no flag — only via `.env`)* | `8` |
| `DROPBOX_ACCESS_TOKEN=<sl.u…>` | *(no flag — only via `.env`)* | — |

`DROPBOX_UPLOAD_PARALLELISM` controls how many uploads run concurrently. Default `8` turns the previous ~47 s serial upload (23 files × ~2 s/file) into ~6 s parallel. Set to `1` for strictly-serial behavior (useful when debugging rate-limiting).

**`adk web` / `adk run`** (live demo or interactive CLI):

```bash
# Add to .env once:
echo "DROPBOX_UPLOAD_ENABLED=true" >> .env

# Then runs through adk upload automatically:
uv run adk web .
```

**`scripts/run_pipeline.py`** (batch / CI), per-invocation flags or env:

```bash
# Default — no upload, identical to today.
.venv/bin/python scripts/run_pipeline.py

# Upload outputs/ snapshot to Dropbox (one-shot via flag).
.venv/bin/python scripts/run_pipeline.py --upload-dropbox

# Upload + create public shared links for report/gallery files.
.venv/bin/python scripts/run_pipeline.py --upload-dropbox --dropbox-shared-links

# Upload to a custom Dropbox folder (defaults to /TD-Creative-Pipeline-POC).
.venv/bin/python scripts/run_pipeline.py --upload-dropbox \
    --dropbox-root /TD-Creative-Pipeline-POC
```

### Where files land

The API-side path the uploader sends to Dropbox is always:

```
<DROPBOX_ROOT_FOLDER>/<campaign_id>/<run_timestamp>/<same tree as outputs/>
```

The actual on-disk location in your Dropbox depends on whether your app is App Folder–scoped or Full Dropbox–scoped.

**App Folder app** with `DROPBOX_ROOT_FOLDER=/runs` (the recommended setup; matches the env in [§ Quick Start](#environment-setup)):

```
/Apps/TD-Creative-Pipeline-POC/                ← app sandbox (auto-created on first upload)
└── runs/                                       ← from DROPBOX_ROOT_FOLDER
    └── summer_refresh_2025/                    ← from <campaign_id>
        └── 20260507T172946Z/                   ← from <run_timestamp>
            ├── report_20260507T172946Z.json
            ├── aquavita_sparkling/
            │   ├── 1x1/US_en.png  US_es.png  US_pt.png  9x16/...  16x9/...
            │   └── source/global_*.png   global_*.json
            └── sunguard_spf50/
                └── ...
```

If you set `DROPBOX_ROOT_FOLDER=/` instead, the `runs/` segment disappears and runs land directly under `/Apps/TD-Creative-Pipeline-POC/<campaign_id>/<run_timestamp>/`.

**Full Dropbox app** with `DROPBOX_ROOT_FOLDER=/TD-Creative-Pipeline-POC`:

```
/TD-Creative-Pipeline-POC/                     ← at the root of the user's Dropbox
└── summer_refresh_2025/
    └── 20260507T172946Z/
        └── ...
```

The uploader walks the local `outputs/` tree, includes only `.png` / `.json` / `.html` files, and skips hidden files (`.gitkeep`, `.DS_Store`, `.adk/...`, `.env`), `__pycache__`, and prior `dropbox_upload_*.json` manifests.

### Verifying an upload

After a run with `--upload-dropbox`, a local manifest is written to:

```
outputs/dropbox_upload_<run_timestamp>.json
```

It records `campaign_id`, `run_timestamp`, `dropbox_run_folder`, `uploaded_count`, every uploaded file's local + Dropbox path, any `failures`, and `shared_links` if requested. The runner also prints a one-line summary like:

```
DROPBOX_UPLOAD: enabled=true folder=/TD-Creative-Pipeline-POC/summer_refresh_2025/20260507T172946Z files=19 failures=0
DROPBOX_MANIFEST: outputs/dropbox_upload_20260507T172946Z.json
```

The token never appears in logs, the manifest, or any returned metadata.

### Notes

- Per-file upload failures are recorded in the manifest and don't abort the run — partial uploads are still useful.
- Files larger than Dropbox's 150 MB simple-upload limit are flagged as failures; chunked upload sessions are a TODO (POC files are well under).
- Uploads run in parallel (default 8 concurrent threads) so a 23-file run completes in ~6 s instead of ~47 s. Tune via `DROPBOX_UPLOAD_PARALLELISM`; set `=1` if Dropbox rate-limits you.
- Tests use a `FakeDropbox` class — the test suite never calls the real Dropbox API.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `DROPBOX_PREFLIGHT WARNING: invalid: AuthError(...invalid_access_token...)` | Token expired (`sl.u.A…` tokens last ~4 h) | Regenerate at App Console → Settings → "Generated access token". |
| `DROPBOX_PREFLIGHT WARNING: write check failed (...) BadInputError(...files.content.write...)` | Token authenticates but missing the write scope | App Console → Permissions tab → tick `files.content.write` → Submit → **regenerate the token** (scope changes invalidate old tokens). |
| `Dropbox upload skipped (DROPBOX_UPLOAD_ENABLED is not set)` | The opt-in flag isn't on | Add `DROPBOX_UPLOAD_ENABLED=true` to `.env` or pass `--upload-dropbox` to `scripts/run_pipeline.py`. |
| Files uploaded but you can't find them at `dropbox.com/home/TD-Creative-Pipeline-POC/` | App Folder–scoped Dropbox app puts everything under `/Apps/` | Look at <https://www.dropbox.com/home/Apps/TD-Creative-Pipeline-POC/>. App Folder is the safer default; see the matrix above. |
| Files end up in `/Apps/TD-Creative-Pipeline-POC/TD-Creative-Pipeline-POC/...` (doubled name) | App Folder app + `DROPBOX_ROOT_FOLDER=/TD-Creative-Pipeline-POC` | Set `DROPBOX_ROOT_FOLDER=/` for App Folder apps; the sandbox already prefixes the app name. |
| `Dropbox upload skipped: token invalid → ...` (in the agent's event log) | Same as preflight failures, surfaced from the agent path | Same fix; the agent's preflight covers `adk web` runs that don't have the script-level preflight. |

## Architecture

The pipeline is built on Google's **Agent Development Kit (ADK)**. ADK is the right tool here because the problem isn't really an LLM problem — it's an **orchestration** problem. Generating one creative is a single API call; generating localized creatives across products, markets, and aspect ratios while keeping brand and legal compliance enforced is a workflow with branching, fan-out, validation gates, and shared state. ADK provides first-class primitives for exactly that — `SequentialAgent`, `ParallelAgent`, session state, and lifecycle callbacks — so the structure of the pipeline lives in the agent topology rather than buried in custom Python plumbing.

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
  ├── ReportingAgent             aggregates state → outputs/report_*.json
  └── DropboxUploaderAgent       optional snapshot of outputs/ to Dropbox
                                 (no-op when DROPBOX_UPLOAD_ENABLED unset;
                                 never raises — failures land in state instead)
```

**Two ideas are doing most of the work.**

**1. Setup → fan-out → fan-in.** Brand guidelines and the campaign brief are standing inputs that load *once* and then feed every downstream branch. Once they're in session state, the `ParallelAgent` fans out one sub-pipeline per product — those run concurrently because nothing about generating product A's creatives depends on product B's. `ReportingAgent` fans the results back in at the end. This is what lets the system scale toward "hundreds of campaigns per month" without rewriting the core: more products is a wider fan-out, not new code.

**2. Each sub-pipeline is itself a `SequentialAgent` of single-responsibility agents.** The per-product flow reads top-to-bottom like a spec — `cache check → generate (if needed) → compose → brand check → legal check → QC check`. Every agent does one thing, reads from session state, and writes back to it. The `AssetManagerAgent` is the only thing that knows where assets live on disk; the `ImageGeneratorAgent` is the only thing that knows about Imagen / gpt-image-1; the `CreativeComposerAgent` is the only thing that knows about Pillow. Replacing the image provider is a one-agent change, not a refactor.

### Architectural choices worth calling out

- **Mixed LLM and deterministic agents.** `CreativeComposerAgent`, `BrandCheckerAgent`, and `QCCheckerAgent` don't call an LLM — they're function-based ADK agents wrapping Pillow / OpenCV / WCAG math. ADK doesn't *require* every node to be an LLM, and using a model for deterministic image work would be slower, more expensive, and less reliable. The LLM shows up where it earns its keep (image generation), and rule-based code shows up where it's better.
- **Compliance is structural, not advisory.** `LegalCheckerAgent` runs in two places: as a `before_model_callback` *before* image generation is ever called (so a prohibited word in the brief halts the run before any spend), and again *after* composition (to verify the per-market disclaimer was actually rendered). Brand checks (palette distance, logo placement) and QC contrast checks run post-composition, on the rendered artifact. The pipeline can't produce an output that bypassed the checks because the checks are nodes in the graph, not a linter run on the side.
- **Session state as the integration contract.** Every agent reads and writes to a single shared state object — the brief, the brand guidelines, per-product outputs, check results, latencies. That's what makes `ReportingAgent` trivial: it just walks state and serializes. It's also what isolates the parallel branches — each writes to its own `state["product:{pid}"]` slot, no cross-branch contention.
- **Provider-agnostic by construction.** The agent LLM is wired through **LiteLLM**, so the same agent code drives Google, OpenAI, or Anthropic without per-provider branches. The image-gen backend is decoupled from the agent LLM (`PROVIDER` and `IMAGE_PROVIDER` are independent env vars), so you can run a Gemini-driven agent that calls `gpt-image-1` for hero generation, or vice versa. This decoupling is the contract that prevents vendor lock-in for a real client.
- **Idempotency by design.** Generated heroes are written back into the input asset directory, so the second run is a cache hit. "Reuse when available" is honored by making generated assets indistinguishable from supplied ones on the next run.

### What this architecture buys

- **Scale** — adding a product is one YAML entry; adding a market or locale is a string in a map; the agent code doesn't change.
- **Substitutability** — any single agent can be swapped (image model, storage backend, compliance ruleset) without touching the others, because session state is the only contract between them.
- **Visibility** — the ADK trace makes the run inspectable in `adk web`: parallel branches stream live, every state delta is visible, debugging is reading the trace rather than grepping logs.
- **Safety** — compliance gates are graph nodes, not optional middleware, so there's no "we forgot to run the legal check" failure mode.

## Key Design Decisions

1. **Brand guidelines as a standing artifact, not brief fields.** Guidelines change yearly; briefs change per campaign. Splitting them lets the brand stay the source of truth while briefs stay concise.
2. **Parallel-per-product fan-out.** Each product runs its own sequential sub-pipeline inside a `ParallelAgent`, so two products generate concurrently. Per-product state lives at flat keys `state["product:{pid}"]` to avoid races on a shared nested dict.
3. **Asset caching for idempotency and cost control.** `inputs/assets/{pid}/hero.png` is the cache; both the asset manager and the image generator's activation gate respect it. Re-running with cached heroes skips the LLM and the image API entirely.
4. **Compliance separated from generation.** Legal pre-check fires as a `before_model_callback` on the image generator's LLM call so prohibited words can never reach the image-gen API. Brand check is deterministic palette + template-match (not an LLM judgement) — fast, predictable, auditable.
5. **Storage abstracted behind an interface, plus an optional out-of-band uploader.** [storage_adapter.py](creative_pipeline/tools/storage_adapter.py) defines the per-write storage boundary used by every agent that persists state — `LocalStorageAdapter` is the only adapter the POC ships, and S3/GCS/Azure adapters would slot in alongside it. The Dropbox upload is intentionally **not** wired through that interface — it's a *post-run snapshot* step that walks the finished `outputs/` tree and mirrors the relevant artifacts to a team-shared Dropbox folder. Keeping it out of the per-write hot path means a Dropbox outage can't fail an in-flight run, the SDK stays an optional `[upload]` extra, and the agents keep treating the local filesystem as the single source of truth. See § *Optional Dropbox Upload*.
6. **LiteLLM provider swap + parallel image-gen track.** The agent LLM and the image-gen backend are decoupled and individually swappable via `PROVIDER` / `IMAGE_PROVIDER` — see the matrix above.

## Assumptions and Limitations

- **Local storage only.** Cloud adapters are a one-class swap behind the existing interface but not implemented.
- **Image-gen backends:** Imagen 3 (`google`) and gpt-image-1 (`openai`). Anthropic has no native image API, so it requires `IMAGE_PROVIDER` to be set explicitly.
- **Text placement is rule-based, not saliency- or vision-aware.** The composer picks per-aspect headline boxes from the layout template, records the resulting headline box and rendered text color in the report, and `QCCheckerAgent` then measures actual final-render contrast against the WCAG-style threshold. A production version could swap the rule-based placement for saliency detection or LLM vision to pick a clean text region — but the QC gate would continue to verify the result regardless of how the placement was chosen.
- **Brand check is heuristic.** Palette distance (RGB Euclidean) + cv2 template matching pass-warn-fail thresholds are tuned for the demo brand; not human-grade.
- **Localization is data-driven**, not LLM-translated. The brief carries a single `campaign_message` (primary string) plus an optional `campaign_message_localized` map. `brief.language` is a directive — when it matches a key in `campaign_message_localized`, that entry wins for single-language runs. With `localized_copy: true`, `brand.market_locales` picks each market's locale with `brief.language` as fallback. The image-gen prompt receives a language-aware audience clause so the photography picks up cultural cues alongside the copy. See § *Language and localization*.
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

## Advanced / Debugging

The recommended demo path is `scripts/run_pipeline.py` — see [§ Quick Start](#quick-start). The two ADK invocations below are for **debugging / development only**.

### ADK Web

```bash
uv run adk web .                     # opens http://127.0.0.1:8000
```

Useful when you want to:
- inspect the Google ADK agent orchestration **visually** (each agent step streams in real time in the browser),
- watch the per-product `ParallelAgent` branches fan out and rejoin,
- step through state-delta updates between agents to debug what each step writes,
- live-walk a stakeholder through the multi-agent graph without running headless from a terminal.

In the dropdown, pick **`creative_pipeline`** and send any message — the message text is ignored, the pipeline kicks off regardless. The Dropbox upload step runs as the final agent in the graph, so when `DROPBOX_UPLOAD_ENABLED=true` is set in `.env`, ADK Web also uploads to Dropbox at the end.

### ADK interactive terminal

```bash
uv run adk run creative_pipeline
```

Same orchestration as ADK Web, terminal-only. Useful for quick interactive runs when the visual UI isn't necessary but you still want the agent-by-agent streaming output rather than the consolidated `scripts/run_pipeline.py` log.

### When to use which

| You want to… | Use |
|---|---|
| Demo the pipeline end-to-end | **`scripts/run_pipeline.py`** (recommended) |
| Force fresh hero generation | reset commands + `scripts/run_pipeline.py` (see [§ Fresh run](#fresh-run--force-new-hero-images)) |
| Visually debug agent steps | `adk web .` |
| Quick interactive terminal run | `adk run creative_pipeline` |
| Run from CI / unattended | `scripts/run_pipeline.py` (no UI dependency) |

## Demo Video

Recorded against `adk web`; covers cold-start image generation, cache reuse on re-run, and a provider-swap segment. *(Link to be embedded after recording.)*

## Demo Talking Points

- **YAML-driven controls.** Every campaign-level choice (language, localization toggles, layout template, creative quality, palette guidance, force-generate / regenerate-cache flags, halt-on-QC-failure) lives in the brief; every brand-rule choice (colors, typography, logo treatment, layout templates, overlay style, accent style, contrast thresholds) lives in the brand guidelines. Switching campaigns or brands does not require touching Python.
- **AI generation plus deterministic composition.** Hero photography is generated by gpt-image-1 (or Imagen 3) under explicit force/regenerate flags and saved with sidecar provenance; everything else — smart crop, gradient overlay, headline placement, brand accent, logo badge, disclaimer placement — runs through deterministic Pillow code so the same brief produces the same final composition every time.
- **Audit-quality reporting.** Each run emits a single timestamped `report_*.json` whose per-output rows carry every field a reviewer would ask for: source asset path, image provider/model, latency, used-cache flag, headline/disclaimer/logo bounding boxes, overlay style and opacity, accent color, brand palette and element scores with reasons, and the full QC rule trace.
- **WCAG-style final-render QC.** `QCCheckerAgent` opens the rendered PNG, samples the actual background under the headline box (filtering out text pixels), and computes a WCAG 2.x-style contrast ratio against the rendered text color. This is a final-render readability check, not a full accessibility certification — but it catches the failure modes (busy backgrounds, color drift) that visual review would otherwise have to find by eye.
- **The SunGuard AA-large nuance.** Navy headline on tan beach-towel background scored 3.24:1. Rather than silently passing or hard-failing, the system surfaced `wcag_level: "AA-large"` so a reviewer can see exactly what they are accepting. A brand that wants stricter copy bumps `min_contrast_ratio` or sets `halt_on_qc_failure: true` — no code change.
- **Modular future QC rules.** `tools/qc_rules.py:build_rules(brand)` builds the active rule list from brand flags. Adding a new rule (minimum font size, focal-area collision, brand-color saturation in headline region, etc.) is a new `QCRule` subclass plus a flag in `RequiredBrandChecks` — `QCCheckerAgent` already iterates whatever `build_rules` returns.
- **Optional Dropbox snapshot — local outputs always win.** `scripts/run_pipeline.py --upload-dropbox` mirrors the run's `outputs/` to `/TD-Creative-Pipeline-POC/<campaign_id>/<run_timestamp>/` after the pipeline finishes. The Dropbox SDK is an opt-in `[upload]` extra so default installs stay slim; the access token is read from `.env`, never logged, never echoed in metadata, and the upload runs **after** the pipeline so a Dropbox outage can't fail a local run. A `dropbox_upload_<ts>.json` manifest lands next to `report_<ts>.json` listing every uploaded file and any per-file failures, plus optional shared links when `--dropbox-shared-links` is passed. See § *Optional Dropbox Upload*.

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
