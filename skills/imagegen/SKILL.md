---
name: imagegen
description: Generate or edit raster images when the task benefits from AI-created bitmap visuals such as photos, illustrations, textures, sprites, mockups, or transparent-background cutouts. Use to create a brand-new image, transform an existing image, or derive variants from references, when the output should be a bitmap asset rather than repo-native code or vector. Do not use when the task is better handled by editing existing SVG/vector/code-native assets, extending an established icon or logo system, or building the visual directly in HTML/CSS/canvas.
---

# Image Generation Skill

Generate or edit images for the current project (website assets, game assets, UI mockups, product mockups, wireframes, logo exploration, photorealistic images, infographics).

Claude Code has no built-in image tool. This skill generates images two ways:

- **Default — codex CLI bridge (ChatGPT subscription):** shells out to the user's installed `codex` CLI, which runs Codex's built-in image generation on the user's ChatGPT subscription. No `OPENAI_API_KEY`, no per-image API billing. Wrapper: `scripts/imagegen_codex.sh`.
- **Fallback — OpenAI Image API CLI:** the bundled `scripts/image_gen.py` (gpt-image-2). Use when the codex CLI is unavailable, or when the user explicitly wants API/model controls (masks, `generate-batch`, `input_fidelity`, `gpt-image-1.5` true transparency). Requires `OPENAI_API_KEY` and is **billed per image, separately from the ChatGPT subscription**.

Both paths share the same prompt-shaping guidance in `references/prompting.md` and `references/sample-prompts.md`.

## Rules

- Use the **codex bridge** by default for normal generation and editing.
- Do not silently switch to the API-key fallback. Switch only when the codex CLI is missing, the user asks for explicit CLI/API/model controls, or a confirmed `gpt-image-1.5` true-transparency request requires it — and tell the user it costs per image via `OPENAI_API_KEY`.
- For transparent images, prefer the bridge with a flat chroma-key background, then remove the key locally with `scripts/remove_chroma_key.py` (see below). Use the API `gpt-image-1.5 --background transparent` path only after the user confirms, because `gpt-image-2` does not support `background=transparent`.
- Many distinct assets means one bridge call per asset (or one `generate-batch` job in the fallback). `n`/variants is for variations of a single prompt, not for distinct assets.
- Never modify `scripts/image_gen.py`. If something is missing, ask first.

## Quick start (codex bridge)

```bash
BRIDGE="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/imagegen/scripts/imagegen_codex.sh"

# Generate
"$BRIDGE" --out output/imagegen/hero.png \
  --prompt "minimal hero photo of a ceramic coffee mug, soft studio light, generous negative space, no text, no watermark"

# Edit an existing file (preserve everything not mentioned)
"$BRIDGE" --out output/imagegen/product-sunset.png \
  --edit assets/product.png \
  --prompt "replace only the background with a warm sunset gradient; keep the product and its edges unchanged"

# Use reference images for style only
"$BRIDGE" --out output/imagegen/cover.png \
  --reference refs/moodboard.png \
  --prompt "album cover in the reference's color and texture, new subject: a lone lighthouse"

# Preview the exact codex invocation without running it
"$BRIDGE" --dry-run --out /tmp/x.png --prompt "test"
```

The bridge writes the image to `--out`, then **view it with the Read tool** to validate it. It runs `codex exec` headless with `--sandbox workspace-write`; if codex cannot reach the network in that mode, retry with `--sandbox danger-full-access` or use the API fallback. Generation can take a couple of minutes.

## When to use / not use

Use for: a new image (concept art, product shot, cover, hero); a new image guided by reference images; editing an existing image (inpainting, lighting/weather, background replacement, object removal, compositing, transparent cutout); producing several assets or variants.

Do not use for: extending or matching an existing SVG/vector icon set or logo system in the repo; simple shapes/diagrams/wireframes/icons better done in SVG/HTML/CSS/canvas; a small local edit when the source already exists in an editable native format; any task where the user clearly wants deterministic code-native output.

## Decide intent and strategy

1. **Intent — generate vs edit.** If the user wants to modify an existing image while preserving parts of it, it's an **edit** (`--edit`). If images are only style/composition/mood references, or there are no images, it's a **generate** (optionally `--reference`).
2. **Strategy — one asset vs many.** One asset → one bridge call. Many distinct assets → one bridge call each. Variants of one prompt → the API fallback's `--n` (1–10), or repeated bridge calls.

Assume a new image unless the user clearly asks to change an existing one. For edits, list invariants explicitly (`change only X; keep Y unchanged`) and save non-destructively (a sibling versioned filename like `hero-v2.png`) unless replacement was requested.

## Workflow

1. Pick the path: codex bridge by default; API fallback only when justified (above).
2. Decide intent (generate/edit) and strategy (single/many/variants).
3. Decide whether output is preview-only or project-bound.
4. Collect inputs: prompt(s), exact verbatim text, constraints/avoid list, and any input images with their role (reference vs edit target).
5. Shape the prompt by specificity: if already detailed, normalize it; if generic, add tasteful augmentation only when it materially helps. Use the schema below.
6. Run the bridge (or fallback). Save project-bound assets into the workspace; keep preview-only output under `output/imagegen/`.
7. View each output with the Read tool and validate: subject, style, composition, text accuracy, invariants/avoid items.
8. Iterate with one targeted change, then re-check.
9. Report the final saved path(s), the final prompt(s), and which path (bridge or API fallback) was used.

## Save-path policy

- If the user names a destination, write there (`--out`).
- If the asset is project-bound, write it into the workspace and update any consuming code/references.
- If it's preview-only, keep it under `output/imagegen/`.
- Do not overwrite an existing asset unless the user asked for replacement; otherwise use a versioned sibling name.

## Transparent images

Default path (no API key): generate a removable chroma-key source via the bridge, then convert the key color to alpha locally.

```bash
BRIDGE="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/imagegen/scripts/imagegen_codex.sh"
"$BRIDGE" --out tmp/imagegen/source.png --prompt "
Create the requested subject on a perfectly flat solid #00ff00 chroma-key background for background removal.
The background must be one uniform color with no shadows, gradients, texture, reflections, floor plane, or lighting variation.
Keep the subject fully separated with crisp edges and generous padding. Do not use #00ff00 in the subject.
No cast shadow, no contact shadow, no reflection, no watermark, and no text unless requested."

python "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/imagegen/scripts/remove_chroma_key.py" \
  --input tmp/imagegen/source.png --out output/imagegen/final.png \
  --auto-key border --soft-matte --transparent-threshold 12 --opaque-threshold 220 --despill
```

- Choose a key color unlikely to appear in the subject: default `#00ff00`; use `#ff00ff` for green subjects; avoid `#0000ff` for blue subjects.
- Validate the result has an alpha channel, transparent corners, plausible coverage, and no key-color fringe. If a thin fringe remains, retry with `--edge-contract 1`; use `--edge-feather 0.25` only for visibly stair-stepped edges on non-shiny subjects.
- Ask before using the API fallback's true transparency (`gpt-image-1.5 --background transparent --output-format png`, needs `OPENAI_API_KEY`) for complex subjects (hair, fur, feathers, smoke, glass, liquids, translucent/reflective materials, soft shadows) or when local removal fails validation.
- `remove_chroma_key.py` and the API fallback need Pillow: `uv pip install pillow` (or `pip install pillow`).

## Prompt augmentation

Reformat user prompts into a structured, production-oriented spec without blindly adding detail. Use only the lines that help.

- If the prompt is already specific, preserve that specificity and only normalize/structure it.
- If the prompt is generic, you may add composition/framing hints, polish/intended-use hints, practical layout guidance, and reasonable scene concreteness.
- Do not invent extra characters/objects, brand names, slogans, palettes, or narrative beats that are not implied.

### Shared prompt schema

```text
Use case: <taxonomy slug>
Asset type: <where the asset will be used>
Primary request: <user's main prompt>
Input images: <Image 1: role; Image 2: role> (optional)
Scene/backdrop: <environment>
Subject: <main subject>
Style/medium: <photo/illustration/3D/etc>
Composition/framing: <wide/close/top-down; placement>
Lighting/mood: <lighting + mood>
Color palette: <palette notes>
Materials/textures: <surface details>
Text (verbatim): "<exact text>"
Constraints: <must keep/must avoid>
Avoid: <negative constraints>
```

For edits, list invariants every iteration. Quote exact text verbatim and specify typography + placement; for tricky words, spell them letter-by-letter. If a critical detail blocks success, ask; otherwise proceed.

### Use-case taxonomy (slugs)

Generate: `photorealistic-natural`, `product-mockup`, `ui-mockup`, `infographic-diagram`, `scientific-educational`, `ads-marketing`, `productivity-visual`, `logo-brand`, `illustration-story`, `stylized-concept`, `historical-scene`.

Edit: `text-localization`, `identity-preserve`, `precise-object-edit`, `lighting-weather`, `background-extraction`, `style-transfer`, `compositing`, `sketch-to-render`.

## API fallback (OpenAI Image API)

Only when the bridge is unavailable or explicit CLI/API/model control is needed. Billed per image via `OPENAI_API_KEY`.

```bash
IMAGE_GEN="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/imagegen/scripts/image_gen.py"
uv pip install openai pillow          # once, into the active environment
export OPENAI_API_KEY=...             # never paste the key in chat; set it locally

python "$IMAGE_GEN" generate --prompt "..." --out output/imagegen/asset.png
python "$IMAGE_GEN" edit --image in.png --prompt "..." --out output/imagegen/edited.png
python "$IMAGE_GEN" generate-batch --input jobs.jsonl --out-dir output/imagegen/
```

- Defaults to `gpt-image-2`; `quality` is `low|medium|high|auto`; sizes follow gpt-image-2 constraints (max edge ≤ 3840px, edges multiples of 16, ratio ≤ 3:1, 655,360–8,294,400 px). `--dry-run` prints the request.
- `gpt-image-2` always uses high input fidelity (do not set `input_fidelity`) and does not support `background=transparent` — that needs `gpt-image-1.5`.
- If `OPENAI_API_KEY` is missing: create a key at https://platform.openai.com/api-keys and set it as an env var. Never ask the user to paste the full key in chat.

## Reference map

- `references/prompting.md` — shared prompting principles for both paths.
- `references/sample-prompts.md` — copy/paste prompt recipes by asset type (website, game, wireframe, logo, etc.).
- `references/cli.md` — the API fallback CLI (`scripts/image_gen.py`) usage and examples.
- `references/image-api.md` — API/CLI parameter reference for the fallback.
- `scripts/imagegen_codex.sh` — codex CLI bridge (default path; ChatGPT subscription).
- `scripts/image_gen.py` — API fallback implementation (do not modify).
- `scripts/remove_chroma_key.py` — local chroma-key → alpha helper for transparent images.
