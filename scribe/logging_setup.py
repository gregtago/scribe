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

Pourquoi la journalisation est posée sur le logger RACINE
---------------------------------------------------------
OCRmyPDF **capture** la sortie de Ghostscript et de Tesseract puis la réémet
par son propre logger Python (``ocrmypdf``). Tant que l'on ne configurait que
le logger ``scribe``, ce logger-là n'avait aucun destinataire : Python bascule
alors sur son « handler de dernier recours », qui écrit sur ``stderr``. Sous le
service, NSSM redirige ``stderr`` vers ``service-err.log`` — un fichier sans
aucune limite de taille. Résultat : les avertissements de Ghostscript, très
nombreux sur des scans, gonflaient ce fichier jusqu'à des dizaines de
méga-octets **sans jamais apparaître dans le journal de Scribe**, là où on les
aurait vus.

Les handlers sont donc posés sur le logger **racine**, qui recueille aussi ce
qu'écrivent les bibliothèques : leurs messages atterrissent dans ``scribe.log``,
plafonné et rotatif, au lieu de fuir sur la sortie d'erreur.

À noter : ``service-out.log`` et ``service-err.log`` eux-mêmes ne sont pas
écrits par Scribe. Ils ont leur propre rotation, réglée à l'installation (voir
``build/installer.iss``).
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

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_file, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    # Logger RACINE : il recueille aussi ce qu'écrivent OCRmyPDF, Ghostscript
    # et Tesseract. Sans lui, ces messages partaient sur la sortie d'erreur,
    # sans plafond (voir l'explication en tête de module). Niveau AVERTISSEMENT
    # pour les bibliothèques : leurs messages d'information n'apportent rien ici.
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    root.handlers.clear()
    root.addHandler(file_handler)

    # En exécutable « fenêtré » (le service), sys.stdout est absent :
    # on n'ajoute la sortie console que si elle existe réellement.
    # La cible est explicitement la sortie STANDARD : sans argument,
    # StreamHandler écrit sur la sortie d'ERREUR, que le service redirige
    # justement vers le fichier que l'on cherche à ne plus voir grossir.
    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        root.addHandler(console)

    # Les avertissements de Python (DeprecationWarning...) passent eux aussi par
    # la journalisation plutôt que par la sortie d'erreur.
    logging.captureWarnings(True)

    # Bibliothèques notoirement bavardes : mêmes seuils que ceux retenus par
    # OCRmyPDF lui-même pour son propre programme.
    for nom, niveau in (
        ("pdfminer", logging.ERROR),
        ("PIL", logging.INFO),
        ("fontTools", logging.ERROR),
    ):
        logging.getLogger(nom).setLevel(niveau)

    # Le journal de Scribe reste au niveau INFO : c'est lui qui raconte le
    # travail fait. Il ne porte pas de handler propre et remonte vers la racine.
    logger = logging.getLogger("scribe")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = True

    if archive is not None:
        logger.warning(
            "Journal précédent hors gabarit, mis de côté sous %s "
            "(la rotation avait échoué ; le fichier n'a pas été supprimé).",
            archive.name,
        )
    return logger
