#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detect_line_limits.py
=====================

Détection du symbole **« Limite de ligne / Pipe end symbol »** sur un P&ID
**vectoriel** (PDF), et placement d'un **point bleu** à la jonction du symbole
sur la conduite.

Méthode (robuste, par calque CAO)
---------------------------------
Le symbole « Limite de ligne » est dessiné sur un **calque dédié** (ici ``"14"``).
On le détecte donc **directement par calque** plutôt que par reconnaissance
géométrique : sur un P&ID, les line breaks, flèches de sens, pente, calo et
changement de classe sont tous de petits triangles creux quasi identiques, et
aucune heuristique locale ne les sépare de façon fiable. Le calque, lui, est
non ambigu.

Étapes :
  1. Récupérer tous les traits du calque ``symbol_layer`` → regrouper en symboles.
  2. Pour chaque symbole, placer le point à l'extrémité de son connecteur qui
     **touche une conduite** (calques ``pipe_layers``), sinon à la projection du
     centre du symbole sur la conduite la plus proche.

Dépendances : pymupdf (fitz), opencv-python, numpy.

Usage :
    python detect_line_limits.py --pdf plan.pdf --outdir out [--page 0] [--symbol-layer 14]
"""
from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass
from typing import List, Tuple

import cv2
import numpy as np

try:
    import fitz  # PyMuPDF
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyMuPDF requis : pip install pymupdf") from exc


@dataclass
class LimitConfig:
    # Calque CAO des symboles « Limite de ligne » (détection fiable par calque).
    symbol_layer: str = "14"
    cluster_tol: float = 18.0     # rayon de regroupement des traits d'un symbole (pt)
    # Calques "conduite" pour poser le point sur le tuyau (exclut les leaders
    # d'annotation/instrument qui ne sont PAS des conduites).
    pipe_layers: Tuple[str, ...] = ("UTI", "0")
    pipe_search: float = 40.0     # rayon de recherche d'une conduite proche (pt)
    on_pipe_tol: float = 2.0      # tolérance "extrémité du symbole sur la conduite" (pt)
    render_zoom: float = 2.5      # zoom du rendu annoté
    dot_radius: int = 9


def _seg_of_item(it) -> Tuple[float, float, float, float]:
    """Renvoie (x1,y1,x2,y2) pour un item ligne 'l' ou courbe 'c' (corde)."""
    if it[0] == "l":
        return (it[1].x, it[1].y, it[2].x, it[2].y)
    if it[0] == "c":
        ps = it[1:]
        return (ps[0].x, ps[0].y, ps[-1].x, ps[-1].y)
    return None


def _foot(px, py, s):
    x1, y1, x2, y2 = s
    dx, dy = x2 - x1, y2 - y1
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / ((dx * dx + dy * dy) or 1.0)))
    fx, fy = x1 + t * dx, y1 + t * dy
    return (fx, fy), math.hypot(px - fx, py - fy)


def detect_line_limits(page, cfg: LimitConfig = LimitConfig()) -> List[dict]:
    """Détecte les « Limite de ligne » (par calque) et place le point sur la conduite.

    Retourne une liste de dicts : {symbol:(x,y), point:(x,y)} en coords PDF
    (espace non roté de la page).
    """
    rot = page.rotation
    page.set_rotation(0)

    sym_segs: List[Tuple] = []   # traits du calque symbole
    longs: List[Tuple] = []      # conduites (calques tuyauterie, >= 18pt)
    for d in page.get_drawings():
        lay = d.get("layer")
        on_pipe_layer = lay in cfg.pipe_layers
        is_symbol = lay == cfg.symbol_layer
        for it in d["items"]:
            s = _seg_of_item(it)
            if s is None:
                continue
            if is_symbol:
                sym_segs.append(s)
            if on_pipe_layer and math.hypot(s[2] - s[0], s[3] - s[1]) >= 18.0:
                longs.append(s)

    # 1) regrouper les traits du calque symbole en symboles distincts
    clusters: List[list] = []   # [cx, cy, [segs]]
    for s in sym_segs:
        mx, my = (s[0] + s[2]) / 2, (s[1] + s[3]) / 2
        placed = False
        for cl in clusters:
            if math.hypot(cl[0] - mx, cl[1] - my) < cfg.cluster_tol:
                cl[2].append(s)
                n = len(cl[2])
                cl[0] = (cl[0] * (n - 1) + mx) / n
                cl[1] = (cl[1] * (n - 1) + my) / n
                placed = True
                break
        if not placed:
            clusters.append([mx, my, [s]])

    # 2) placer le point sur la conduite
    results = []
    for cx, cy, segs in clusters:
        near = [L for L in longs if _foot(cx, cy, L)[1] < cfg.pipe_search]
        if near:
            # extrémité d'un trait du symbole qui touche une conduite
            junction, best = None, 1e9
            for s in segs:
                for ex, ey in ((s[0], s[1]), (s[2], s[3])):
                    for L in near:
                        if _foot(ex, ey, L)[1] < cfg.on_pipe_tol:
                            d = math.hypot(ex - cx, ey - cy)
                            if d < best:
                                best, junction = d, (ex, ey)
            if junction is None:  # repli : projection du centre sur la conduite
                junction = min((_foot(cx, cy, L) for L in near), key=lambda r: r[1])[0]
        else:
            junction = (cx, cy)
        results.append({"symbol": (cx, cy), "point": junction})

    page.set_rotation(rot)
    return results


def annotate(page, results: List[dict], cfg: LimitConfig) -> np.ndarray:
    """Rend la page (orientation d'affichage) avec un point bleu par détection."""
    M = page.rotation_matrix
    z = cfg.render_zoom
    pix = page.get_pixmap(matrix=fitz.Matrix(z, z))
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR).copy()
    for r in results:
        P = fitz.Point(*r["point"]) * M * z
        x, y = int(P.x), int(P.y)
        cv2.circle(img, (x, y), cfg.dot_radius, (255, 0, 0), -1)
        cv2.circle(img, (x, y), cfg.dot_radius, (0, 0, 0), 1)
    return img


def main() -> int:
    ap = argparse.ArgumentParser(description="Détection des « Limite de ligne » sur P&ID PDF.")
    ap.add_argument("--pdf", required=True, help="P&ID vectoriel (PDF)")
    ap.add_argument("--page", type=int, default=0)
    ap.add_argument("--symbol-layer", default="14",
                    help="calque CAO des symboles « Limite de ligne » (défaut: 14)")
    ap.add_argument("--outdir", default="out")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    doc = fitz.open(args.pdf)
    page = doc[args.page]
    cfg = LimitConfig(symbol_layer=args.symbol_layer)
    results = detect_line_limits(page, cfg)
    print(f"[OK] {len(results)} « Limite de ligne » détectés (calque {cfg.symbol_layer})")

    img = annotate(page, results, cfg)
    img_path = os.path.join(args.outdir, "line_limits.png")
    cv2.imwrite(img_path, img)
    with open(os.path.join(args.outdir, "line_limits.json"), "w", encoding="utf-8") as f:
        json.dump({"line_limits": [{"point": r["point"]} for r in results]}, f, indent=2)
    print(f"     image : {img_path}")
    print(f"     json  : {os.path.join(args.outdir, 'line_limits.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
