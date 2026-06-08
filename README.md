# PID-DETECTOR

Détection et suivi des **lignes de tuyauterie** sur un plan **P&ID** (norme ISA) :
détection des lignes, lecture des marquages ISA (OCR), gestion des **symboles de
rupture** (la ligne reste logiquement continue), et colorisation cohérente.

## Pipeline

```
load_image → detect_edges → detect_lines → merge_segments
   → detect_break_symbols → reconnect_lines → build_graph
   → run_ocr → associate_text_to_lines → colorize_lines → export_results
```

**Idée clé.** Chaque segment fusionné est une *arête* d'un graphe `networkx` ;
les extrémités proches sont fusionnées en *noeuds* (intersections). Une **ligne
logique = composante connexe**. Le **symbole de rupture ISA** (triangle ouvert /
« Y » inversé) ne coupe pas la ligne : il est détecté puis transformé en *arête
de reconnexion* — les deux côtés gardent donc le **même ID et la même couleur**.

## Installation

```bash
pip install -r requirements.txt
# EasyOCR est le moteur par défaut. Pour Tesseract : installer le binaire système
# (apt install tesseract-ocr) puis: pip install pytesseract
```

## Utilisation

```bash
python pid_detector.py --input plan.png --outdir out --debug
```

Options principales :

| Option              | Effet                                                        |
|---------------------|-------------------------------------------------------------|
| `--input/-i`        | Image P&ID en entrée (obligatoire)                          |
| `--outdir/-o`       | Dossier de sortie (`out` par défaut)                        |
| `--debug`           | Sauvegarde les étapes intermédiaires dans `out/debug/`     |
| `--no-ocr`          | Désactive l'OCR (utile sans EasyOCR/Tesseract)             |
| `--ocr-engine`      | `easyocr` (défaut) ou `tesseract`                          |
| `--no-svg`          | Désactive l'export SVG                                      |
| `--canny-low/-high` | Seuils Canny                                                |
| `--reconnect-dist`  | Distance max de reconnexion à travers une rupture (px)     |

Tous les seuils sont centralisés et réglables dans la dataclass `Config`
(en haut de `pid_detector.py`).

## Sorties

- `out/annotated.png` — image avec chaque ligne d'une couleur unique, ruptures
  marquées en rouge, étiquettes `L<id>`.
- `out/lines.json` :

```json
{
  "lines": [
    {
      "id": 2,
      "label": "AZOTE DN50 316L",
      "segments": [[205,150,285,150], [369,150,639,150]],
      "breaks_detected": true
    }
  ]
}
```

- `out/lines.svg` — export vectoriel des lignes (bonus).

## Marquages ISA reconnus

Format typique le long des lignes : `DN PRODUIT NUM CLASSE CALO TRACEUR`
(ex. `50x2.4 316L DNxx AZOTE`). Le filtrage OCR par regex est configurable via
`Config.ocr_keep_patterns` (DN…, diamètre `AxB`, matériaux `316L/304/INOX/CS`,
produits `AZOTE/AIR/EAU/VAPEUR`…).

## Bonus

- Détection du **sens** via les flèches (triangles pleins).
- Export **SVG**.
- **Mode debug** (étapes intermédiaires).

## Limites & réglages

- Lignes très fines → double détection de contour : ajuster `node_snap_tol` et
  `merge_gap_tol` pour limiter la fragmentation.
- Symboles de rupture variés selon les chartes : adapter `break_*` (aire,
  nombre de sommets, ratio) et/ou passer en *template matching*.
