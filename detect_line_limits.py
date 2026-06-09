#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detect_line_limits.py
=====================

Détection **vectorielle** du symbole **« Limite de ligne / Pipe end symbol »**
sur un P&ID PDF, et placement d'un point à la jonction du symbole sur la conduite.

Module de détection importé par ``highlight_lines.py`` (la commande unique). Tout
est lu depuis les **données vectorielles** du PDF (traits, calques) : aucun rendu
ni traitement d'image n'est nécessaire.

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

Dépendances : pymupdf (fitz).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Tuple

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


def _apex_dir(cx, cy, small):
    """Direction de l'apex du triangle ▽ (le sens du symbole) : deux côtés de
    longueurs ~égales se rejoignent à l'apex ; direction = milieu de la base
    vers l'apex. `small` = petits segments (~4-10pt) proches."""
    near = [s for s in small if abs((s[0] + s[2]) / 2 - cx) < 14 and abs((s[1] + s[3]) / 2 - cy) < 14]
    best = None  # (longueur_cote, apex, base_mid)
    for i in range(len(near)):
        for j in range(i + 1, len(near)):
            a, b = near[i], near[j]
            la = math.hypot(a[2] - a[0], a[3] - a[1])
            lb = math.hypot(b[2] - b[0], b[3] - b[1])
            if abs(la - lb) > 2.5:
                continue
            for ax, ay, afx, afy in ((a[0], a[1], a[2], a[3]), (a[2], a[3], a[0], a[1])):
                for bx, by, bfx, bfy in ((b[0], b[1], b[2], b[3]), (b[2], b[3], b[0], b[1])):
                    if math.hypot(ax - bx, ay - by) < 2.5:   # sommet partagé = apex
                        apex = ((ax + bx) / 2, (ay + by) / 2)
                        bm = ((afx + bfx) / 2, (afy + bfy) / 2)
                        side = (la + lb) / 2
                        if best is None or side > best[0]:
                            best = (side, apex, bm)
    if best is None:
        return None
    _, apex, bm = best
    dx, dy = apex[0] - bm[0], apex[1] - bm[1]
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-6 else None


def detect_line_limits(page, cfg: LimitConfig = LimitConfig()) -> List[dict]:
    """Détecte les « Limite de ligne » (par calque) et place le point sur la conduite.

    Retourne une liste de dicts : {symbol:(x,y), point:(x,y)} en coords PDF
    (espace non roté de la page).
    """
    rot = page.rotation
    page.set_rotation(0)

    sym_segs: List[Tuple] = []   # traits du calque symbole
    longs: List[Tuple] = []      # conduites (calques tuyauterie, >= 18pt)
    small: List[Tuple] = []      # petits traits (triangles ▽) pour le sens
    for d in page.get_drawings():
        lay = d.get("layer")
        on_pipe_layer = lay in cfg.pipe_layers
        is_symbol = lay == cfg.symbol_layer
        filled = d.get("fill") is not None and d.get("type") in ("f", "fs")
        for it in d["items"]:
            s = _seg_of_item(it)
            if s is None:
                continue
            if is_symbol:
                sym_segs.append(s)
            L = math.hypot(s[2] - s[0], s[3] - s[1])
            if on_pipe_layer and L >= 18.0:
                longs.append(s)
            if not filled and 4.0 <= L <= 10.0:
                small.append(s)

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
        direction = _apex_dir(cx, cy, small)   # sens du triangle ▽
        results.append({"symbol": (cx, cy), "point": junction, "dir": direction})

    # dédoublonnage : symboles distincts projetés sur le même point
    dedup = []
    for r in results:
        if all(math.hypot(r["point"][0] - u["point"][0],
                          r["point"][1] - u["point"][1]) > 6.0 for u in dedup):
            dedup.append(r)

    page.set_rotation(rot)
    return dedup
