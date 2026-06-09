#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui.py
======

Interface graphique pour PID-DETECTOR : sélection d'un **dossier de P&ID (PDF)**,
**génération** du surlignage vectoriel (une couleur par numéro de ligne) et
**rendu** à l'écran.

La sortie reste **vectorielle** : pour chaque plan, un ``highlight.pdf`` est écrit
dans ``<dossier>/out/<nom_du_plan>/``. L'aperçu affiché n'est qu'une
rasterisation de ce PDF (on ne peut pas peindre du vectoriel sur un canvas) ;
le fichier produit, lui, reste un PDF vectoriel.

Aucune dépendance supplémentaire : Tkinter (stdlib) + PyMuPDF (déjà requis).

Usage :
    python gui.py [dossier]
"""
from __future__ import annotations

import base64
import os
import queue
import sys
import threading
import traceback
from contextlib import redirect_stdout
from io import StringIO

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except ImportError as exc:  # pragma: no cover - environnement sans Tk
    raise SystemExit(
        "Tkinter est requis pour l'interface (paquet système python3-tk)."
    ) from exc

try:
    import fitz  # PyMuPDF
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyMuPDF requis : pip install pymupdf") from exc

from highlight_lines import HighlightConfig, process


def list_pdfs(folder: str) -> list:
    """Liste triée des PDF d'un dossier (P&ID à traiter)."""
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder) if f.lower().endswith(".pdf"))


class App:
    """Fenêtre principale : dossier → génération (thread) → aperçu vectoriel."""

    def __init__(self, root: tk.Tk, initial_folder: str = "") -> None:
        self.root = root
        self.root.title("PID-DETECTOR — surlignage des lignes")
        self.root.geometry("1100x720")

        self.outputs: dict = {}            # nom du plan -> chemin highlight.pdf
        self.q: queue.Queue = queue.Queue()
        self._imgref = None               # garde une réf à la PhotoImage (anti-GC)

        self._build_widgets()
        if initial_folder:
            self.folder_var.set(initial_folder)
            self._scan_folder()
        self.root.after(100, self._poll)

    # ----- construction de l'UI -------------------------------------------
    def _build_widgets(self) -> None:
        bar = ttk.Frame(self.root, padding=8)
        bar.pack(side="top", fill="x")

        self.folder_var = tk.StringVar()
        self.excel_var = tk.StringVar()
        self.layers_var = tk.StringVar(value="UTI")
        self.context_var = tk.StringVar(value="any")
        self.diagnose_var = tk.BooleanVar(value=False)

        ttk.Label(bar, text="Dossier P&ID :").grid(row=0, column=0, sticky="w")
        ttk.Entry(bar, textvariable=self.folder_var, width=58).grid(row=0, column=1, padx=4)
        ttk.Button(bar, text="Parcourir…", command=self._pick_folder).grid(row=0, column=2)

        ttk.Label(bar, text="Excel couleurs (option) :").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(bar, textvariable=self.excel_var, width=58).grid(row=1, column=1, padx=4, pady=(4, 0))
        ttk.Button(bar, text="Parcourir…", command=self._pick_excel).grid(row=1, column=2, pady=(4, 0))

        ttk.Label(bar, text="Calques tuyaux :").grid(row=2, column=0, sticky="w", pady=(4, 0))
        opt = ttk.Frame(bar)
        opt.grid(row=2, column=1, sticky="w", padx=4, pady=(4, 0))
        ttk.Entry(opt, textvariable=self.layers_var, width=12).pack(side="left")
        ttk.Label(opt, text="  Contexte numéro :").pack(side="left")
        ttk.Combobox(opt, textvariable=self.context_var, width=6, state="readonly",
                     values=("both", "any", "none")).pack(side="left")
        ttk.Checkbutton(opt, text="Diagnostic", variable=self.diagnose_var).pack(side="left", padx=(8, 0))
        self.gen_btn = ttk.Button(bar, text="Générer le rendu", command=self._generate)
        self.gen_btn.grid(row=2, column=2, pady=(4, 0))

        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.pack(side="top", fill="both", expand=True, padx=8, pady=4)

        left = ttk.Frame(body)
        ttk.Label(left, text="Plans détectés").pack(anchor="w")
        self.listbox = tk.Listbox(left, width=34, exportselection=False)
        self.listbox.pack(side="left", fill="y", expand=False)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.listbox.yview)
        sb.pack(side="left", fill="y")
        self.listbox.config(yscrollcommand=sb.set)
        self.listbox.bind("<<ListboxSelect>>", self._on_select)
        body.add(left, weight=0)

        right = ttk.Frame(body)
        self.canvas = tk.Canvas(right, bg="#202020", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        body.add(right, weight=1)

        bottom = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        bottom.pack(side="bottom", fill="both")
        self.status = tk.StringVar(value="Sélectionnez un dossier contenant des P&ID PDF.")
        ttk.Label(bottom, textvariable=self.status).pack(anchor="w")
        self.log = tk.Text(bottom, height=7, wrap="word")
        self.log.pack(fill="x")
        self.log.configure(state="disabled")

    # ----- actions utilisateur --------------------------------------------
    def _pick_folder(self) -> None:
        folder = filedialog.askdirectory(title="Dossier contenant les P&ID (PDF)")
        if folder:
            self.folder_var.set(folder)
            self._scan_folder()

    def _pick_excel(self) -> None:
        path = filedialog.askopenfilename(
            title="Excel ligne/couleur (optionnel)",
            filetypes=[("Excel", "*.xlsx"), ("Tous", "*.*")],
        )
        if path:
            self.excel_var.set(path)

    def _scan_folder(self) -> None:
        """Remplit la liste avec les PDF trouvés (avant toute génération)."""
        folder = self.folder_var.get().strip()
        files = list_pdfs(folder)
        self.outputs.clear()
        self.listbox.delete(0, "end")
        for name in files:
            self.listbox.insert("end", name)
        self.canvas.delete("all")
        self._imgref = None
        if files:
            self.status.set(f"{len(files)} plan(s) trouvé(s). Cliquez « Générer le rendu ».")
        else:
            self.status.set("Aucun PDF dans ce dossier.")

    def _generate(self) -> None:
        folder = self.folder_var.get().strip()
        if not os.path.isdir(folder):
            messagebox.showerror("Dossier", "Sélectionnez un dossier valide.")
            return
        files = list_pdfs(folder)
        if not files:
            messagebox.showwarning("Dossier", "Aucun PDF trouvé dans ce dossier.")
            return
        excel = self.excel_var.get().strip() or None
        layers = tuple(s.strip() for s in self.layers_var.get().split(",") if s.strip()) or ("UTI",)
        context = self.context_var.get()
        diagnose = bool(self.diagnose_var.get())

        self.gen_btn.config(state="disabled")
        self.outputs.clear()
        self._log_clear()
        self.status.set(f"Génération de {len(files)} plan(s)…")
        threading.Thread(
            target=self._work, args=(folder, files, excel, layers, context, diagnose), daemon=True
        ).start()

    # ----- traitement en arrière-plan -------------------------------------
    def _work(self, folder, files, excel, layers, context, diagnose) -> None:
        """Thread worker : traite chaque PDF, poste les résultats dans la queue."""
        cfg = HighlightConfig(pipe_layers=layers, mark_context=context, diagnose=diagnose)
        for name in files:
            pdf = os.path.join(folder, name)
            stem = os.path.splitext(name)[0]
            outdir = os.path.join(folder, "out", stem)
            buf = StringIO()
            try:
                with redirect_stdout(buf):
                    out_pdf = process(pdf, 0, excel, outdir, cfg)
                self.q.put(("done", name, out_pdf, buf.getvalue()))
            except Exception:
                self.q.put(("error", name, None, buf.getvalue() + "\n" + traceback.format_exc()))
        self.q.put(("all_done", None, None, None))

    def _poll(self) -> None:
        """Vide la queue côté thread principal (Tkinter n'est pas thread-safe)."""
        try:
            while True:
                kind, name, path, log = self.q.get_nowait()
                if kind == "done":
                    self.outputs[name] = path
                    self._log(f"✓ {name}\n{log.strip()}\n")
                    if len(self.outputs) == 1:  # affiche le premier dès qu'il est prêt
                        self._select_name(name)
                elif kind == "error":
                    self._log(f"✗ {name} — échec\n{log.strip()}\n")
                elif kind == "all_done":
                    self.gen_btn.config(state="normal")
                    self.status.set(f"Terminé : {len(self.outputs)} rendu(s) généré(s).")
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    # ----- aperçu ----------------------------------------------------------
    def _on_select(self, _evt=None) -> None:
        sel = self.listbox.curselection()
        if not sel:
            return
        name = self.listbox.get(sel[0])
        out = self.outputs.get(name)
        if out and os.path.isfile(out):
            self._render(out)
        else:
            self.canvas.delete("all")
            self.status.set(f"{name} : pas encore généré.")

    def _select_name(self, name: str) -> None:
        try:
            idx = self.listbox.get(0, "end").index(name)
        except ValueError:
            return
        self.listbox.selection_clear(0, "end")
        self.listbox.selection_set(idx)
        self.listbox.see(idx)
        self._on_select()

    def _on_canvas_resize(self, _evt=None) -> None:
        sel = self.listbox.curselection()
        if sel:
            name = self.listbox.get(sel[0])
            out = self.outputs.get(name)
            if out and os.path.isfile(out):
                self._render(out)

    def _render(self, pdf_path: str) -> None:
        """Rasterise la page 0 du PDF surligné pour l'afficher (aperçu seulement)."""
        try:
            doc = fitz.open(pdf_path)
            page = doc[0]
            cw = max(self.canvas.winfo_width(), 50)
            ch = max(self.canvas.winfo_height(), 50)
            pw, ph = page.rect.width, page.rect.height
            if page.rotation in (90, 270):
                pw, ph = ph, pw
            zoom = max(0.05, min(cw / pw, ch / ph))
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            data = base64.b64encode(pix.tobytes("png")).decode("ascii")
            doc.close()
        except Exception as exc:
            self.status.set(f"Aperçu impossible : {exc}")
            return
        self._imgref = tk.PhotoImage(data=data)
        self.canvas.delete("all")
        self.canvas.create_image(cw // 2, ch // 2, image=self._imgref, anchor="center")
        self.status.set(os.path.relpath(pdf_path))

    # ----- log -------------------------------------------------------------
    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _log_clear(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")


def main() -> int:
    initial = sys.argv[1] if len(sys.argv) > 1 else ""
    root = tk.Tk()
    App(root, initial_folder=initial)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
