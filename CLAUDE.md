# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

PID-DETECTOR detects and tracks **piping lines** on **P&ID** drawings (ISA standard):
line detection, reading ISA markings (OCR / vector text), handling **line-break
symbols** (a logically continuous line that is drawn with a visual gap), and
consistent per-line colorization. The codebase and most comments/docstrings are
written in **French** — match that language when editing existing modules.

## Commands

```bash
pip install -r requirements.txt          # OpenCV, numpy, networkx, easyocr, pymupdf, openpyxl

# --- Raster pipeline (PNG/JPEG) : pid_detector.py ---
python pid_detector.py --input plan.png --outdir out --debug        # clean/synthetic plan
python pid_detector.py --input plan.jpeg --preset real_plan --no-ocr # real scan, calibrated preset
python pid_detector.py --input plan.jpeg --preset real_plan --list-lines        # list detected lines and exit
python pid_detector.py --input plan.jpeg --preset real_plan --highlight 2       # highlight line by id
python pid_detector.py --input plan.jpeg --preset real_plan --highlight-label AZOTE  # highlight by OCR label

# --- Vector PDF pipeline : highlight_lines.py (uses detect_line_limits.py) ---
python detect_line_limits.py --pdf plan.pdf --outdir out            # detect "line limit" symbols, blue dot on pipe
python highlight_lines.py --pdf plan.pdf --make-template --excel couleurs.xlsx  # 1) generate Excel template
python highlight_lines.py --pdf plan.pdf --excel couleurs.xlsx --outdir out --png  # 2) colorize from filled Excel
```

There is **no test suite, linter config, build system, or CI** in this repo.
Validation is done by running the scripts on sample plans and inspecting the
output images/PDF/JSON in the output directory.

## Architecture: two independent pipelines

The repo contains **two separate detection systems** that do not share code
(except `highlight_lines.py` importing from `detect_line_limits.py`). Pick based
on the input format.

### 1. Raster pipeline — `pid_detector.py` (images: PNG/JPEG)

Self-contained single file. Pipeline (see the `process()` orchestrator):

```
load_image → detect_edges (Canny) → detect_lines (HoughLinesP) → merge_segments
  → detect_break_symbols + detect_fittings → reconnect_lines → build_graph
  → run_ocr → associate_text_to_lines → colorize_lines → export_results
```

**Core idea (the part that requires reading multiple functions to grasp):**
every merged segment becomes an **edge** of a `networkx` graph; nearby segment
endpoints are snapped onto shared **nodes** (intersections). **A logical line =
one connected component.** This is why line identity survives visual gaps:

- **Break symbols** (ISA open triangle / inverted "Y", `detect_break_symbols`)
  and **through-components** (valves, insulation clouds, instrument bubbles,
  `detect_fittings`) cut the line *visually* but not *logically*. They are fed
  to `reconnect_lines` as **junction points**, which adds virtual "bridge" edges
  between genuinely collinear segments on either side. Because the bridge keeps
  both sides in the same connected component, they share the same line id and
  color. `reconnect_lines` only bridges segments that are collinear / aligned /
  close, so an isolated text blob does not create a false bridge.

Everything is tuned through the `Config` dataclass at the top of the file — all
thresholds live there. The `real_plan` **preset** (in `PRESETS`) loosens
gap/snap tolerances and sets `ocr_upscale=3.0`; the OCR upscale is essential
for low-res scans (tiny marking text is enlarged before OCR, then coordinates
are reprojected to native scale). CLI flags override preset values
(`build_config_from_args`). OCR degrades gracefully to an empty result if the
engine is missing — it never crashes the pipeline.

Outputs: `out/annotated.png` (per-line colors, red break dots, `L<id>` labels),
`out/lines.json`, `out/lines.svg`, and `out/highlighted.png` (with `--highlight`).

### 2. Vector pipeline — `detect_line_limits.py` + `highlight_lines.py` (PDF)

Uses PyMuPDF (`fitz`) to read the **vector geometry and CAD layers** of a PDF
P&ID. This is fundamentally different from the raster pipeline: detection relies
on **CAD layer membership**, not image heuristics.

- `detect_line_limits.py`: the "line limit / pipe end" symbol is drawn on a
  dedicated CAD layer (default `"14"`), so it is detected **by layer**, not by
  geometry — on a P&ID, line breaks, flow arrows, slope, insulation and class
  change are near-identical small hollow triangles that no local heuristic
  separates reliably; the layer is unambiguous. Each symbol is placed as a point
  where its connector touches a pipe (pipe layers, default `("UTI","0")`).
  `_apex_dir` recovers the triangle's pointing direction for the chevron marker.

- `highlight_lines.py`: builds the pipe network from pipe-layer segments
  (`build_pipes`), **cuts** pipes at each blue line-limit point (`split_pipes`),
  then segments into **runs** (`assign_lines`): pipes connected without crossing
  a blue point form a run; STRONG adjacency = normal junction, WEAK adjacency =
  junction at a blue point. A run takes the line number from the nearest
  5-digit marking (`extract_line_markings`, e.g. `... 32309 C103 ...` → `32309`),
  and unnumbered runs **inherit** their neighbor's number across blue points.
  Colors come from an **Excel file** (`ligne` → hex `#RRGGBB`); run
  `--make-template` first to emit a sheet pre-filled with detected line numbers,
  fill in the colors, then run again. Output is a vector-annotated `highlight.pdf`
  (optionally `highlight.png` with `--png`).

Both PDF scripts temporarily set `page.set_rotation(0)` to work in unrotated
MediaBox space, then restore the display rotation for rendering.

## Conventions

- Keep all tunable thresholds in the relevant `Config` / `LimitConfig` /
  `HighlightConfig` dataclass — do not scatter magic numbers through the logic.
  New raster presets go in the `PRESETS` dict.
- Heavy/optional dependencies are imported lazily inside the function that needs
  them (`easyocr`, `pytesseract`, `openpyxl`, `fitz` guarded with a friendly
  `SystemExit`) so the rest of the tool still runs when one is absent.
- OpenCV uses **BGR**; color tuples in config are BGR. PDF/PyMuPDF drawing uses
  normalized **RGB** 0–1 (`_hex_to_rgb01`) while raster rendering uses BGR
  (`_hex_to_bgr`) — convert at the boundary, don't mix.
