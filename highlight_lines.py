#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
highlight_lines.py
==================

**Commande unique** de surlignage des **lignes de tuyauterie** d'un P&ID
vectoriel (PDF), une couleur par **numéro de ligne**, avec segmentation aux
**« Limite de ligne »** (chaque marqueur = changement de numéro de ligne).

Tout est **vectoriel** : les ruptures de ligne et les marquages sont lus
directement dans les données du PDF, et la sortie est un **PDF annoté**.

Pipeline
--------
1. Ruptures de ligne = « Limite de ligne » (via ``detect_line_limits``, par calque).
2. Réseau de tuyaux : segments axis-aligned fusionnés (collinéaires + ponts de
   trous), **coupés** à chaque rupture, puis reliés aux jonctions (T).
3. Marquages : numéro de ligne = nombre à 5 chiffres d'un marquage
   ``DN PRODUIT NUMÉRO CLASSE …`` (ex. ``40 V6 32309 C103 CC N`` → ``32309``).
4. Chaque tronçon prend le numéro du marquage le plus proche (flood-fill sur le
   réseau, **bloqué aux ruptures**).
5. Couleurs : **automatiques** par défaut (palette de 256 couleurs distinctes).
   Un **fichier Excel** ``ligne / couleur`` (hex ``#RRGGBB``) est *optionnel* et
   ne sert qu'à surcharger certaines couleurs.
6. Rendu : **PDF vectoriel** annoté (lignes surlignées + chevrons de rupture).

Usage
-----
    # couleurs automatiques (aucun Excel requis) — une seule commande
    python highlight_lines.py --pdf plan.pdf --outdir out

    # optionnel : surcharger des couleurs via un Excel ligne/couleur
    python highlight_lines.py --pdf plan.pdf --excel couleurs.xlsx --outdir out

Dépendances : pymupdf, openpyxl.
"""
from __future__ import annotations

import argparse
import colorsys
import math
import os
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, List, Tuple

try:
    import fitz
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyMuPDF requis : pip install pymupdf") from exc

from detect_line_limits import LimitConfig, detect_line_limits


@dataclass
class HighlightConfig:
    # Calques CAO des tuyaux à colorier (les instruments, vannes, équipements et
    # le cartouche sont sur d'autres calques et ne sont PAS coloriés).
    pipe_layers: Tuple[str, ...] = ("UTI",)
    pipe_min_len: float = 6.0      # longueur min d'un segment de tuyau (pt)
    merge_gap: float = 35.0        # trou max ponté entre 2 segments collinéaires (pt)
    merge_tol: float = 2.5         # tolérance transversale de colinéarité (pt)
    cut_tol: float = 4.0           # tolérance "point bleu sur le tuyau" (pt)
    junction_tol: float = 5.0      # tolérance de jonction entre tuyaux (pt)
    blue_block: float = 5.0        # rayon de blocage autour d'un point bleu (pt)
    mark_prod: Tuple[str, ...] = ("V6", "C6", "ERR", "ERA", "N2")
    mark_prod_dist: float = 32.0   # distance num<->produit (pt)
    mark_cls_dist: float = 60.0    # distance num<->classe (pt)
    seed_dist: float = 25.0        # distance max marquage<->tuyau pour amorcer (pt)
    pdf_line_width: float = 3.5    # épaisseur trait surligné (PDF, pt)
    pdf_arrow_head: float = 5.0    # taille du chevron '>' (pt)
    pdf_arrow_width: float = 1.5   # épaisseur du chevron (pt)
    arrow_reverse: bool = False    # inverser le sens du chevron
    alpha: float = 0.6
    default_color: str = "#B0B0B0" # couleur de repli (lignes sans numéro résolu)


# ----------------------------------------------------------------------------
#  Réseau de tuyaux
# ----------------------------------------------------------------------------
Pipe = List  # [(x1,y1),(x2,y2),'h'|'v']


def _merge_axis(items, gap, tol):
    """Fusionne des segments (lo,hi,cross) par bande transversale, en pontant gap."""
    by = defaultdict(list)
    for lo, hi, c in items:
        by[round(c / tol)].append((lo, hi, c))
    out = []
    for band in by.values():
        band.sort()
        clo, chi, cc = band[0][0], band[0][1], [band[0][2]]
        for lo, hi, c in band[1:]:
            if lo <= chi + gap:
                chi = max(chi, hi)
                cc.append(c)
            else:
                out.append((clo, chi, sum(cc) / len(cc)))
                clo, chi, cc = lo, hi, [c]
        out.append((clo, chi, sum(cc) / len(cc)))
    return out


def build_pipes(page, cfg: HighlightConfig) -> List[Pipe]:
    """Extrait et fusionne les tuyaux horizontaux/verticaux (non remplis)."""
    H, V = [], []
    for dr in page.get_drawings():
        if dr.get("layer") not in cfg.pipe_layers:
            continue  # ne colorier que les calques de tuyauterie
        if dr.get("fill") is not None and dr.get("type") in ("f", "fs"):
            continue
        for it in dr["items"]:
            if it[0] != "l":
                continue
            x1, y1, x2, y2 = it[1].x, it[1].y, it[2].x, it[2].y
            if math.hypot(x2 - x1, y2 - y1) < cfg.pipe_min_len:
                continue
            ang = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180
            if ang < 8 or ang > 172:
                H.append((min(x1, x2), max(x1, x2), (y1 + y2) / 2))
            elif abs(ang - 90) < 8:
                V.append((min(y1, y2), max(y1, y2), (x1 + x2) / 2))
    pipes: List[Pipe] = []
    for lo, hi, y in _merge_axis(H, cfg.merge_gap, cfg.merge_tol):
        pipes.append([(lo, y), (hi, y), 'h'])
    for lo, hi, x in _merge_axis(V, cfg.merge_gap, cfg.merge_tol):
        pipes.append([(x, lo), (x, hi), 'v'])
    return pipes


def _on_pipe(p: Pipe, pt, tol) -> bool:
    (x1, y1), (x2, y2), o = p
    if o == 'h':
        return abs(pt[1] - y1) < tol and x1 - tol <= pt[0] <= x2 + tol
    return abs(pt[0] - x1) < tol and y1 - tol <= pt[1] <= y2 + tol


def _dist_pt_pipe(pt, p) -> float:
    (x1, y1), (x2, y2), o = p
    if o == 'h':
        cx = min(max(pt[0], x1), x2)
        return math.hypot(pt[0] - cx, pt[1] - y1)
    cy = min(max(pt[1], y1), y2)
    return math.hypot(pt[0] - x1, pt[1] - cy)


def split_pipes(pipes: List[Pipe], blue, cfg: HighlightConfig) -> List[Pipe]:
    """Coupe chaque tuyau aux points bleus posés dessus (changement de ligne)."""
    out: List[Pipe] = []
    for p in pipes:
        (x1, y1), (x2, y2), o = p
        cuts = [pt for pt in blue if _on_pipe(p, pt, cfg.cut_tol)]
        if not cuts:
            out.append(p)
            continue
        if o == 'h':
            xs = sorted([x1] + [c[0] for c in cuts] + [x2])
            for i in range(len(xs) - 1):
                out.append([(xs[i], y1), (xs[i + 1], y1), 'h'])
        else:
            ys = sorted([y1] + [c[1] for c in cuts] + [y2])
            for i in range(len(ys) - 1):
                out.append([(x1, ys[i]), (x1, ys[i + 1]), 'v'])
    return out


def build_adjacency(pipes: List[Pipe], blue, cfg: HighlightConfig):
    """Adjacence entre tuyaux qui se touchent, séparée en :
       - STRONG : jonction normale (même ligne),
       - WEAK   : jonction au niveau d'un point bleu (changement de ligne possible).
    """
    def near_blue(pt):
        return any(math.hypot(pt[0] - b[0], pt[1] - b[1]) < cfg.blue_block for b in blue)

    strong: Dict[int, set] = defaultdict(set)
    weak: Dict[int, set] = defaultdict(set)
    for i, p in enumerate(pipes):
        for e in (tuple(p[0]), tuple(p[1])):
            at_blue = near_blue(e)
            for j, q in enumerate(pipes):
                if j == i:
                    continue
                if _dist_pt_pipe(e, q) < cfg.junction_tol:
                    (weak if at_blue else strong)[i].add(j)
                    (weak if at_blue else strong)[j].add(i)
    return strong, weak


def assign_lines(pipes, strong, weak, markings, cfg: HighlightConfig) -> Dict[int, str]:
    """Segmente en *runs* (tuyaux reliés sans franchir un point bleu) puis :
       - un run portant un marquage prend ce numéro de ligne ;
       - un run sans marquage **hérite** du run voisin à travers un point bleu
         (règle : au point bleu on ne change de ligne que si le tronçon suivant a
         son propre numéro ; sinon on reste sur la ligne courante).
    """
    n = len(pipes)
    run = [-1] * n
    rid = 0
    for i in range(n):
        if run[i] >= 0:
            continue
        run[i] = rid
        dq = deque([i])
        while dq:
            u = dq.popleft()
            for w in strong[u]:
                if run[w] < 0:
                    run[w] = rid
                    dq.append(w)
        rid += 1
    # numéro de chaque run = marquage le plus proche d'un de ses tuyaux
    run_num: Dict[int, str] = {}
    for num, mp in markings:
        bi = min(range(n), key=lambda i: _dist_pt_pipe(mp, pipes[i]))
        if _dist_pt_pipe(mp, pipes[bi]) < cfg.seed_dist and run[bi] not in run_num:
            run_num[run[bi]] = num
    # graphe entre runs via jonctions WEAK (à travers les points bleus)
    run_adj: Dict[int, set] = defaultdict(set)
    for i in weak:
        for j in weak[i]:
            if run[i] != run[j]:
                run_adj[run[i]].add(run[j])
                run_adj[run[j]].add(run[i])
    # héritage : propage les numéros aux runs voisins non numérotés
    dq = deque(run_num.keys())
    while dq:
        r = dq.popleft()
        for nb in run_adj[r]:
            if nb not in run_num:
                run_num[nb] = run_num[r]
                dq.append(nb)
    return {i: run_num[run[i]] for i in range(n) if run[i] in run_num}


# ----------------------------------------------------------------------------
#  Marquages (numéros de ligne)
# ----------------------------------------------------------------------------
def extract_line_markings(page, cfg: HighlightConfig) -> List[Tuple[str, Tuple[float, float]]]:
    """Numéro de ligne = nombre 5 chiffres entouré d'un produit et d'une classe."""
    words = [(w[4], (w[0] + w[2]) / 2, (w[1] + w[3]) / 2) for w in page.get_text("words")]
    prod = [(x, y) for t, x, y in words if t in cfg.mark_prod]
    cls = [(x, y) for t, x, y in words if re.fullmatch(r"C10\d", t)]
    out = []
    for t, x, y in words:
        if not re.fullmatch(r"\d{5}", t) or t.startswith("71"):
            continue  # 71xxx = équipement/instrument, pas une ligne
        if any(math.hypot(x - px, y - py) < cfg.mark_prod_dist for px, py in prod) and \
           any(math.hypot(x - cx, y - cy) < cfg.mark_cls_dist for cx, cy in cls):
            out.append((t, (x, y)))
    return out


# ----------------------------------------------------------------------------
#  Couleurs : palette automatique (256) + surcharge Excel optionnelle
# ----------------------------------------------------------------------------
def auto_palette(n: int = 256) -> List[str]:
    """Génère ``n`` couleurs hex ``#RRGGBB`` distinctes et déterministes.

    Les teintes sont réparties par le **nombre d'or** (espacement maximal) ;
    saturation et valeur alternent par cycle pour rester séparables même avec
    de nombreuses lignes. 256 couleurs couvrent largement un P&ID typique.
    """
    golden = 0.6180339887498949
    sats = (0.85, 0.65, 1.00, 0.75)
    vals = (0.95, 0.80, 0.70, 1.00)
    out: List[str] = []
    h = 0.13
    for i in range(n):
        h = (h + golden) % 1.0
        s = sats[i % len(sats)]
        v = vals[(i // len(sats)) % len(vals)]
        r, g, b = colorsys.hsv_to_rgb(h, s, v)
        out.append(f"#{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}")
    return out


def resolve_colors(detected: List[str], excel_colors: Dict[str, str]) -> Dict[str, str]:
    """Couleur par numéro de ligne : palette auto par défaut, Excel prioritaire.

    L'Excel est **optionnel** : toute ligne sans couleur fournie reçoit une
    couleur de la palette automatique (256 couleurs).
    """
    palette = auto_palette(256)
    colors: Dict[str, str] = {}
    for idx, num in enumerate(sorted(set(detected))):
        override = excel_colors.get(num, "")
        colors[num] = override or palette[idx % len(palette)]
    return colors


def write_template(path: str, colors: Dict[str, str]) -> None:
    """Écrit un Excel ``ligne / couleur`` pré-rempli avec les couleurs résolues,
    que l'utilisateur peut éditer pour surcharger la palette automatique."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "lignes"
    ws.append(["ligne", "couleur"])
    for n in sorted(colors):
        ws.append([n, colors[n]])  # hex #RRGGBB, éditable
    wb.save(path)


def read_colors(path: str) -> Dict[str, str]:
    import openpyxl
    wb = openpyxl.load_workbook(path)
    ws = wb.active
    colors: Dict[str, str] = {}
    rows = list(ws.iter_rows(values_only=True))
    for row in rows[1:]:  # saute l'entête
        if not row or row[0] is None:
            continue
        line = str(row[0]).strip()
        color = str(row[1]).strip() if len(row) > 1 and row[1] else ""
        if color:
            colors[line] = color
    return colors


# ----------------------------------------------------------------------------
#  Rendu
# ----------------------------------------------------------------------------
def _hex_to_rgb01(h: str) -> Tuple[float, float, float]:
    h = h.lstrip("#")
    if len(h) != 6:
        return (0.69, 0.69, 0.69)
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255)


def _lb_dir(r, pipes, reverse):
    """Direction du chevron = **sens de l'apex du triangle** (r['dir']).
    Repli sur l'orientation du tuyau si l'apex n'a pas pu être calculé."""
    v = r.get("dir")
    if v is None:
        # repli : le long du tuyau le plus proche
        if pipes:
            q = min(pipes, key=lambda p: _dist_pt_pipe(r["point"], p))
            v = (1.0, 0.0) if q[2] == 'h' else (0.0, 1.0)
        else:
            v = (1.0, 0.0)
    return (-v[0], -v[1]) if reverse else v


def _chevron_strokes(point, v, head):
    """Renvoie les 2 traits du chevron '>' (sans hampe), pointe sur `point`,
    aligné sur la direction `v`."""
    vx, vy = v
    px, py = -vy, vx  # perpendiculaire
    tip = (point[0] + vx * head, point[1] + vy * head)
    b1 = (tip[0] - vx * head + px * head, tip[1] - vy * head + py * head)
    b2 = (tip[0] - vx * head - px * head, tip[1] - vy * head - py * head)
    return tip, b1, b2


def annotate_pdf(doc, page, pipes, label, colors, lb, cfg: HighlightConfig,
                 out_path: str) -> None:
    """Dessine les surlignages (vectoriels) sur la page et sauve un PDF annoté.

    Les coordonnées des tuyaux sont en espace MediaBox (non roté), ce que les
    méthodes de dessin PyMuPDF utilisent directement ; la rotation de la page
    est conservée pour l'affichage. Chaque line break est marqué par une
    **flèche bleue** orientée vers la conduite (sens de connexion).
    """
    default = _hex_to_rgb01(cfg.default_color)
    # une forme par couleur (regroupe les traits) pour un PDF compact
    by_color: Dict[Tuple[float, float, float], List[Pipe]] = defaultdict(list)
    for i, p in enumerate(pipes):
        if i not in label:
            continue
        rgb = _hex_to_rgb01(colors[label[i]]) if label[i] in colors else default
        by_color[rgb].append(p)
    for rgb, plist in by_color.items():
        shape = page.new_shape()
        for p in plist:
            shape.draw_line(fitz.Point(*p[0]), fitz.Point(*p[1]))
        # trait épais translucide = effet surligneur, le tuyau noir reste lisible
        shape.finish(color=rgb, width=cfg.pdf_line_width,
                     stroke_opacity=cfg.alpha, lineCap=1)
        shape.commit()
    # chevrons bleus '>' aux line breaks : colinéaires au tuyau, sens rupture
    shape = page.new_shape()
    for r in lb:
        v = _lb_dir(r, pipes, cfg.arrow_reverse)
        tip, b1, b2 = _chevron_strokes(r["point"], v, cfg.pdf_arrow_head)
        shape.draw_line(fitz.Point(*b1), fitz.Point(*tip))
        shape.draw_line(fitz.Point(*tip), fitz.Point(*b2))
    shape.finish(color=(0, 0, 1), width=cfg.pdf_arrow_width, lineCap=1)
    shape.commit()
    doc.save(out_path, garbage=3, deflate=True)


# ----------------------------------------------------------------------------
def process(pdf, page_no, excel, outdir, cfg: HighlightConfig):
    os.makedirs(outdir, exist_ok=True)
    doc = fitz.open(pdf)
    page = doc[page_no]
    orig_rot = page.rotation  # conserver l'orientation d'affichage d'origine

    # 1) ruptures de ligne (symbole + point sur la conduite)
    lb = detect_line_limits(page, LimitConfig())
    blue = [r["point"] for r in lb]
    page.set_rotation(0)
    print(f"[1] {len(lb)} « Limite de ligne »")

    # 2-3) réseau + marquages
    pipes = build_pipes(page, cfg)
    pipes = split_pipes(pipes, blue, cfg)
    strong, weak = build_adjacency(pipes, blue, cfg)
    markings = extract_line_markings(page, cfg)
    print(f"[2] {len(pipes)} tuyaux (calques {cfg.pipe_layers}) | {len(markings)} marquages")

    # 4) attribution des numéros (runs + héritage)
    label = assign_lines(pipes, strong, weak, markings, cfg)
    detected = sorted(set(label.values()))
    print(f"[3] {len(label)}/{len(pipes)} tuyaux étiquetés | {len(detected)} lignes : {detected}")

    # 5) couleurs : palette automatique (256), Excel optionnel en surcharge
    excel_colors = read_colors(excel) if excel and os.path.isfile(excel) else {}
    colors = resolve_colors(detected, excel_colors)
    if excel_colors:
        print(f"[4] {len(colors)} couleur(s) (dont {len(excel_colors)} surchargée(s) via {excel})")
    else:
        print(f"[4] {len(colors)} couleur(s) générée(s) automatiquement (palette 256)")
        if excel:  # template éditable pré-rempli pour personnaliser ensuite
            write_template(excel, colors)
            print(f"    template éditable écrit : {excel}")

    # 6) sortie PDF annoté (vectoriel) — on restaure l'orientation d'affichage
    page.set_rotation(orig_rot)
    out_pdf = os.path.join(outdir, "highlight.pdf")
    annotate_pdf(doc, page, pipes, label, colors, lb, cfg, out_pdf)
    print(f"[5] PDF annoté : {out_pdf}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Surlignage vectoriel des lignes P&ID (couleurs auto, Excel optionnel)."
    )
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--page", type=int, default=0)
    ap.add_argument("--excel", default=None,
                    help="Excel ligne/couleur (OPTIONNEL) pour surcharger la palette auto ; "
                         "si le chemin n'existe pas, un template éditable y est écrit")
    ap.add_argument("--outdir", default="out")
    ap.add_argument("--pipe-layers", default="UTI",
                    help="calques de tuyauterie à colorier, séparés par des virgules "
                         "(défaut: UTI ; ex: UTI,0 pour plus de couverture)")
    args = ap.parse_args()
    cfg = HighlightConfig(pipe_layers=tuple(s.strip() for s in args.pipe_layers.split(",") if s.strip()))
    process(args.pdf, args.page, args.excel, args.outdir, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
