"""Application compagnon : icône dans la barre des tâches + fenêtre de progression.

Contrairement au service Windows (isolé du bureau), cette petite application
tourne dans la session de l'utilisateur et peut donc afficher une icône en bas
à droite et une fenêtre avec une barre de progression. Elle ne fait AUCUN OCR :
elle lit simplement le fichier d'état (status.json) écrit par le service et
l'affiche. Le rafraîchissement s'espace automatiquement lorsque la fenêtre
est masquée : il n'y a alors qu'une infobulle à tenir à jour.

Lancement : python -m scribe.tray
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from . import control, paths
from .status import read_status

try:  # l'icône de la barre des tâches est optionnelle (dégradation gracieuse)
    import pystray
    from PIL import Image, ImageDraw
    _HAS_TRAY = True
except Exception:  # noqa: BLE001
    _HAS_TRAY = False

# Rafraîchissement de l'affichage. Fenêtre visible : une fois par seconde.
# Fenêtre masquée (le cas le plus fréquent — l'app vit dans la barre des
# tâches) : beaucoup plus rarement, il n'y a alors qu'une infobulle à tenir à
# jour. Relire et réanalyser status.json chaque seconde pour une fenêtre que
# personne ne regarde était une dépense pure.
POLL_VISIBLE_MS = 1000
POLL_HIDDEN_MS = 5000


def _watch_dir() -> Path | None:
    """Lit le dossier surveillé depuis config.toml (pour le menu)."""
    import tomllib

    try:
        cfg = tomllib.loads(paths.default_config_path().read_text(encoding="utf-8"))
        return Path(cfg["watch_dir"])
    except Exception:  # noqa: BLE001
        return None


def _log_path() -> Path:
    return paths.data_dir() / "scribe.log"


def _status_path() -> Path:
    return paths.data_dir() / "status.json"


def _open(path: Path | None) -> None:
    if path and path.exists():
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]  # Windows
        except (AttributeError, OSError):
            pass


def _asset_path(name: str) -> Path | None:
    """Localise une ressource (icône) en mode gelé (PyInstaller) ou dev."""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / name)
    candidates.append(paths.app_dir() / "assets" / name)
    candidates.append(paths.app_dir() / name)
    for c in candidates:
        if c.exists():
            return c
    return None


def _make_icon_image():
    """Icône de la barre des tâches : le scribe (assets/icon.png)."""
    p = _asset_path("icon.png")
    if p is not None:
        try:
            return Image.open(str(p))
        except Exception:  # noqa: BLE001
            pass
    # Repli minimal si l'image n'est pas trouvée.
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([10, 6, 46, 58], radius=6, fill=(240, 244, 250, 255),
                        outline=(40, 90, 160, 255), width=2)
    return img


class TrayApp:
    """Fenêtre Tkinter + icône pystray, reliées par une file de commandes."""

    def __init__(self) -> None:
        self._cmds: queue.Queue[str] = queue.Queue()
        self._icon = None
        self._visible = True
        self._last_updated: str | None = None   # horodatage du dernier état affiché
        self._root = tk.Tk()
        self._build_window()

    # -- construction de l'interface -------------------------------------
    def _build_window(self) -> None:
        self._root.title("Scribe — progression")
        self._root.geometry("460x360")
        self._root.minsize(420, 320)
        self._root.protocol("WM_DELETE_WINDOW", self._hide)  # X = masquer

        # Icône de la fenêtre (barre de titre / Alt+Tab).
        icon_png = _asset_path("icon.png")
        if icon_png is not None:
            try:
                self._win_icon = tk.PhotoImage(file=str(icon_png))
                self._root.iconphoto(True, self._win_icon)
            except tk.TclError:
                pass

        pad = {"padx": 12, "pady": 6}
        header = ttk.Label(self._root, text="Scribe", font=("Segoe UI", 16, "bold"))
        header.pack(anchor="w", **pad)

        self._count_var = tk.StringVar(value="Initialisation…")
        ttk.Label(self._root, textvariable=self._count_var,
                  font=("Segoe UI", 11)).pack(anchor="w", padx=12)

        self._bar = ttk.Progressbar(self._root, mode="determinate", length=430)
        self._bar.pack(padx=12, pady=8)

        self._current_var = tk.StringVar(value="")
        ttk.Label(self._root, textvariable=self._current_var,
                  foreground="#1a5aa0").pack(anchor="w", padx=12)

        self._pending_var = tk.StringVar(value="")
        ttk.Label(self._root, textvariable=self._pending_var).pack(anchor="w", padx=12)

        ttk.Label(self._root, text="Derniers fichiers traités :").pack(
            anchor="w", padx=12, pady=(10, 0))
        self._recent = tk.Listbox(self._root, height=7)
        self._recent.pack(fill="both", expand=True, padx=12, pady=4)

        btns = ttk.Frame(self._root)
        btns.pack(fill="x", padx=12, pady=8)
        self._pause_btn = ttk.Button(btns, text="Mettre en pause",
                                     command=self._toggle_pause)
        self._pause_btn.pack(side="left")
        ttk.Button(btns, text="Ouvrir le dossier",
                   command=lambda: _open(_watch_dir())).pack(side="left", padx=6)
        ttk.Button(btns, text="Ouvrir le journal",
                   command=lambda: _open(_log_path())).pack(side="left")
        ttk.Button(btns, text="Masquer", command=self._hide).pack(side="right")

        self._updated_var = tk.StringVar(value="")
        ttk.Label(self._root, textvariable=self._updated_var,
                  foreground="#888").pack(anchor="w", padx=12, pady=(0, 6))

    # -- rafraîchissement -------------------------------------------------
    def _refresh(self) -> None:
        # Un seul accès disque pour l'état de pause (il en fallait trois).
        paused = control.is_paused(paths.data_dir())
        st = read_status(_status_path())

        if st:
            done = st.get("done", 0)
            total = st.get("total", 0)
            pending = st.get("pending", 0)
            current = st.get("current_name")
            updated = st.get("updated", "")

            if self._icon is not None:
                suffix = " (en pause)" if paused else ""
                self._icon.title = f"Scribe : {done}/{total} traités{suffix}"

            # Fenêtre masquée : l'infobulle suffit, on ne redessine rien.
            # Rien de neuf depuis le dernier passage : idem.
            if self._visible and updated != self._last_updated:
                self._last_updated = updated
                self._paint(st, done, total, pending, current, updated, paused)
        elif self._visible:
            self._count_var.set("En attente du service Scribe…")
            self._current_var.set("Le service n'a pas encore publié d'état.")

        if self._visible:
            self._update_pause_button()
        # Traiter les commandes venues de l'icône (thread pystray).
        self._drain_commands()
        self._root.after(
            POLL_VISIBLE_MS if self._visible else POLL_HIDDEN_MS, self._refresh
        )

    def _paint(self, st, done, total, pending, current, updated, paused) -> None:
        """Met à jour les widgets. Appelé seulement si la fenêtre est visible."""
        ocr = st.get("ocr")
        if ocr is None:
            self._count_var.set(f"{done} sur {total} PDF traités")
        else:
            # On distingue ce qui a été océrisé de ce qui était déjà à jour :
            # sans cela, un simple contrôle gonflait le compteur comme un
            # véritable traitement.
            self._count_var.set(
                f"{done} sur {total} PDF examinés — {ocr} océrisé(s), "
                f"{st.get('skipped', 0)} déjà à jour"
            )
        self._bar["maximum"] = max(total, 1)
        self._bar["value"] = done

        if paused:
            self._current_var.set("⏸ En pause — traitement suspendu")
        elif current:
            self._current_var.set(f"En cours : {current}")
        elif pending == 0:
            self._current_var.set("À jour — tous les PDF sont traités ✔")
        else:
            self._current_var.set("En attente…")

        errors = st.get("errors", 0)
        self._pending_var.set(
            f"Restant en file : {pending}"
            + (f" — {errors} erreur(s), voir le journal" if errors else "")
        )
        self._updated_var.set(f"Mis à jour à {updated}")

        self._recent.delete(0, tk.END)
        for item in st.get("recent", []):
            self._recent.insert(
                tk.END, f"{item.get('at', '')}  {item.get('file', '')}  "
                        f"[{item.get('status', '')}]")

    def _drain_commands(self) -> None:
        try:
            while True:
                cmd = self._cmds.get_nowait()
                if cmd == "show":
                    self._show()
                elif cmd == "pause":
                    self._toggle_pause()
                elif cmd == "folder":
                    _open(_watch_dir())
                elif cmd == "log":
                    _open(_log_path())
                elif cmd == "quit":
                    self._quit()
        except queue.Empty:
            pass

    # -- pause / reprise --------------------------------------------------
    def _toggle_pause(self) -> None:
        paused = control.is_paused(paths.data_dir())
        control.set_paused(paths.data_dir(), not paused)
        self._update_pause_button()

    def _update_pause_button(self) -> None:
        paused = control.is_paused(paths.data_dir())
        self._pause_btn.config(text="Reprendre" if paused else "Mettre en pause")

    # -- fenêtre : afficher / masquer / quitter --------------------------
    def _show(self) -> None:
        self._root.deiconify()
        self._root.lift()
        self._root.focus_force()
        self._visible = True
        self._last_updated = None   # force un réaffichage complet

    def _hide(self) -> None:
        # Masquer dans la barre des tâches si l'icône existe, sinon réduire.
        if self._icon is not None:
            self._root.withdraw()
        else:
            self._root.iconify()
        self._visible = False

    def _quit(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001
                pass
        self._root.destroy()

    # -- icône de la barre des tâches ------------------------------------
    def _start_tray(self) -> None:
        if not _HAS_TRAY:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Afficher la progression",
                             lambda: self._cmds.put("show"), default=True),
            pystray.MenuItem("Mettre en pause / Reprendre",
                             lambda: self._cmds.put("pause")),
            pystray.MenuItem("Ouvrir le dossier surveillé",
                             lambda: self._cmds.put("folder")),
            pystray.MenuItem("Ouvrir le journal",
                             lambda: self._cmds.put("log")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quitter", lambda: self._cmds.put("quit")),
        )
        self._icon = pystray.Icon("scribe", _make_icon_image(), "Scribe", menu)
        threading.Thread(target=self._icon.run, daemon=True).start()

    # -- boucle principale -----------------------------------------------
    def run(self) -> None:
        self._start_tray()
        if self._icon is not None:
            self._root.withdraw()  # démarre discrètement dans la barre des tâches
            self._visible = False
        self._root.after(500, self._refresh)
        self._root.mainloop()


def main(argv: list[str] | None = None) -> int:
    paths.configure_engines()  # sans effet ici, mais cohérent
    try:
        TrayApp().run()
    except tk.TclError as exc:
        print(f"Interface indisponible : {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
