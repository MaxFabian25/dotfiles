#!/usr/bin/env bash
# imagegen via the codex CLI bridge (ChatGPT-subscription-backed).
#
# Claude Code has no built-in image tool. This shells out to the user's
# installed `codex` CLI, which runs Codex's built-in image_gen using the user's
# ChatGPT subscription (no OPENAI_API_KEY, no per-image API billing).
#
# Usage:
#   imagegen_codex.sh --out PATH --prompt "TEXT" [options]
#
# Required:
#   --out PATH            Absolute or relative path to write the final image to.
#   --prompt TEXT         What to generate/edit (or use --prompt-file).
#   --prompt-file FILE    Read the prompt from a file instead of --prompt.
#
# Optional:
#   --edit FILE           Edit this existing image (repeatable). Implies edit intent.
#   --reference FILE      Use as a style/composition reference only (repeatable).
#   --size SPEC           Hint, e.g. 1024x1024, 1536x1024, auto.
#   --transparent         Ask for a transparent background.
#   --extra TEXT          Extra instruction appended verbatim.
#   --sandbox MODE        codex sandbox: workspace-write (default) | danger-full-access.
#   --model MODEL         Pass a specific model to `codex exec -m`.
#   --timeout SECONDS     Kill codex after N seconds (default 600).
#   --dry-run             Print the codex invocation without running it.
set -euo pipefail

err() { printf 'Error: %s\n' "$1" >&2; exit 1; }

command -v codex >/dev/null 2>&1 || err "codex CLI not found on PATH. Install/login to codex, or use the image_gen.py API fallback."

OUT="" ; PROMPT="" ; PROMPT_FILE="" ; SIZE="" ; EXTRA="" ; MODEL=""
SANDBOX="workspace-write" ; TIMEOUT="600" ; TRANSPARENT="false" ; DRY="false"
EDITS=() ; REFS=() ; N_EDITS=0 ; N_REFS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2;;
    --prompt) PROMPT="$2"; shift 2;;
    --prompt-file) PROMPT_FILE="$2"; shift 2;;
    --edit) EDITS+=("$2"); N_EDITS=$((N_EDITS+1)); shift 2;;
    --reference) REFS+=("$2"); N_REFS=$((N_REFS+1)); shift 2;;
    --size) SIZE="$2"; shift 2;;
    --transparent) TRANSPARENT="true"; shift;;
    --extra) EXTRA="$2"; shift 2;;
    --sandbox) SANDBOX="$2"; shift 2;;
    --model) MODEL="$2"; shift 2;;
    --timeout) TIMEOUT="$2"; shift 2;;
    --dry-run) DRY="true"; shift;;
    -h|--help) sed -n '2,40p' "$0"; exit 0;;
    *) err "unknown argument: $1";;
  esac
done

[ -n "$OUT" ] || err "--out is required."
if [ -z "$PROMPT" ] && [ -n "$PROMPT_FILE" ]; then
  [ -f "$PROMPT_FILE" ] || err "prompt file not found: $PROMPT_FILE"
  PROMPT="$(cat "$PROMPT_FILE")"
fi
[ -n "$PROMPT" ] || err "--prompt or --prompt-file is required."

# Resolve OUT to an absolute path and ensure its directory exists.
OUTDIR="$(cd "$(dirname "$OUT")" 2>/dev/null && pwd || true)"
if [ -z "$OUTDIR" ]; then
  mkdir -p "$(dirname "$OUT")"
  OUTDIR="$(cd "$(dirname "$OUT")" && pwd)"
fi
OUT_ABS="$OUTDIR/$(basename "$OUT")"

# Validate input image files and collect codex -i flags.
IMG_FLAGS=()
for f in ${EDITS[@]+"${EDITS[@]}"} ${REFS[@]+"${REFS[@]}"}; do
  [ -f "$f" ] || err "image file not found: $f"
  IMG_FLAGS+=(-i "$f")
done

# Build the instruction for the headless codex agent.
INSTRUCTION="You are running headless to produce one image asset. Use your imagegen skill (the built-in image_gen tool) to create it."
if [ "$N_EDITS" -gt 0 ]; then
  INSTRUCTION="$INSTRUCTION This is an EDIT of the attached image(s); preserve everything not mentioned and only change what the task asks."
fi
if [ "$N_REFS" -gt 0 ]; then
  INSTRUCTION="$INSTRUCTION Treat the attached image(s) as style/composition references only, not as the literal output."
fi
INSTRUCTION="$INSTRUCTION
Task: $PROMPT"
[ -n "$SIZE" ] && INSTRUCTION="$INSTRUCTION
Target size: $SIZE."
[ "$TRANSPARENT" = "true" ] && INSTRUCTION="$INSTRUCTION
Output must have a transparent background (PNG with alpha)."
[ -n "$EXTRA" ] && INSTRUCTION="$INSTRUCTION
$EXTRA"
INSTRUCTION="$INSTRUCTION
Save the final image to this exact path: $OUT_ABS
Create parent directories if needed and overwrite the file if it already exists.
Do not ask any clarifying questions; choose reasonable defaults and proceed.
When finished, output exactly one line: SAVED $OUT_ABS"

LAST_MSG="$(mktemp -t imagegen_codex_last.XXXXXX)"
CMD=(codex exec --cd "$OUTDIR" --skip-git-repo-check -s "$SANDBOX" -o "$LAST_MSG")
[ -n "$MODEL" ] && CMD+=(-m "$MODEL")
CMD+=(${IMG_FLAGS[@]+"${IMG_FLAGS[@]}"} "$INSTRUCTION")

if [ "$DRY" = "true" ]; then
  printf 'Would run (cwd=%s):\n' "$OUTDIR"
  printf '%q ' "${CMD[@]}"; printf '\n\n--- instruction ---\n%s\n' "$INSTRUCTION"
  exit 0
fi

echo "Generating via codex CLI (ChatGPT subscription). This can take a couple of minutes..." >&2

if command -v timeout >/dev/null 2>&1; then
  timeout "$TIMEOUT" "${CMD[@]}" || true
else
  "${CMD[@]}" || true
fi

if [ -f "$OUT_ABS" ]; then
  echo "SAVED $OUT_ABS"
  exit 0
fi

echo "Last codex message:" >&2
cat "$LAST_MSG" >&2 || true
err "codex finished but the expected output file was not created: $OUT_ABS
If codex could not reach the network in workspace-write mode, retry with --sandbox danger-full-access, or use the image_gen.py API fallback."
