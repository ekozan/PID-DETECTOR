#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detect_line_limits.py
=====================

Détection du symbole **« Limite de ligne / Pipe end symbol »** sur un P&ID
**vectoriel** (PDF), et placement d'un **point bleu** à la jonction du symbole
sur la conduite.

Approche (extraction vectorielle PyMuPDF, pas de raster) :
  1. Extraire tous les segments de trait ; séparer les chemins **remplis**
     (flèches pleines de sens/vanne) des chemins **non remplis**.
  2. Le symbole « Limite de ligne » contient un **petit triangle creux ~7 pt**
     (3 segments non remplis formant un triangle fermé) relié par un connecteur
     coudé à une **ligne perpendiculaire « limite »** qui touche la conduite.
  3. Filtres :
       - triangle creux isolé (taille ~7 pt) ;
       - **exclusion des soupapes** : deux triangles creux partageant un sommet
         (nœud papillon) → PSV/relief ;
       - exclusion par densité d'encre (flèches pleines résiduelles).
  4. Point = extrémité du connecteur du symbole **qui touche la conduite**
     (la jonction réellement dessinée).

Limites connues : certains symboles voisins (« Changement de classe / calo »,
callouts de numéro de ligne) contiennent un triangle similaire et peuvent
produire des faux positifs ; le discriminateur fin reste à affiner.

Dépendances : pymupdf (fitz), opencv-python, numpy.

Usage :
    python detect_line_limits.py --pdf plan.pdf --outdir out [--page 0]
"""
from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict, Counter
from dataclasses import dataclass
from typing import List, Tuple, Optional

import cv2
import numpy as np

try:
    import fitz  # PyMuPDF
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyMuPDF requis : pip install pymupdf") from exc


@dataclass
class LimitConfig:
    tri_min: float = 4.0          # longueur min d'un côté de triangle (pt)
    tri_max: float = 10.0         # longueur max (pt)
    pipe_min: float = 18.0        # longueur min d'une "conduite" (pt)
    snap: float = 1.0             # tolérance de fusion des extrémités (pt)
    dedup: float = 5.0            # distance min entre 2 détections (pt)
    pipe_search: float = 35.0     # rayon de recherche d'une conduite proche (pt)
    on_pipe_tol: float = 1.5      # tolérance "extrémité sur la conduite" (pt)
    connector_reach: float = 15.0 # distance max connecteur->triangle (pt)
    ink_max: float = 0.16         # densité d'encre max (exclut flèches pleines)
    render_zoom: float = 2.5      # zoom du rendu annoté
    dot_radius: int = 9


def _extract_segments(page) -> Tuple[List[Tuple], List[Tuple]]:
    """Retourne (segments_non_remplis, conduites_longues)."""
    nf: List[Tuple] = []
    longs: List[Tuple] = []
    for d in page.get_drawings():
        filled = d.get("fill") is not None and d.get("type") in ("f", "fs")
        for it in d["items"]:
            if it[0] != "l":
                continue
            s = (it[1].x, it[1].y, it[2].x, it[2].y)
            L = math.hypot(s[2] - s[0], s[3] - s[1])
            if not filled:
                nf.append((s, L))
            if L >= 18.0:
                longs.append(s)
    return nf, longs


def _hollow_triangles(nf, cfg: LimitConfig):
    """Triangles creux isolés (hors soupapes à sommet partagé)."""
    small = [s for s, L in nf if cfg.tri_min <= L <= cfg.tri_max]

    def key(x, y):
        return (round(x), round(y))

    adj = defaultdict(set)
    for s in small:
        a, b = key(s[0], s[1]), key(s[2], s[3])
        adj[a].add(b)
        adj[b].add(a)
    edges = set((key(s[0], s[1]), key(s[2], s[3])) for s in small)
    tris = set()
    for a, b in edges:
        for c in adj.get(a, ()):
            if c != b and c in adj.get(b, ()):
                tris.add(tuple(sorted([a, b, c])))
    # exclusion soupapes : triangle partageant un sommet avec un autre triangle
    vcount = Counter()
    for t in tris:
        for v in t:
            vcount[v] += 1
    solo = [t for t in tris if all(vcount[v] == 1 for v in t)]
    cents = [(sum(p[0] for p in t) / 3, sum(p[1] for p in t) / 3) for t in solo]
    uniq = []
    for c in sorted(cents):
        if all(abs(c[0] - u[0]) > cfg.dedup or abs(c[1] - u[1]) > cfg.dedup for u in uniq):
            uniq.append(c)
    return uniq


def _foot(px, py, s):
    x1, y1, x2, y2 = s
    dx, dy = x2 - x1, y2 - y1
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / ((dx * dx + dy * dy) or 1.0)))
    fx, fy = x1 + t * dx, y1 + t * dy
    return (fx, fy), math.hypot(px - fx, py - fy)


def detect_line_limits(page, cfg: LimitConfig = LimitConfig()) -> List[dict]:
    """Détecte les « Limite de ligne » et la jonction sur la conduite.

    Retourne une liste de dicts : {triangle:(x,y), point:(x,y)} en coords PDF
    (espace non roté de la page).
    """
    rot = page.rotation
    page.set_rotation(0)
    nf, longs = _extract_segments(page)
    tris = _hollow_triangles(nf, cfg)

    results = []
    for cx, cy in tris:
        # densité d'encre -> exclut flèches pleines
        pix = page.get_pixmap(matrix=fitz.Matrix(10, 10),
                              clip=fitz.Rect(cx - 9, cy - 9, cx + 9, cy + 9))
        im = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
        if (cv2.cvtColor(im, cv2.COLOR_RGB2GRAY) < 128).mean() > cfg.ink_max:
            continue
        near = [L for L in longs if _foot(cx, cy, L)[1] < cfg.pipe_search]
        if not near:
            continue
        # jonction : extrémité d'un segment touchant la conduite, l'autre bout
        # près du triangle (le connecteur coudé du symbole)
        junction, best = None, 1e9
        for s, L in nf:
            for (ex, ey), (ox, oy) in (((s[0], s[1]), (s[2], s[3])),
                                       ((s[2], s[3]), (s[0], s[1]))):
                if math.hypot(ox - cx, oy - cy) > cfg.connector_reach:
                    continue
                for P in near:
                    if _foot(ex, ey, P)[1] < cfg.on_pipe_tol:
                        dtri = math.hypot(ex - cx, ey - cy)
                        if dtri < best:
                            best, junction = dtri, (ex, ey)
        if junction is None:
            junction = min((_foot(cx, cy, L) for L in near), key=lambda r: r[1])[0]
        results.append({"triangle": (cx, cy), "point": junction})

    page.set_rotation(rot)
    return results


def annotate(page, results: List[dict], cfg: LimitConfig) -> np.ndarray:
    """Rend la page (orientation d'affichage) avec un point bleu par détection."""
    M = page.rotation_matrix
    z = cfg.render_zoom
    pix = page.get_pixmap(matrix=fitz.Matrix(z, z))
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR).copy()
    for i, r in enumerate(results, 1):
        P = fitz.Point(*r["point"]) * M * z
        x, y = int(P.x), int(P.y)
        cv2.circle(img, (x, y), cfg.dot_radius, (255, 0, 0), -1)
        cv2.circle(img, (x, y), cfg.dot_radius, (0, 0, 0), 1)
    return img


def main() -> int:
    ap = argparse.ArgumentParser(description="Détection des « Limite de ligne » sur P&ID PDF.")
    ap.add_argument("--pdf", required=True, help="P&ID vectoriel (PDF)")
    ap.add_argument("--page", type=int, default=0)
    ap.add_argument("--outdir", default="out")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    doc = fitz.open(args.pdf)
    page = doc[args.page]
    cfg = LimitConfig()
    results = detect_line_limits(page, cfg)
    print(f"[OK] {len(results)} « Limite de ligne » détectés")

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
