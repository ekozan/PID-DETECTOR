#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pid_detector.py
===============

Détection et suivi des lignes de tuyauterie sur un plan P&ID (norme ISA).

Pipeline :
    load_image -> detect_edges -> detect_lines -> merge_segments
        -> detect_break_symbols -> reconnect_lines -> build_graph
        -> run_ocr -> associate_text_to_lines -> colorize_lines -> export_results

Idée directrice
---------------
Chaque segment fusionné est une *arête* d'un graphe. Les extrémités proches sont
"snappées" sur des noeuds partagés (les intersections). Une **ligne logique** est
une **composante connexe** de ce graphe.

Le symbole de rupture ISA (triangle ouvert / "Y" inversé) NE coupe PAS la ligne :
il est détecté puis transformé en *arête de reconnexion* entre les deux segments
de part et d'autre. Les deux côtés appartiennent donc à la même composante connexe
et reçoivent le même identifiant de ligne / la même couleur.

Dépendances : opencv-python, numpy, networkx, (easyocr | pytesseract).

Exemple :
    python pid_detector.py --input plan.png --outdir out --debug
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

try:
    import networkx as nx
except ImportError as exc:  # pragma: no cover - dépendance obligatoire
    raise SystemExit("networkx est requis : pip install networkx") from exc


# ============================================================================
#  CONFIGURATION (tous les seuils réglables au même endroit)
# ============================================================================
@dataclass
class Config:
    """Paramètres réglables du pipeline."""

    # --- Pré-traitement / Canny ---
    gaussian_blur: int = 3            # noyau de flou (impair, 0 = désactivé)
    canny_low: int = 50
    canny_high: int = 150

    # --- HoughLinesP ---
    hough_threshold: int = 50         # votes minimum
    hough_min_line_length: int = 30   # longueur minimale d'un segment (px)
    hough_max_line_gap: int = 5       # trou maximal toléré dans un segment (px)

    # --- Classification d'orientation ---
    angle_tol_deg: float = 10.0       # tolérance pour "horizontal"/"vertical"

    # --- Fusion des segments (merge_segments) ---
    merge_perp_tol: int = 6           # écart perpendiculaire max pour être colinéaire (px)
    merge_gap_tol: int = 25           # trou max le long de l'axe pour fusionner (px)

    # --- Détection du symbole de rupture ---
    break_min_area: int = 40          # aire min du contour (px^2)
    break_max_area: int = 4000        # aire max du contour (px^2)
    break_approx_eps: float = 0.04    # epsilon relatif pour approxPolyDP
    break_min_vertices: int = 3       # un triangle a 3 sommets
    break_max_vertices: int = 4       # tolère 4 sommets (triangle ouvert / "Y")
    break_ar_min: float = 0.3         # ratio h/l plausible
    break_ar_max: float = 3.5

    # --- Reconnexion à travers une rupture ---
    reconnect_max_dist: int = 100     # distance max entre 2 segments à reconnecter (px)
    reconnect_align_tol: int = 12     # tolérance d'alignement perpendiculaire (px)
    reconnect_angle_tol_deg: float = 12.0

    # --- Construction du graphe ---
    node_snap_tol: int = 12           # rayon de fusion des extrémités en noeuds (px)

    # --- OCR ---
    ocr_enabled: bool = True
    ocr_engine: str = "easyocr"       # "easyocr" | "tesseract"
    ocr_languages: Tuple[str, ...] = ("en",)
    ocr_min_confidence: float = 0.30
    ocr_upscale: float = 1.0          # facteur d'agrandissement avant OCR (petit texte)
    text_to_line_max_dist: int = 80   # distance texte->ligne max pour association (px)

    # --- Détection des flèches (bonus) ---
    detect_arrows: bool = True
    arrow_min_area: int = 30
    arrow_max_area: int = 2500

    # --- Composants "traversants" (vannes, nuages calorifuge, bulles d'instrument) ---
    # Ils coupent visuellement la ligne mais ne la coupent pas logiquement :
    # traités comme des points de jonction qui déclenchent une reconnexion.
    detect_fittings: bool = True
    fitting_min_area: int = 30
    fitting_max_area: int = 6000
    fitting_ar_min: float = 0.3       # ratio largeur/hauteur "compact"
    fitting_ar_max: float = 3.5

    # --- Surlignage d'une ligne (mode highlight) ---
    highlight_color: Tuple[int, int, int] = (0, 255, 255)  # BGR jaune
    highlight_thickness: int = 10
    highlight_alpha: float = 0.45

    # --- Marquage des ruptures de ligne ---
    break_dot_color: Tuple[int, int, int] = (255, 0, 0)  # BGR bleu
    break_dot_radius: int = 5

    # --- Sorties ---
    draw_thickness: int = 3
    export_svg: bool = True
    debug: bool = False

    # Expressions régulières des marquages ISA à conserver
    ocr_keep_patterns: Tuple[str, ...] = (
        r"\bDN\s?\w+\b",                 # DN50, DNxx ...
        r"\b\d{1,4}\s?x\s?\d+(?:\.\d+)?\b",  # 50x2.4 (diamètre x épaisseur)
        r"\b316L?\b|\b304L?\b|\bINOX\b|\bCS\b|\bA106\b",  # matériaux
        r"\bAZOTE\b|\bAIR\b|\bN2\b|\bO2\b|\bEAU\b|\bVAPEUR\b|\bGAZ\b",  # produits
    )


# Presets de seuils. "real_plan" est calibré sur des P&ID scannés réels où la
# conduite principale est quasi-horizontale et traverse vannes / nuages / ruptures.
PRESETS: Dict[str, dict] = {
    "default": {},
    "real_plan": dict(
        angle_tol_deg=6.0,
        merge_perp_tol=8,
        merge_gap_tol=160,
        node_snap_tol=40,
        reconnect_max_dist=250,
        reconnect_align_tol=18,
        draw_thickness=4,
        ocr_upscale=3.0,          # plans scannés : petit texte -> agrandir avant OCR
        ocr_min_confidence=0.25,
    ),
}


def apply_preset(cfg: "Config", name: str) -> "Config":
    """Applique un preset de seuils sur une configuration existante."""
    if name not in PRESETS:
        raise ValueError(f"Preset inconnu : {name} (dispo : {', '.join(PRESETS)})")
    for key, val in PRESETS[name].items():
        setattr(cfg, key, val)
    return cfg


# ============================================================================
#  STRUCTURES DE DONNEES
# ============================================================================
@dataclass
class Segment:
    """Un segment de droite orienté, avec métadonnées."""

    x1: int
    y1: int
    x2: int
    y2: int
    orient: str = "other"      # "h", "v" ou "other"
    line_id: int = -1          # rempli après construction du graphe

    # ---- propriétés géométriques utiles ----
    @property
    def p1(self) -> Tuple[int, int]:
        return (self.x1, self.y1)

    @property
    def p2(self) -> Tuple[int, int]:
        return (self.x2, self.y2)

    @property
    def midpoint(self) -> Tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def length(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)

    @property
    def angle_deg(self) -> float:
        """Angle dans [0, 180)."""
        a = math.degrees(math.atan2(self.y2 - self.y1, self.x2 - self.x1))
        return a % 180.0

    def as_list(self) -> List[int]:
        return [int(self.x1), int(self.y1), int(self.x2), int(self.y2)]


@dataclass
class BreakSymbol:
    """Position d'un symbole de rupture ISA détecté."""

    cx: int
    cy: int
    bbox: Tuple[int, int, int, int]   # x, y, w, h
    vertices: int = 0


@dataclass
class TextItem:
    """Un texte OCR retenu."""

    text: str
    cx: int
    cy: int
    bbox: Tuple[int, int, int, int]
    confidence: float
    line_id: int = -1


# ============================================================================
#  1. CHARGEMENT
# ============================================================================
def load_image(path: str) -> np.ndarray:
    """Charge une image P&ID en BGR. Lève FileNotFoundError si absente."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Image introuvable : {path}")
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"Impossible de décoder l'image : {path}")
    return img


# ============================================================================
#  2. DETECTION DES CONTOURS (Canny)
# ============================================================================
def detect_edges(img: np.ndarray, cfg: Config) -> np.ndarray:
    """Convertit en niveaux de gris, débruite légèrement, applique Canny."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if cfg.gaussian_blur and cfg.gaussian_blur >= 3:
        k = cfg.gaussian_blur | 1  # force impair
        gray = cv2.GaussianBlur(gray, (k, k), 0)
    edges = cv2.Canny(gray, cfg.canny_low, cfg.canny_high, apertureSize=3)
    return edges


# ============================================================================
#  3. DETECTION DES LIGNES (HoughLinesP) + classification d'orientation
# ============================================================================
def _classify_orientation(seg: Segment, tol_deg: float) -> str:
    a = seg.angle_deg
    if a <= tol_deg or a >= 180.0 - tol_deg:
        return "h"
    if abs(a - 90.0) <= tol_deg:
        return "v"
    return "other"


def detect_lines(edges: np.ndarray, cfg: Config) -> List[Segment]:
    """Détecte les segments via Hough probabiliste et les classe (h/v/other)."""
    raw = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180.0,
        threshold=cfg.hough_threshold,
        minLineLength=cfg.hough_min_line_length,
        maxLineGap=cfg.hough_max_line_gap,
    )
    segments: List[Segment] = []
    if raw is None:
        return segments
    for x1, y1, x2, y2 in raw[:, 0, :]:
        seg = Segment(int(x1), int(y1), int(x2), int(y2))
        # Oriente toujours p1 -> p2 dans le sens croissant (x puis y) : stabilise la fusion.
        if (seg.x2, seg.y2) < (seg.x1, seg.y1):
            seg.x1, seg.y1, seg.x2, seg.y2 = seg.x2, seg.y2, seg.x1, seg.y1
        seg.orient = _classify_orientation(seg, cfg.angle_tol_deg)
        segments.append(seg)
    return segments


# ============================================================================
#  4. FUSION DES SEGMENTS COLINEAIRES -> polylignes
# ============================================================================
def _merge_axis_group(group: List[Segment], axis: str, cfg: Config) -> List[Segment]:
    """
    Fusionne un groupe de segments quasi-colinéaires.
    axis="h" : tri par x, projection sur y constant.
    axis="v" : tri par y, projection sur x constant.
    """
    if not group:
        return []

    if axis == "h":
        group.sort(key=lambda s: min(s.x1, s.x2))
        get_lo = lambda s: min(s.x1, s.x2)
        get_hi = lambda s: max(s.x1, s.x2)
        cross = lambda s: (s.y1 + s.y2) / 2.0
    else:
        group.sort(key=lambda s: min(s.y1, s.y2))
        get_lo = lambda s: min(s.y1, s.y2)
        get_hi = lambda s: max(s.y1, s.y2)
        cross = lambda s: (s.x1 + s.x2) / 2.0

    merged: List[Segment] = []
    cur_lo = get_lo(group[0])
    cur_hi = get_hi(group[0])
    cur_cross = [cross(group[0])]

    for seg in group[1:]:
        lo, hi = get_lo(seg), get_hi(seg)
        # chevauchement ou trou inférieur au seuil -> on prolonge
        if lo <= cur_hi + cfg.merge_gap_tol:
            cur_hi = max(cur_hi, hi)
            cur_cross.append(cross(seg))
        else:
            merged.append(_build_axis_segment(axis, cur_lo, cur_hi, cur_cross))
            cur_lo, cur_hi, cur_cross = lo, hi, [cross(seg)]

    merged.append(_build_axis_segment(axis, cur_lo, cur_hi, cur_cross))
    return merged


def _build_axis_segment(axis: str, lo: float, hi: float, cross_vals: List[float]) -> Segment:
    c = int(round(sum(cross_vals) / len(cross_vals)))
    if axis == "h":
        s = Segment(int(lo), c, int(hi), c, orient="h")
    else:
        s = Segment(c, int(lo), c, int(hi), orient="v")
    return s


def merge_segments(segments: Sequence[Segment], cfg: Config) -> List[Segment]:
    """
    Regroupe les segments par alignement + proximité puis fusionne chaque
    groupe en une polyligne (un segment représentatif).
    """
    horizontals = [s for s in segments if s.orient == "h"]
    verticals = [s for s in segments if s.orient == "v"]
    others = [s for s in segments if s.orient == "other"]

    # --- Groupage des horizontales par bande de y proche ---
    merged: List[Segment] = []
    merged += _bucket_and_merge(horizontals, axis="h", cfg=cfg)
    merged += _bucket_and_merge(verticals, axis="v", cfg=cfg)
    # Les "other" (diagonales) sont conservées telles quelles : utiles pour le graphe.
    merged += others
    return merged


def _bucket_and_merge(segs: List[Segment], axis: str, cfg: Config) -> List[Segment]:
    """Regroupe par valeur transversale proche (clustering 1D) puis fusionne."""
    if not segs:
        return []
    key = (lambda s: (s.y1 + s.y2) / 2.0) if axis == "h" else (lambda s: (s.x1 + s.x2) / 2.0)
    segs_sorted = sorted(segs, key=key)

    result: List[Segment] = []
    bucket: List[Segment] = [segs_sorted[0]]
    ref = key(segs_sorted[0])
    for s in segs_sorted[1:]:
        if abs(key(s) - ref) <= cfg.merge_perp_tol:
            bucket.append(s)
            ref = (ref * (len(bucket) - 1) + key(s)) / len(bucket)  # moyenne glissante
        else:
            result += _merge_axis_group(bucket, axis, cfg)
            bucket = [s]
            ref = key(s)
    result += _merge_axis_group(bucket, axis, cfg)
    return result


# ============================================================================
#  5. DETECTION DU SYMBOLE DE RUPTURE (contours + approx polygonale)
# ============================================================================
def detect_break_symbols(img: np.ndarray, cfg: Config) -> List[BreakSymbol]:
    """
    Détecte les symboles de rupture ISA (triangle ouvert vers le bas / "Y" inversé).

    Méthode recommandée :
        threshold -> findContours -> approxPolyDP -> filtre triangle + taille + ratio.
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # Binarisation robuste (les traits sont sombres sur fond clair sur un P&ID).
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    contours, _ = cv2.findContours(th, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    breaks: List[BreakSymbol] = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if not (cfg.break_min_area <= area <= cfg.break_max_area):
            continue
        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, cfg.break_approx_eps * peri, True)
        nv = len(approx)
        if not (cfg.break_min_vertices <= nv <= cfg.break_max_vertices):
            continue
        x, y, w, h = cv2.boundingRect(approx)
        if w == 0 or h == 0:
            continue
        ar = h / float(w)
        if not (cfg.break_ar_min <= ar <= cfg.break_ar_max):
            continue
        # Un triangle de rupture est plutôt "compact" : aire >= 50% de la bbox/2.
        if area < 0.25 * w * h:
            continue
        cx, cy = x + w // 2, y + h // 2
        breaks.append(BreakSymbol(cx=cx, cy=cy, bbox=(x, y, w, h), vertices=nv))
    return breaks


# ============================================================================
#  5bis. COMPOSANTS TRAVERSANTS (vannes, nuages, bulles d'instrument)
# ============================================================================
def detect_fittings(img: np.ndarray, cfg: Config) -> List[Tuple[int, int]]:
    """
    Détecte les symboles "traversants" qui coupent visuellement la ligne sans la
    couper logiquement : vannes (noeud papillon), nuages de calorifuge/tracé,
    bulles d'instrument. Renvoie leurs centroïdes.

    Ils sont fournis comme points de jonction à `reconnect_lines`, qui ne relie
    que des segments réellement colinéaires de part et d'autre : un blob isolé
    (texte, cartouche) ne crée donc pas de faux pont.
    """
    if not cfg.detect_fittings:
        return []
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    points: List[Tuple[int, int]] = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if not (cfg.fitting_min_area <= area <= cfg.fitting_max_area):
            continue
        x, y, w, h = cv2.boundingRect(cnt)
        if w == 0 or h == 0:
            continue
        ar = w / float(h)
        if not (cfg.fitting_ar_min <= ar <= cfg.fitting_ar_max):
            continue  # écarte les contours très allongés (= morceaux de ligne)
        M = cv2.moments(cnt)
        if M["m00"] <= 0:
            continue
        points.append((int(M["m10"] / M["m00"]), int(M["m01"] / M["m00"])))
    return points


# ============================================================================
#  6. RECONNEXION DES LIGNES A TRAVERS LES RUPTURES / COMPOSANTS
# ============================================================================
def _endpoint_near(seg: Segment, pt: Tuple[int, int]) -> Tuple[int, int]:
    """Retourne l'extrémité de `seg` la plus proche de `pt`."""
    d1 = math.hypot(seg.x1 - pt[0], seg.y1 - pt[1])
    d2 = math.hypot(seg.x2 - pt[0], seg.y2 - pt[1])
    return seg.p1 if d1 <= d2 else seg.p2


def reconnect_lines(
    segments: List[Segment],
    junctions: Sequence[Tuple[int, int]],
    cfg: Config,
) -> List[Tuple[int, int, Tuple[int, int]]]:
    """
    Pour chaque point de jonction (rupture ISA OU composant traversant), cherche
    deux segments alignés de part et d'autre et renvoie la liste des ponts
    (i, j, centre) à reconnecter (= arêtes virtuelles ajoutées au graphe).

    Conditions : alignement transversal, distance < seuil, angle similaire.
    """
    bridges: List[Tuple[int, int, Tuple[int, int]]] = []
    for center in junctions:
        # candidats : segments dont une extrémité passe près du symbole
        candidates: List[Tuple[int, float, Tuple[int, int]]] = []
        for idx, seg in enumerate(segments):
            ep = _endpoint_near(seg, center)
            d = math.hypot(ep[0] - center[0], ep[1] - center[1])
            if d <= cfg.reconnect_max_dist:
                candidates.append((idx, d, ep))

        # Apparie les candidats compatibles (alignés + angle proche), 2 par 2.
        candidates.sort(key=lambda c: c[1])
        used = set()
        for a in range(len(candidates)):
            ia, da, ea = candidates[a]
            if ia in used:
                continue
            for b in range(a + 1, len(candidates)):
                ib, db, eb = candidates[b]
                if ib in used:
                    continue
                sa, sb = segments[ia], segments[ib]
                if _are_collinear_across(sa, sb, ea, eb, center, cfg):
                    bridges.append((ia, ib, center))
                    used.add(ia)
                    used.add(ib)
                    break
    return bridges


def _are_collinear_across(
    sa: Segment,
    sb: Segment,
    ea: Tuple[int, int],
    eb: Tuple[int, int],
    center: Tuple[int, int],
    cfg: Config,
) -> bool:
    """Deux segments sont reconnectables s'ils ont un angle proche, qu'ils sont
    de part et d'autre du symbole, et alignés transversalement."""
    # angles proches (modulo 180)
    dang = abs(sa.angle_deg - sb.angle_deg)
    dang = min(dang, 180.0 - dang)
    if dang > cfg.reconnect_angle_tol_deg:
        return False

    # extrémités de part et d'autre du centre (produit scalaire opposé)
    va = (ea[0] - center[0], ea[1] - center[1])
    vb = (eb[0] - center[0], eb[1] - center[1])
    if va[0] * vb[0] + va[1] * vb[1] > 0:  # même côté -> ce n'est pas une reconnexion
        return False

    # alignement transversal : la distance du point eb à la droite (ea, direction sa)
    ang = math.radians(sa.angle_deg)
    dirx, diry = math.cos(ang), math.sin(ang)
    wx, wy = eb[0] - ea[0], eb[1] - ea[1]
    perp = abs(wx * (-diry) + wy * dirx)
    return perp <= cfg.reconnect_align_tol


# ============================================================================
#  7. CONSTRUCTION DU GRAPHE (networkx) -> lignes = composantes connexes
# ============================================================================
def build_graph(
    segments: List[Segment],
    bridges: Sequence[Tuple[int, int, Tuple[int, int]]],
    cfg: Config,
) -> Tuple[nx.Graph, Dict[int, List[int]]]:
    """
    Construit un graphe :
        - noeuds = extrémités de segments (snappées entre elles si proches),
        - arêtes = segments + ponts de reconnexion,
        - une ligne = composante connexe.

    Effet de bord : remplit `seg.line_id` pour chaque segment.
    Retourne (graphe, mapping line_id -> liste d'indices de segments).
    """
    G = nx.Graph()
    nodes: List[Tuple[float, float]] = []   # coordonnées des noeuds créés

    def get_node(pt: Tuple[int, int]) -> int:
        """Renvoie l'id d'un noeud existant proche, sinon en crée un."""
        for nid, (nx_, ny_) in enumerate(nodes):
            if math.hypot(nx_ - pt[0], ny_ - pt[1]) <= cfg.node_snap_tol:
                return nid
        nodes.append((float(pt[0]), float(pt[1])))
        nid = len(nodes) - 1
        G.add_node(nid, pos=(float(pt[0]), float(pt[1])))
        return nid

    # une arête par segment, mémorise quel segment porte cette arête
    seg_nodes: List[Tuple[int, int]] = []
    for idx, seg in enumerate(segments):
        n1 = get_node(seg.p1)
        n2 = get_node(seg.p2)
        seg_nodes.append((n1, n2))
        if n1 != n2:
            G.add_edge(n1, n2, seg_index=idx)

    # ponts de reconnexion (rupture/composant) : relient les noeuds proches du symbole
    for ia, ib, _center in bridges:
        a_n1, a_n2 = seg_nodes[ia]
        b_n1, b_n2 = seg_nodes[ib]
        # relie les extrémités les plus proches entre les deux segments
        best = None
        for na in (a_n1, a_n2):
            for nb in (b_n1, b_n2):
                d = math.hypot(
                    nodes[na][0] - nodes[nb][0], nodes[na][1] - nodes[nb][1]
                )
                if best is None or d < best[0]:
                    best = (d, na, nb)
        if best and best[1] != best[2]:
            G.add_edge(best[1], best[2], bridge=True)

    # composantes connexes -> identifiants de lignes
    line_map: Dict[int, List[int]] = {}
    for line_id, comp in enumerate(nx.connected_components(G), start=1):
        for u, v, data in G.edges(comp, data=True):
            si = data.get("seg_index")
            if si is not None:
                segments[si].line_id = line_id
                line_map.setdefault(line_id, []).append(si)
    return G, line_map


# ============================================================================
#  8. OCR DES TEXTES + filtrage des marquages ISA
# ============================================================================
def _ocr_easyocr(img: np.ndarray, cfg: Config) -> List[Tuple[str, Tuple[int, int, int, int], float]]:
    import easyocr  # import paresseux : lourd à charger
    reader = easyocr.Reader(list(cfg.ocr_languages), gpu=False, verbose=False)
    results = reader.readtext(img)
    out = []
    for box, text, conf in results:
        xs = [int(p[0]) for p in box]
        ys = [int(p[1]) for p in box]
        bbox = (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))
        out.append((text, bbox, float(conf)))
    return out


def _ocr_tesseract(img: np.ndarray, cfg: Config) -> List[Tuple[str, Tuple[int, int, int, int], float]]:
    import pytesseract
    from pytesseract import Output
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    data = pytesseract.image_to_data(gray, output_type=Output.DICT)
    out = []
    n = len(data["text"])
    for i in range(n):
        text = data["text"][i].strip()
        if not text:
            continue
        conf = float(data["conf"][i]) / 100.0 if data["conf"][i] not in ("-1", -1) else 0.0
        bbox = (data["left"][i], data["top"][i], data["width"][i], data["height"][i])
        out.append((text, bbox, conf))
    return out


def run_ocr(img: np.ndarray, cfg: Config) -> List[TextItem]:
    """
    Lance l'OCR (EasyOCR par défaut, Tesseract en repli) et conserve les
    textes pertinents (marquages ISA) via les regex de la config.
    En cas d'absence du moteur, renvoie une liste vide sans planter.
    """
    if not cfg.ocr_enabled:
        return []

    import re

    # Agrandissement optionnel : sur les plans scannés, le texte le long des
    # lignes est minuscule et l'OCR échoue à l'échelle native. On agrandit,
    # on lit, puis on reprojette les coordonnées à l'échelle d'origine.
    scale = max(1.0, float(cfg.ocr_upscale))
    ocr_img = img
    if scale > 1.0:
        ocr_img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    try:
        if cfg.ocr_engine == "tesseract":
            raw = _ocr_tesseract(ocr_img, cfg)
        else:
            raw = _ocr_easyocr(ocr_img, cfg)
    except Exception as exc:  # moteur absent / échec : on dégrade proprement
        print(f"[OCR] désactivé ({exc})")
        return []

    if scale > 1.0:  # reprojette les bbox vers l'échelle native
        raw = [
            (t, (int(x / scale), int(y / scale), int(w / scale), int(h / scale)), c)
            for t, (x, y, w, h), c in raw
        ]

    patterns = [re.compile(p, re.IGNORECASE) for p in cfg.ocr_keep_patterns]
    items: List[TextItem] = []
    for text, bbox, conf in raw:
        if conf < cfg.ocr_min_confidence:
            continue
        keep = any(p.search(text) for p in patterns) if patterns else True
        if not keep:
            continue
        x, y, w, h = bbox
        items.append(
            TextItem(text=text.strip(), cx=x + w // 2, cy=y + h // 2, bbox=bbox, confidence=conf)
        )
    return items


# ============================================================================
#  9. ASSOCIATION TEXTE -> LIGNE
# ============================================================================
def _point_segment_distance(px: float, py: float, seg: Segment) -> float:
    """Distance d'un point au segment [p1,p2]."""
    x1, y1, x2, y2 = seg.x1, seg.y1, seg.x2, seg.y2
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / float(dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    projx, projy = x1 + t * dx, y1 + t * dy
    return math.hypot(px - projx, py - projy)


def associate_text_to_lines(
    texts: List[TextItem], segments: List[Segment], cfg: Config
) -> Dict[int, List[str]]:
    """
    Associe chaque texte à la ligne (segment) la plus proche, sous réserve de
    rester sous `text_to_line_max_dist`. Retourne line_id -> liste de textes.
    """
    labels: Dict[int, List[str]] = {}
    for t in texts:
        best_d = float("inf")
        best_line = -1
        for seg in segments:
            if seg.line_id < 0:
                continue
            d = _point_segment_distance(t.cx, t.cy, seg)
            if d < best_d:
                best_d = d
                best_line = seg.line_id
        if best_line >= 0 and best_d <= cfg.text_to_line_max_dist:
            t.line_id = best_line
            labels.setdefault(best_line, []).append(t.text)
    return labels


# ============================================================================
#  BONUS : détection du sens (flèches)
# ============================================================================
def detect_arrows(img: np.ndarray, cfg: Config) -> List[Tuple[int, int]]:
    """
    Détection grossière des flèches de direction (petits triangles pleins).
    Renvoie les centroïdes. Sert d'indice de sens d'écoulement.
    """
    if not cfg.detect_arrows:
        return []
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    arrows: List[Tuple[int, int]] = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if not (cfg.arrow_min_area <= area <= cfg.arrow_max_area):
            continue
        approx = cv2.approxPolyDP(cnt, 0.05 * cv2.arcLength(cnt, True), True)
        if len(approx) == 3:  # triangle plein = flèche probable
            M = cv2.moments(cnt)
            if M["m00"] > 0:
                arrows.append((int(M["m10"] / M["m00"]), int(M["m01"] / M["m00"])))
    return arrows


# ============================================================================
#  10. COLORISATION
# ============================================================================
def _color_for(line_id: int) -> Tuple[int, int, int]:
    """Couleur BGR déterministe et bien contrastée pour un id donné."""
    rnd = random.Random(line_id * 9973 + 12345)
    # on évite les couleurs trop claires (fond blanc)
    return (rnd.randint(0, 200), rnd.randint(0, 200), rnd.randint(0, 200))


def colorize_lines(
    img: np.ndarray,
    segments: List[Segment],
    breaks: Sequence[BreakSymbol],
    cfg: Config,
) -> np.ndarray:
    """Dessine chaque ligne (composante connexe) avec une couleur unique."""
    canvas = img.copy()
    for seg in segments:
        if seg.line_id < 0:
            continue
        color = _color_for(seg.line_id)
        cv2.line(canvas, seg.p1, seg.p2, color, cfg.draw_thickness, cv2.LINE_AA)

    # marque chaque rupture par un POINT BLEU (les segments reconnectés gardent
    # la même couleur de ligne : la rupture ne coupe pas la ligne logiquement)
    for br in breaks:
        cv2.circle(canvas, (br.cx, br.cy), cfg.break_dot_radius,
                   cfg.break_dot_color, -1, cv2.LINE_AA)

    # étiquette l'id de ligne près d'un segment représentatif
    seen = set()
    for seg in segments:
        if seg.line_id < 0 or seg.line_id in seen:
            continue
        seen.add(seg.line_id)
        mx, my = seg.midpoint
        cv2.putText(
            canvas, f"L{seg.line_id}", (int(mx), int(my) - 5),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, _color_for(seg.line_id), 1, cv2.LINE_AA,
        )
    return canvas


# ============================================================================
#  BONUS : surlignage d'une ligne choisie (façon highlight jaune du plan)
# ============================================================================
def line_summaries(
    segments: List[Segment], labels: Dict[int, List[str]]
) -> List[dict]:
    """Résumé par ligne : id, longueur cumulée, étendue x, label. Trié par longueur."""
    agg: Dict[int, dict] = {}
    for s in segments:
        if s.line_id < 0:
            continue
        a = agg.setdefault(
            s.line_id, {"id": s.line_id, "length": 0.0, "xmin": 1e9, "xmax": -1e9}
        )
        a["length"] += s.length
        a["xmin"] = min(a["xmin"], s.x1, s.x2)
        a["xmax"] = max(a["xmax"], s.x1, s.x2)
    out = []
    for lid, a in agg.items():
        a["length"] = int(a["length"])
        a["xmin"], a["xmax"] = int(a["xmin"]), int(a["xmax"])
        a["label"] = " ".join(dict.fromkeys(labels.get(lid, [])))
        out.append(a)
    out.sort(key=lambda d: -d["length"])
    return out


def select_line(
    summaries: List[dict], line_id: Optional[int], label: Optional[str]
) -> Optional[int]:
    """Choisit la ligne à surligner : par id explicite, par label, sinon la plus longue."""
    if line_id is not None:
        return line_id
    if label:
        lab = label.lower()
        for s in summaries:
            if lab in s["label"].lower():
                return s["id"]
        return None
    return summaries[0]["id"] if summaries else None


def highlight_line(
    img: np.ndarray, segments: List[Segment], line_id: int, cfg: Config
) -> np.ndarray:
    """Surligne UNE ligne en jaune épais semi-transparent, comme sur un plan annoté."""
    overlay = img.copy()
    for s in segments:
        if s.line_id == line_id:
            cv2.line(
                overlay, s.p1, s.p2, cfg.highlight_color,
                cfg.highlight_thickness, cv2.LINE_AA,
            )
    return cv2.addWeighted(
        overlay, cfg.highlight_alpha, img, 1.0 - cfg.highlight_alpha, 0
    )


# ============================================================================
#  11. SORTIES (JSON, image, SVG)
# ============================================================================
def _build_line_records(
    segments: List[Segment],
    line_map: Dict[int, List[int]],
    labels: Dict[int, List[str]],
    breaks: Sequence[BreakSymbol],
    bridges: Sequence[Tuple[int, int, Tuple[int, int]]],
    cfg: Config,
) -> List[dict]:
    # une ligne a une rupture ISA si un pont, centré sur un *symbole de rupture*
    # (et non un simple composant traversant), implique l'un de ses segments.
    break_centers = [(b.cx, b.cy) for b in breaks]
    lines_with_break = set()
    for ia, ib, center in bridges:
        near_break = any(
            math.hypot(center[0] - bx, center[1] - by) <= cfg.node_snap_tol
            for bx, by in break_centers
        )
        if not near_break:
            continue
        for idx in (ia, ib):
            lid = segments[idx].line_id
            if lid >= 0:
                lines_with_break.add(lid)

    records = []
    for line_id, seg_indices in sorted(line_map.items()):
        segs = [segments[i].as_list() for i in seg_indices]
        text_list = labels.get(line_id, [])
        records.append(
            {
                "id": line_id,
                "label": " ".join(dict.fromkeys(text_list)) if text_list else "",
                "segments": segs,
                "breaks_detected": line_id in lines_with_break,
            }
        )
    return records


def export_results(
    outdir: str,
    annotated: np.ndarray,
    records: List[dict],
    segments: List[Segment],
    cfg: Config,
    image_shape: Tuple[int, int],
) -> None:
    """Écrit l'image annotée, le JSON, et (option) le SVG."""
    os.makedirs(outdir, exist_ok=True)

    # image annotée
    img_path = os.path.join(outdir, "annotated.png")
    cv2.imwrite(img_path, annotated)

    # JSON
    json_path = os.path.join(outdir, "lines.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"lines": records}, f, indent=2, ensure_ascii=False)

    # SVG (bonus)
    if cfg.export_svg:
        _export_svg(os.path.join(outdir, "lines.svg"), segments, image_shape)

    print(f"[OK] {len(records)} ligne(s) exportée(s)")
    print(f"     - image : {img_path}")
    print(f"     - json  : {json_path}")
    if cfg.export_svg:
        print(f"     - svg   : {os.path.join(outdir, 'lines.svg')}")


def _export_svg(path: str, segments: List[Segment], image_shape: Tuple[int, int]) -> None:
    h, w = image_shape[:2]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}">'
    ]
    for seg in segments:
        if seg.line_id < 0:
            continue
        b, g, r = _color_for(seg.line_id)
        parts.append(
            f'<line x1="{seg.x1}" y1="{seg.y1}" x2="{seg.x2}" y2="{seg.y2}" '
            f'stroke="rgb({r},{g},{b})" stroke-width="2" '
            f'data-line-id="{seg.line_id}"/>'
        )
    parts.append("</svg>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


def _save_debug(outdir: str, name: str, img: np.ndarray) -> None:
    dbg = os.path.join(outdir, "debug")
    os.makedirs(dbg, exist_ok=True)
    cv2.imwrite(os.path.join(dbg, name), img)


# ============================================================================
#  ORCHESTRATION
# ============================================================================
def process(
    image_path: str,
    outdir: str,
    cfg: Config,
    highlight: Optional[int] = None,
    highlight_label: Optional[str] = None,
    list_only: bool = False,
) -> List[dict]:
    """Exécute l'ensemble du pipeline et renvoie les enregistrements de lignes."""
    print(f"[1] Chargement       : {image_path}")
    img = load_image(image_path)

    print("[2] Détection contours (Canny)")
    edges = detect_edges(img, cfg)
    if cfg.debug:
        _save_debug(outdir, "01_edges.png", edges)

    print("[3] Détection lignes (Hough)")
    segments = detect_lines(edges, cfg)
    print(f"    -> {len(segments)} segments bruts")

    print("[4] Fusion des segments")
    segments = merge_segments(segments, cfg)
    print(f"    -> {len(segments)} segments fusionnés")

    print("[5] Détection des symboles de rupture")
    breaks = detect_break_symbols(img, cfg)
    print(f"    -> {len(breaks)} rupture(s)")

    print("[5bis] Détection des composants traversants (vannes/nuages/instruments)")
    fittings = detect_fittings(img, cfg)
    print(f"    -> {len(fittings)} composant(s) traversant(s)")

    print("[6] Reconnexion à travers ruptures + composants")
    # ruptures ISA ET composants traversants servent de points de jonction
    junctions = [(b.cx, b.cy) for b in breaks] + fittings
    bridges = reconnect_lines(segments, junctions, cfg)
    print(f"    -> {len(bridges)} pont(s) de reconnexion")

    print("[7] Construction du graphe (composantes = lignes)")
    _, line_map = build_graph(segments, bridges, cfg)
    print(f"    -> {len(line_map)} ligne(s) logique(s)")

    print("[8] OCR")
    texts = run_ocr(img, cfg)
    print(f"    -> {len(texts)} marquage(s) retenu(s)")

    print("[9] Association texte -> ligne")
    labels = associate_text_to_lines(texts, segments, cfg)

    if cfg.detect_arrows:
        arrows = detect_arrows(img, cfg)
        print(f"[bonus] {len(arrows)} flèche(s) détectée(s)")

    # résumé des lignes (utile pour choisir laquelle surligner)
    summaries = line_summaries(segments, labels)
    if list_only or cfg.debug:
        print("    Lignes détectées (id | long.px | x[min..max] | label) :")
        for s in summaries[:20]:
            print(f"      L{s['id']:<3} | {s['length']:>5} | "
                  f"{s['xmin']:>4}..{s['xmax']:<4} | {s['label']}")
    if list_only:
        return _build_line_records(segments, line_map, labels, breaks, bridges, cfg)

    print("[10] Colorisation")
    annotated = colorize_lines(img, segments, breaks, cfg)

    # mode surlignage : une ligne en jaune épais sur le plan d'origine
    if highlight is not None or highlight_label is not None:
        chosen = select_line(summaries, highlight, highlight_label)
        if chosen is None:
            print(f"    [highlight] aucune ligne ne correspond à '{highlight_label}'")
        else:
            hl = highlight_line(img, segments, chosen, cfg)
            os.makedirs(outdir, exist_ok=True)
            hpath = os.path.join(outdir, "highlighted.png")
            cv2.imwrite(hpath, hl)
            print(f"    [highlight] ligne L{chosen} surlignée -> {hpath}")

    print("[11] Export")
    records = _build_line_records(segments, line_map, labels, breaks, bridges, cfg)
    export_results(outdir, annotated, records, segments, cfg, img.shape)
    return records


def build_config_from_args(args: argparse.Namespace) -> Config:
    cfg = Config()
    # 1) preset d'abord (valeurs de base), puis surcharges CLI
    if args.preset:
        apply_preset(cfg, args.preset)
    cfg.debug = args.debug
    cfg.ocr_enabled = not args.no_ocr
    cfg.ocr_engine = args.ocr_engine
    cfg.export_svg = not args.no_svg
    cfg.detect_fittings = not args.no_fittings
    for attr, val in (
        ("canny_low", args.canny_low),
        ("canny_high", args.canny_high),
        ("reconnect_max_dist", args.reconnect_dist),
        ("merge_gap_tol", args.merge_gap),
        ("node_snap_tol", args.node_snap),
        ("reconnect_align_tol", args.reconnect_align),
        ("angle_tol_deg", args.angle_tol),
    ):
        if val is not None:
            setattr(cfg, attr, val)
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Détection et suivi des lignes de tuyauterie sur un P&ID (ISA)."
    )
    parser.add_argument("--input", "-i", required=True, help="Image P&ID en entrée")
    parser.add_argument("--outdir", "-o", default="out", help="Dossier de sortie")
    parser.add_argument("--preset", choices=list(PRESETS), default=None,
                        help="Preset de seuils ('real_plan' pour plans scannés réels)")
    parser.add_argument("--debug", action="store_true", help="Sauve les étapes intermédiaires")
    parser.add_argument("--no-ocr", action="store_true", help="Désactive l'OCR")
    parser.add_argument(
        "--ocr-engine", default="easyocr", choices=["easyocr", "tesseract"],
        help="Moteur OCR",
    )
    parser.add_argument("--no-svg", action="store_true", help="Désactive l'export SVG")
    parser.add_argument("--no-fittings", action="store_true",
                        help="Ne pas traiter vannes/nuages comme traversants")
    # --- surlignage / inspection ---
    parser.add_argument("--highlight", type=int, default=None,
                        help="Surligne la ligne d'id donné (jaune épais)")
    parser.add_argument("--highlight-label", default=None,
                        help="Surligne la ligne dont le label contient ce texte (ex: AZOTE)")
    parser.add_argument("--list-lines", action="store_true",
                        help="Liste les lignes détectées (id, longueur, étendue, label) et sort")
    # --- seuils réglables ---
    parser.add_argument("--canny-low", type=int, default=None)
    parser.add_argument("--canny-high", type=int, default=None)
    parser.add_argument("--reconnect-dist", type=int, default=None,
                        help="Distance max de reconnexion à travers une jonction (px)")
    parser.add_argument("--merge-gap", type=int, default=None,
                        help="Trou max le long de l'axe pour fusionner deux segments (px)")
    parser.add_argument("--node-snap", type=int, default=None,
                        help="Rayon de fusion des extrémités en noeuds/intersections (px)")
    parser.add_argument("--reconnect-align", type=int, default=None,
                        help="Tolérance d'alignement transversal à la reconnexion (px)")
    parser.add_argument("--angle-tol", type=float, default=None,
                        help="Tolérance d'orientation horizontale/verticale (deg)")
    args = parser.parse_args()

    cfg = build_config_from_args(args)
    try:
        process(
            args.input, args.outdir, cfg,
            highlight=args.highlight,
            highlight_label=args.highlight_label,
            list_only=args.list_lines,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"[ERREUR] {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
