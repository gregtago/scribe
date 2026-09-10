"""Traitement OCR d'un fichier PDF : image -> texte recherchable.

S'appuie sur la bibliothèque OCRmyPDF (moteur Tesseract). La reconnaissance
se fait avec l'option skip_text : les pages déjà porteuses de texte sont
laissées telles quelles, seules les pages « image » sont océrisées. Le
traitement est donc idempotent — relancer sur un PDF déjà océrisé ne
l'abîme pas.

Deux garde-fous protègent les ressources du poste :

- **Contrôle préalable** : avant de lancer quoi que ce soit, on regarde si
  toutes les pages portent déjà du texte. Si oui, on s'arrête là. Ce contrôle
  coûte quelques millisecondes, là où un passage inutile dans Tesseract coûte
  plusieurs minutes et tous les cœurs de la machine.
- **Parallélisme borné** : OCRmyPDF reçoit un nombre de tâches (``jobs``)
  explicite. Sans cela il s'empare de TOUS les cœurs et le poste se fige
  pendant le traitement.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from .config import Config

logger = logging.getLogger("scribe.processor")


def has_text_layer(pdf: Path) -> bool | None:
    """Toutes les pages portent-elles déjà du texte ?

    Renvoie True (rien à océriser), False (au moins une page est une pure
    image) ou None quand la question ne peut pas être tranchée — PDF chiffré,
    illisible, ou pikepdf indisponible : on laisse alors OCRmyPDF décider.

    Le critère est la présence d'une **police** dans les ressources de chaque
    page : une page issue d'un scanner n'en contient aucune, une page océrisée
    en contient toujours au moins une. Seuls les dictionnaires de pages sont
    lus, jamais le contenu : c'est quasi instantané, même sur un gros PDF.

    Réserve : un scan sur lequel une police aurait été ajoutée à chaque page
    par un autre outil (numérotation, tampon) serait écarté à tort. Le cas est
    rare, il est tracé dans le journal, et l'option ``skip_if_text = false``
    permet de désactiver ce contrôle.
    """
    try:
        import pikepdf
    except ImportError:
        return None

    try:
        with pikepdf.Pdf.open(str(pdf)) as doc:
            if len(doc.pages) == 0:
                return None
            for page in doc.pages:
                resources = pikepdf.Page(page).resources
                fonts = resources.get("/Font") if resources is not None else None
                if not fonts or len(fonts) == 0:
                    return False
        return True
    except Exception:  # noqa: BLE001 - contrôle purement opportuniste
        logger.debug("Contrôle de couche texte impossible sur %s", pdf, exc_info=True)
        return None


def _backup_original(pdf: Path, config: Config) -> None:
    """Conserve une copie de l'original avant remplacement, si demandé."""
    if not config.keep_backup:
        return

    if config.backup_dir:
        dest_dir = pdf.parent / config.backup_dir
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / pdf.name
    else:
        dest = pdf.with_suffix(".orig.pdf")

    # Ne pas écraser une sauvegarde déjà présente.
    if not dest.exists():
        shutil.copy2(pdf, dest)
        logger.info("Sauvegarde de l'original -> %s", dest)


def process_pdf(pdf: Path, config: Config) -> bool:
    """Océrise un PDF et remplace l'original par la version recherchable.

    Renvoie True si le fichier a bien été (re)traité, False s'il a été
    ignoré (déjà entièrement textuel, chiffré, ou erreur récupérable).
    """
    # Contrôle préalable : le PDF porte-t-il déjà du texte partout ? Si oui,
    # on n'engage ni Tesseract ni Ghostscript, et on ne réécrit pas le fichier
    # (une réécriture inutile relancerait la synchronisation OneDrive).
    if config.skip_if_text and has_text_layer(pdf) is True:
        logger.info("Déjà recherchable, aucun traitement : %s", pdf)
        return False

    logger.info("Traitement : %s (jusqu'à %d cœur(s))", pdf, config.effective_jobs)

    # Import différé : le moteur OCR (lourd, dépendances natives) n'est requis
    # qu'au moment de traiter un fichier, pas à l'import du paquet.
    import ocrmypdf

    # On écrit d'abord dans un fichier temporaire, puis on bascule de façon
    # atomique : jamais de PDF à moitié écrit à la place de l'original.
    # Le temporaire reste dans le dossier du PDF, seul moyen de garantir que
    # le remplacement final est bien atomique (même volume).
    tmp_fd = tempfile.NamedTemporaryFile(
        prefix="ocr_", suffix=".pdf", dir=str(pdf.parent), delete=False
    )
    tmp_out = Path(tmp_fd.name)
    tmp_fd.close()

    try:
        # Le fichier d'entrée et de sortie sont passés en arguments POSITIONNELS
        # (le nom du 1er paramètre varie selon les versions d'OCRmyPDF ; le
        # passer en nommé provoquait « missing argument input_file_or_options »).
        result = ocrmypdf.ocr(
            str(pdf),
            str(tmp_out),
            language=config.language_arg,
            skip_text=True,          # n'océrise que les pages sans texte
            deskew=config.deskew,
            rotate_pages=config.rotate_pages,
            optimize=config.optimize,
            jobs=config.effective_jobs,   # borne le nombre de cœurs mobilisés
            progress_bar=False,
            output_type="pdf",
        )
    except ocrmypdf.exceptions.PriorOcrFoundError:
        logger.info("Déjà océrisé, ignoré : %s", pdf)
        tmp_out.unlink(missing_ok=True)
        return False
    except ocrmypdf.exceptions.EncryptedPdfError:
        logger.warning("PDF chiffré (mot de passe), ignoré : %s", pdf)
        tmp_out.unlink(missing_ok=True)
        return False
    except ocrmypdf.exceptions.MissingDependencyError as exc:
        logger.error("Dépendance manquante (Tesseract/Ghostscript ?) : %s", exc)
        tmp_out.unlink(missing_ok=True)
        raise
    except Exception:
        # Erreur inattendue : on nettoie et on PROPAGE (le fichier ne sera pas
        # marqué comme traité, donc réessayé plus tard après correction).
        tmp_out.unlink(missing_ok=True)
        raise

    if result != ocrmypdf.ExitCode.ok:
        logger.warning("OCRmyPDF a renvoyé le code %s pour %s", result, pdf)
        tmp_out.unlink(missing_ok=True)
        return False

    _backup_original(pdf, config)
    # Remplacement atomique de l'original par la version océrisée.
    shutil.move(str(tmp_out), str(pdf))
    logger.info("Terminé : %s (PDF texte recherchable)", pdf)
    return True
