"""Configuration de la journalisation (fichier tournant + console).

``scribe.log`` est plafonné : 2 Mo par fichier, 5 archives, soit 12 Mo au grand
maximum.

Ce plafond n'est pourtant pas une garantie absolue. La rotation procède par
**renommage**, et sous Windows un renommage échoue si un autre programme tient
le fichier ouvert — antivirus, visionneuse laissée ouverte, indexeur. Or
``logging`` avale cette erreur en silence : le journal continue alors de
grossir dans le même fichier, sans limite. D'où le filet de sécurité posé au
démarrage, moment où plus personne ne tient le fichier : un journal
manifestement hors gabarit est mis de côté — **renommé, jamais supprimé**.

À noter : les fichiers ``service-out.log`` et ``service-err.log`` du dossier de
données ne viennent PAS d'ici. C'est le gestionnaire de service (NSSM) qui y
redirige la sortie du processus, dont celle de Tesseract et de Ghostscript. Ils
ont leur propre rotation, réglée à l'installation (voir ``build/installer.iss``).
"""

from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Gabarit du journal : 2 Mo par fichier, 5 archives -> 12 Mo au grand maximum.
MAX_BYTES = 2_000_000
BACKUP_COUNT = 5

# Au-delà de ce seuil, la rotation n'a manifestement pas fonctionné.
_OVERSIZE_FACTOR = 3


def archive_if_oversize(log_file: Path, max_bytes: int = MAX_BYTES) -> Path | None:
    """Met de côté un journal hors gabarit. Renvoie le nom de l'archive.

    Le fichier n'est jamais supprimé : il est renommé avec la date du jour, de
    sorte qu'il reste consultable et que la journalisation reparte à zéro.
    """
    try:
        if not log_file.exists() or log_file.stat().st_size <= max_bytes * _OVERSIZE_FACTOR:
            return None
        archive = log_file.with_name(
            f"{log_file.stem}-{time.strftime('%Y%m%d-%H%M%S')}{log_file.suffix}.ancien"
        )
        log_file.rename(archive)
        return archive
    except OSError:
        # Fichier verrouillé : on continue, la journalisation reste possible.
        return None


def setup_logging(log_file: str | Path) -> logging.Logger:
    """Initialise le logger racine du service."""
    log_file = Path(log_file)
    archive = archive_if_oversize(log_file)

    logger = logging.getLogger("scribe")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_file, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # En exécutable « fenêtré » (le service), sys.stdout est absent :
    # on n'ajoute la sortie console que si elle existe réellement.
    if sys.stdout is not None:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        logger.addHandler(console)

    if archive is not None:
        logger.warning(
            "Journal précédent hors gabarit, mis de côté sous %s "
            "(la rotation avait échoué ; le fichier n'a pas été supprimé).",
            archive.name,
        )
    return logger
