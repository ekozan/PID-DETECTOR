# PID-DETECTOR

Surlignage des **lignes de tuyauterie** sur un plan **P&ID** (norme ISA), une
**couleur par numéro de ligne**. Tout est **vectoriel** : les **ruptures de
ligne** (« Limite de ligne ») et les **marquages** sont lus directement dans les
données vectorielles du PDF, et la sortie est un **PDF annoté**.

Le tout en **une seule commande**, et **sans Excel obligatoire** : les couleurs
sont générées automatiquement (palette de **256 couleurs** distinctes). Un
fichier Excel reste possible, uniquement pour *surcharger* certaines couleurs.

## Pipeline

```
detect_line_limits (ruptures, par calque CAO)
   → build_pipes → split_pipes (coupe aux ruptures) → build_adjacency
   → extract_line_markings → assign_lines (runs + héritage)
   → resolve_colors (palette 256 auto, Excel optionnel) → annotate_pdf
```

**Idée clé.** Chaque **rupture de ligne** marque un changement de numéro de
ligne : le réseau de tuyaux est coupé à ces points, segmenté en *runs*, puis
chaque run reçoit le **numéro du marquage** le plus proche (``DN PRODUIT NUMÉRO
CLASSE …``, ex. ``40 V6 32309 C103 CC N`` → ``32309``). Les runs sans marquage
**héritent** du voisin à travers la rupture. Une couleur est ensuite attribuée
par numéro de ligne.

**Détection par calque.** Les symboles « Limite de ligne », les flèches de sens,
la pente et le calorifuge sont tous de petits triangles creux quasi identiques :
aucune heuristique géométrique ne les sépare de façon fiable. On les détecte donc
**par calque CAO** (non ambigu), directement dans le vectoriel.

## Installation

```bash
pip install -r requirements.txt   # pymupdf + openpyxl
```

## Utilisation

**Une seule commande.** Couleurs automatiques, aucun Excel requis :

```bash
python highlight_lines.py --pdf plan.pdf --outdir out
```

Optionnel — surcharger certaines couleurs via un Excel `ligne / couleur` :

```bash
python highlight_lines.py --pdf plan.pdf --excel couleurs.xlsx --outdir out
```

Si le chemin `--excel` n'existe pas encore, un **template éditable** pré-rempli
avec les couleurs automatiques y est écrit (et le PDF est quand même produit) :
il suffit d'ajuster la colonne `couleur` (hex `#RRGGBB`) puis de relancer.

### Options

| Option           | Effet                                                                 |
|------------------|-----------------------------------------------------------------------|
| `--pdf`          | P&ID vectoriel en entrée (obligatoire)                              |
| `--page`         | Index de page (0 par défaut)                                         |
| `--outdir`       | Dossier de sortie (`out` par défaut)                                |
| `--excel`        | Excel `ligne/couleur` **optionnel** (surcharge la palette auto)      |
| `--pipe-layers`  | Calques de tuyauterie à colorier (défaut `UTI` ; ex. `UTI,0`)       |

Tous les seuils (fusion, jonctions, distances marquage↔tuyau, rendu PDF) sont
centralisés dans la dataclass `HighlightConfig` en haut de `highlight_lines.py`.

## Couleurs

- **Automatique (défaut).** `auto_palette(256)` génère 256 couleurs hex
  distinctes et déterministes (teintes réparties par le nombre d'or, saturation
  et valeur alternées). Chaque numéro de ligne détecté reçoit une couleur stable.
- **Surcharge Excel (optionnel).** Un fichier `ligne / couleur` (couleur en hex
  `#RRGGBB`) ne sert qu'à remplacer la couleur auto pour les lignes listées ;
  les autres restent en palette automatique.

## Sortie

- `out/highlight.pdf` — **PDF vectoriel** annoté : chaque ligne surlignée de sa
  couleur (trait épais translucide, le tuyau d'origine reste lisible) et un
  **chevron `>`** à chaque rupture de ligne, orienté selon le sens du symbole.

## Marquages ISA reconnus

Numéro de ligne = nombre à 5 chiffres d'un marquage `DN PRODUIT NUMÉRO CLASSE …`,
entouré d'un produit (`V6`, `C6`, `N2`…) et d'une classe (`C10x`). Réglable via
`HighlightConfig` (`mark_prod`, distances `mark_prod_dist` / `mark_cls_dist`).

## Limites & réglages

- Couverture des tuyaux : ajuster `--pipe-layers` selon les calques CAO du plan.
- Segmentation : `merge_gap` / `merge_tol` (fusion), `junction_tol` (jonctions T),
  `blue_block` (rayon de coupure à une rupture).
- Détection des ruptures : calque dédié réglable dans `LimitConfig`
  (`symbol_layer`, défaut `14`).
