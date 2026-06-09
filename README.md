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

**Composants traversants.** Les vannes (noeud papillon), nuages de calorifuge et
bulles d'instrument coupent *visuellement* la ligne sans la couper logiquement.
Ils sont détectés (`detect_fittings`) et utilisés comme points de jonction
supplémentaires pour la reconnexion : la conduite reste **une seule ligne** d'un
bout à l'autre, même quand elle traverse une vanne. La reconnexion ne relie que
des segments réellement colinéaires de part et d'autre, ce qui évite les faux
ponts (un blob de texte isolé ne fusionne rien).

## Installation

```bash
pip install -r requirements.txt
# EasyOCR est le moteur par défaut. Pour Tesseract : installer le binaire système
# (apt install tesseract-ocr) puis: pip install pytesseract
```

## Utilisation

```bash
# plan synthétique / propre
python pid_detector.py --input plan.png --outdir out --debug

# plan scanné réel : utiliser le preset calibré
python pid_detector.py --input plan.jpeg --outdir out --preset real_plan --no-ocr

# lister les lignes détectées (id, longueur, étendue, label) pour en choisir une
python pid_detector.py --input plan.jpeg --preset real_plan --list-lines

# surligner UNE ligne en jaune épais (par id ou par label OCR)
python pid_detector.py --input plan.jpeg --preset real_plan --highlight 2
python pid_detector.py --input plan.jpeg --preset real_plan --highlight-label AZOTE
```

Options principales :

| Option                | Effet                                                            |
|-----------------------|-----------------------------------------------------------------|
| `--input/-i`          | Image P&ID en entrée (obligatoire)                             |
| `--outdir/-o`         | Dossier de sortie (`out` par défaut)                          |
| `--preset`            | `default` ou `real_plan` (seuils calibrés plans scannés)      |
| `--highlight <id>`    | Surligne la ligne d'id donné → `out/highlighted.png`         |
| `--highlight-label X` | Surligne la ligne dont le label OCR contient `X` (ex. AZOTE)  |
| `--list-lines`        | Liste les lignes (id, longueur, étendue, label) puis sort      |
| `--debug`             | Sauvegarde les étapes intermédiaires dans `out/debug/`        |
| `--no-ocr`            | Désactive l'OCR (utile sans EasyOCR/Tesseract)               |
| `--no-fittings`       | Ne traite pas vannes/nuages comme traversants                 |
| `--ocr-engine`        | `easyocr` (défaut) ou `tesseract`                            |
| `--no-svg`            | Désactive l'export SVG                                         |
| `--canny-low/-high`   | Seuils Canny                                                   |
| `--reconnect-dist`    | Distance max de reconnexion à travers une jonction (px)       |
| `--merge-gap`         | Trou max le long de l'axe pour fusionner deux segments (px)   |
| `--node-snap`         | Rayon de fusion des extrémités en intersections (px)          |
| `--reconnect-align`   | Tolérance d'alignement transversal à la reconnexion (px)      |
| `--angle-tol`         | Tolérance d'orientation horizontale/verticale (deg)           |

Tous les seuils sont centralisés et réglables dans la dataclass `Config`
(en haut de `pid_detector.py`). Le preset **`real_plan`** applique :
`merge_gap_tol=160`, `reconnect_max_dist=250`, `node_snap_tol=40`,
`reconnect_align_tol=18`, `angle_tol_deg=6`, `ocr_upscale=3.0` — valeurs
calibrées sur un plan scanné réel où la conduite principale traverse vannes,
nuages et ruptures. L'**upscale OCR** (×3) est essentiel : sur un scan basse
résolution, le texte des marquages est trop petit à l'échelle native ; on
agrandit avant lecture puis on reprojette les coordonnées. Validé sur plan réel :
le produit `AZOTE` est lu et associé à la bonne ligne, et
`--highlight-label AZOTE` la surligne automatiquement.

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
- `out/highlighted.png` — (si `--highlight`) une ligne surlignée en jaune épais
  semi-transparent sur le plan d'origine, à la manière d'un repérage manuel.

## Marquages ISA reconnus

Format typique le long des lignes : `DN PRODUIT NUM CLASSE CALO TRACEUR`
(ex. `50x2.4 316L DNxx AZOTE`). Le filtrage OCR par regex est configurable via
`Config.ocr_keep_patterns` (DN…, diamètre `AxB`, matériaux `316L/304/INOX/CS`,
produits `AZOTE/AIR/EAU/VAPEUR`…).

## Bonus

- **Mode surlignage** : suivi et repérage d'une ligne en jaune épais (`--highlight`).
- Détection du **sens** via les flèches (triangles pleins).
- Export **SVG**.
- **Mode debug** (étapes intermédiaires + liste des lignes).
- Composants **traversants** auto (vannes/nuages/instruments).

## Limites & réglages

- Lignes très fines → double détection de contour : ajuster `node_snap_tol` et
  `merge_gap_tol` pour limiter la fragmentation.
- Symboles de rupture variés selon les chartes : adapter `break_*` (aire,
  nombre de sommets, ratio) et/ou passer en *template matching*.
