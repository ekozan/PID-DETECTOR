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

## Interface graphique

Pour traiter un **dossier de P&ID** sans passer par la ligne de commande :

```bash
python gui.py            # ou : python gui.py /chemin/vers/le/dossier
```

1. **Sélectionner un dossier** contenant des plans PDF.
2. **Générer le rendu** : chaque plan est surligné et écrit en
   `<dossier>/out/<nom_du_plan>/highlight.pdf` (sortie **vectorielle**).
3. **Aperçu** : cliquer un plan dans la liste affiche son rendu à l'écran.

L'Excel de couleurs (`option`) et les calques de tuyaux restent réglables depuis
la fenêtre. L'aperçu est une simple rasterisation du PDF produit ; le fichier de
sortie, lui, reste vectoriel. Dépend de Tkinter (paquet système `python3-tk`).

## Utilisation (ligne de commande)

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
| `--mark-context` | Indices requis autour d'un numéro : `both` / `any` (défaut) / `none` |
| `--number-re`    | Regex du numéro de ligne (défaut : 5 chiffres isolés)               |
| `--seed-dist`    | Distance max marquage↔tuyau pour amorcer une ligne (pt)            |
| `--break-style`  | Marqueur de rupture : `chevron` (sens), `tick` (sans sens), `dot`   |
| `--reverse-arrows` | Inverse le sens des chevrons de rupture                           |
| `--diagnose`     | Journalise le texte près des tuyaux **et** le placement des ruptures |

Tous les seuils (fusion, jonctions, distances marquage↔tuyau, rendu PDF) sont
centralisés dans la dataclass `HighlightConfig` en haut de `highlight_lines.py`.

### Lignes non détectées (numéros manquants)

Une ligne n'est coloriée que si son **numéro** est reconnu près d'un tuyau. Si
des lignes restent grises :

1. **Diagnostic** — voir le texte réellement présent près des tuyaux :
   ```bash
   python highlight_lines.py --pdf plan.pdf --diagnose
   ```
   La liste montre chaque jeton numérique, sa distance au tuyau, et s'il a été
   retenu (`✓`). On y lit le **format réel** des numéros.
2. **Contexte trop strict** — par défaut un numéro est validé s'il a un produit
   **ou** une classe à proximité (`any`). Pour n'exiger aucun contexte :
   ```bash
   python highlight_lines.py --pdf plan.pdf --mark-context none
   ```
3. **Format différent** — si les numéros ne sont pas des nombres à 5 chiffres,
   ajuster la regex, ex. 6 chiffres :
   ```bash
   python highlight_lines.py --pdf plan.pdf --number-re "(?<!\d)\d{6}(?!\d)"
   ```
4. **Marquage trop loin du tuyau** — augmenter `--seed-dist` (défaut 30 pt).

Dans l'interface graphique, le sélecteur **Contexte numéro** et la case
**Diagnostic** font la même chose (le journal affiche le diagnostic).

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
  **marqueur** à chaque rupture de ligne.

### Ruptures de ligne : localisation & sens

Le point de rupture est **projeté sur la conduite** : on prend l'extrémité du
symbole la plus proche d'un tuyau et on la projette dessus (le point tombe donc
toujours exactement sur la conduite, sans dépendre d'un seuil serré). Le marqueur
est **toujours colinéaire au tuyau** (jamais en biais) ; seul le *sens* vient de
l'apex du triangle.

Si le sens est inversé sur certains plans :

```bash
python highlight_lines.py --pdf plan.pdf --reverse-arrows   # inverse les chevrons
python highlight_lines.py --pdf plan.pdf --break-style tick  # marqueur sans sens
```

`--break-style tick` (ou `dot`) supprime toute notion de direction si seule la
**position** importe. `--diagnose` affiche, pour chaque rupture, le centre du
symbole, le point posé, le décalage et le sens retenu.

## Marquages ISA reconnus

Format attendu : **`DN PRODUIT NUMÉRO(5 chiffres) CLASSE`** (ex. `40 V6 32309 C103`).
Le **numéro** (défaut : 5 chiffres isolés) est validé par son **contexte** : un
produit et/ou une classe à proximité. Le produit est reconnu par liste
(`mark_prod`) **ou** par motif générique (`mark_prod_re`, codes ISA courts type
`V6`, `N2`, `ERR`…), donc un produit hors liste reste rattrapé ; la **classe**
(`mark_class_re`) sert d'ancre fiable. Le jeton est extrait par recherche, donc
un numéro **collé** (ex. `32309C103`) est aussi rattrapé. Tout est réglable via
`HighlightConfig` (`mark_number_re`, `mark_class_re`, `mark_prod`,
`mark_prod_re`, `mark_context`, distances `mark_prod_dist` / `mark_cls_dist`).

## Limites & réglages

- Couverture des tuyaux : ajuster `--pipe-layers` selon les calques CAO du plan.
- Segmentation : `merge_gap` / `merge_tol` (fusion), `junction_tol` (jonctions T),
  `blue_block` (rayon de coupure à une rupture).
- Détection des ruptures : calque dédié réglable dans `LimitConfig`
  (`symbol_layer`, défaut `14`).
