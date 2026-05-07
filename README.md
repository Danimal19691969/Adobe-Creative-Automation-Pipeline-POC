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

### Run modes

| Mode | Command | When to use |
|---|---|---|
| **Live demo** | `uv run adk web .` | Live walkthrough — chat with the agent, watch each step stream in the ADK UI. |
| **CLI / interactive** | `uv run adk run creative_pipeline` | Same orchestration, terminal output. |
| **Batch / CI / one-shot** | `.venv/bin/python scripts/run_pipeline.py` | Headless, no UI. Prints `REPORT: outputs/report_*.json` and exits. |
| **Batch + Dropbox snapshot** | `.venv/bin/python scripts/run_pipeline.py --upload-dropbox` | Above + optional post-run upload of `outputs/` to a team-shared Dropbox folder. See § *Optional Dropbox Upload*. |

The `scripts/run_pipeline.py` driver also supports `--dropbox-shared-links` (best-effort public links for `report_*.json` and gallery files) and `--dropbox-root <path>` (override the default `/TD-Creative-Pipeline-POC` destination). Each flag has an env-var equivalent.

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
language: en                  # primary language tag — directive (see § Language)
localized_copy: false         # toggle: per-market fan-out across locales
localized_legal_copy: false   # toggle: per-market legal text
markets: ["MX", "BR", "CO"]
target_audience: "Health-conscious adults 25-40"

# Single-language headline (used when localized_copy=false). Localized
# entries are consulted automatically when ``language`` matches a key.
campaign_message: "Refresh your summer, naturally."
campaign_message_localized:
  en: "Refresh your summer, naturally."
  es: "Refresca tu verano, naturalmente."
  pt: "Renove seu verão, naturalmente."

# Optional campaign-specific disclaimer override; falls back to brand
# legal when unset. See § Language and localization.
disclaimer_text: null
disclaimer_text_localized: {}

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

The brief has two orthogonal switches that together drive all language behavior:

| Flag | Type | Effect |
|---|---|---|
| `language` | locale code (`en`, `es`, `pt`, `fr`, …) | Primary language directive. When ``localized_copy=false``, picks the entry from ``campaign_message_localized`` whose key matches; falls back to ``campaign_message`` if no match. Also drives the locale tag in output filenames (`MX_es.png`) and adds a language-aware audience clause to the image-gen prompt. |
| `localized_copy` | bool | When `true`, the composer fans out per market using `brand.market_locales` to pick each market's locale and pull the headline from `campaign_message_localized[locale]`. When `false`, every market renders the single primary language. |

Three common patterns:

```yaml
# 1. Single-language English campaign — most briefs land here.
language: en
localized_copy: false
campaign_message: "Refresh your summer, naturally."
# campaign_message_localized.en wins automatically; en/es/pt entries
# below are kept as a translation library that becomes active if you
# flip language or localized_copy later.

# 2. Single-language Spanish campaign — flip ONE field.
language: es
localized_copy: false
# Composer auto-pulls campaign_message_localized["es"] for every market,
# so you don't rewrite campaign_message. The image-gen prompt also
# gains a "Spanish-speaking audience (LATAM / Iberian context)" cue.

# 3. Multi-locale fan-out — Spanish in MX/CO, Portuguese in BR.
language: en
localized_copy: true
campaign_message_localized:
  en: "Refresh your summer, naturally."
  es: "Refresca tu verano, naturalmente."
  pt: "Renove seu verão, naturalmente."
# Output filenames: MX_es.png, BR_pt.png, CO_es.png.
```

**Disclaimer text** follows the same brief-first principle. Set
`disclaimer_text` (or `disclaimer_text_localized` for per-market variants)
on the brief to ship campaign-specific legal copy — "Promotion ends August
31, 2025." or similar. When both are unset, the composer falls back to
`brand.legal.default_disclaimer` / `brand.legal.required_disclaimers`,
keeping compliance boilerplate as the safety net. Resolution order is
brief-localized → brief-default → brand-localized → brand-default. Both
fields are validated by the schema in [creative_pipeline/schemas.py](creative_pipeline/schemas.py).

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

## Optional Dropbox Upload

The pipeline writes all artifacts to local `outputs/` first — that remains the source of truth. The Dropbox upload step is an **opt-in** post-run snapshot for sharing run artifacts with reviewers who don't have filesystem access to the runner host. It runs as the final agent in the graph (`DropboxUploaderAgent`, after `ReportingAgent`), so **`adk web`, `adk run`, and `scripts/run_pipeline.py` all support upload** through the same code path. A missing token / SDK / config issue never affects the local run — the agent records the failure in state and yields a "skipped" event instead of raising.

### Setup

1. **Create a Dropbox app** at <https://www.dropbox.com/developers/apps/create>. The "Permission type" you pick at creation time matters — see the next subsection.
2. **Generate an access token** (App Console → your app → Settings → "Generated access token" → Generate). Tokens starting with `sl.u.A…` are short-lived (~4 hours) and work fine for POC demos. For unattended runs, use the OAuth refresh-token flow.
3. **Add it to `.env`**:

   ```
   DROPBOX_ACCESS_TOKEN=sl.u.xxxx...
   ```

   `.env` is already in `.gitignore` — don't commit it.

4. **Install the SDK** (optional `[upload]` extra; local runs don't need it):

   ```bash
   pip install -e .[upload]
   # or
   uv sync --extra upload
   ```

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

The API-side path is always:

```
<DROPBOX_ROOT_FOLDER>/<campaign_id>/<run_timestamp>/<same tree as outputs/>
```

The Dropbox-side path depends on whether you have an App Folder or Full Dropbox app.

**App Folder app** (with `DROPBOX_ROOT_FOLDER=/`):

```
/Apps/TD-Creative-Pipeline-POC/        ← app sandbox
└── summer_refresh_2025/                 ← from <campaign_id>
    └── 20260507T172946Z/                ← from <run_timestamp>
        ├── report_20260507T172946Z.json
        ├── aquavita_sparkling/
        │   ├── 1x1/MX_es.png  9x16/MX_es.png  16x9/MX_es.png  ...
        │   └── source/global_*.png   global_*.json
        └── sunguard_spf50/
            └── ...
```

**Full Dropbox app** (with `DROPBOX_ROOT_FOLDER=/TD-Creative-Pipeline-POC`):

```
/TD-Creative-Pipeline-POC/               ← at the root of the user's Dropbox
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
- Tests use a `FakeDropbox` class — the test suite never calls the real Dropbox API.

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
  ├── ReportingAgent             aggregates state → outputs/report_*.json
  └── DropboxUploaderAgent       optional snapshot of outputs/ to Dropbox
                                 (no-op when DROPBOX_UPLOAD_ENABLED unset;
                                 never raises — failures land in state instead)
```

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