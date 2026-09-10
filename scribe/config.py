"""Chargement et validation de la configuration (config.toml)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .resources import PRIORITY_LEVELS, resolve_jobs


@dataclass
class Config:
    """Paramètres du service, lus depuis config.toml."""

    watch_dir: Path
    languages: list[str] = field(default_factory=lambda: ["fra"])
    keep_backup: bool = False
    backup_dir: str = "_originaux"
    optimize: int = 1
    deskew: bool = True
    rotate_pages: bool = True
    use_polling: bool = True
    stable_seconds: float = 5.0
    rescan_seconds: float = 1800.0
    log_file: str = "scribe.log"
    # -- ménagement des ressources ---------------------------------------
    jobs: int = 0                 # 0 = automatique (la moitié des cœurs)
    priority: str = "basse"       # normale | basse | inactive
    poll_interval: float = 15.0   # secondes entre deux scrutations du dossier
    skip_if_text: bool = True     # ne pas relancer l'OCR si le PDF porte déjà du texte

    @property
    def language_arg(self) -> str:
        """Chaîne de langues attendue par Tesseract, ex. 'fra+eng'."""
        return "+".join(self.languages) if self.languages else "fra"

    @property
    def effective_jobs(self) -> int:
        """Nombre de cœurs réellement accordés à l'OCR."""
        return resolve_jobs(self.jobs)


def load_config(path: str | Path) -> Config:
    """Lit config.toml et renvoie un objet Config validé.

    Lève FileNotFoundError si le fichier est absent et ValueError si le
    dossier surveillé n'existe pas.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Fichier de configuration introuvable : {path}\n"
            "Copiez 'config.example.toml' en 'config.toml' puis adaptez-le."
        )

    with path.open("rb") as fh:
        data = tomllib.load(fh)

    if "watch_dir" not in data:
        raise ValueError("La clé 'watch_dir' est obligatoire dans config.toml.")

    watch_dir = Path(data["watch_dir"]).expanduser()
    if not watch_dir.is_dir():
        raise ValueError(
            f"Le dossier à surveiller n'existe pas : {watch_dir}\n"
            "Vérifiez la valeur de 'watch_dir' dans config.toml."
        )

    priority = str(data.get("priority", "basse")).strip().lower()
    if priority not in PRIORITY_LEVELS:
        raise ValueError(
            f"Valeur de 'priority' invalide : {priority!r}. "
            f"Valeurs possibles : {', '.join(PRIORITY_LEVELS)}."
        )

    # Une scrutation trop rapprochée est la première cause de disque qui
    # travaille en permanence : on impose un plancher raisonnable.
    poll_interval = max(1.0, float(data.get("poll_interval", 15.0)))

    return Config(
        watch_dir=watch_dir,
        languages=list(data.get("languages", ["fra"])),
        keep_backup=bool(data.get("keep_backup", False)),
        backup_dir=str(data.get("backup_dir", "_originaux")),
        optimize=int(data.get("optimize", 1)),
        deskew=bool(data.get("deskew", True)),
        rotate_pages=bool(data.get("rotate_pages", True)),
        use_polling=bool(data.get("use_polling", True)),
        stable_seconds=float(data.get("stable_seconds", 5.0)),
        rescan_seconds=float(data.get("rescan_seconds", 1800.0)),
        log_file=str(data.get("log_file", "scribe.log")),
        jobs=int(data.get("jobs", 0)),
        priority=priority,
        poll_interval=poll_interval,
        skip_if_text=bool(data.get("skip_if_text", True)),
    )
