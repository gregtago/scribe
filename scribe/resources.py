"""Bridage des ressources : priorité du processus et parallélisme de l'OCR.

Scribe travaille en tâche de fond pendant que l'on se sert du poste. Il doit
donc s'effacer : priorité processeur basse et entrées/sorties en arrière-plan,
pour que les autres logiciels restent prioritaires.

Deux leviers, complémentaires :

- ``resolve_jobs`` limite le nombre de cœurs qu'OCRmyPDF a le droit d'utiliser.
  Sans cette limite, OCRmyPDF prend TOUS les cœurs de la machine et le poste
  devient inutilisable pendant le traitement d'un gros PDF.
- ``lower_priority`` abaisse la priorité du processus. Sous Windows, les
  processus enfants (Tesseract, Ghostscript) héritent de la priorité du père :
  il suffit donc de l'abaisser une fois au démarrage du service.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("scribe.resources")

# Niveaux acceptés dans config.toml (valeur -> libellé).
PRIORITY_LEVELS = ("normale", "basse", "inactive")

# Constantes Windows (winbase.h).
_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
_IDLE_PRIORITY_CLASS = 0x00000040
_PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000


def default_jobs() -> int:
    """Parallélisme par défaut : la moitié des cœurs, au minimum 1.

    La moitié laisse systématiquement de quoi travailler confortablement,
    même sur un poste à 4 cœurs.
    """
    cores = os.cpu_count() or 2
    return max(1, cores // 2)


def resolve_jobs(configured: int | None) -> int:
    """Valeur effective de ``jobs`` : 0 ou absent -> valeur par défaut."""
    cores = os.cpu_count() or 2
    if not configured or configured <= 0:
        return default_jobs()
    return max(1, min(int(configured), cores))


def _lower_priority_windows(level: str) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.GetCurrentProcess()

    if level == "inactive":
        # Mode « arrière-plan » : abaisse d'un coup la priorité processeur,
        # celle des entrées/sorties ET celle de la mémoire. C'est le réglage
        # le plus discret que propose Windows.
        if kernel32.SetPriorityClass(handle, _PROCESS_MODE_BACKGROUND_BEGIN):
            return True
        # Échoue si le processus y est déjà : on retombe sur IDLE.
        return bool(kernel32.SetPriorityClass(handle, _IDLE_PRIORITY_CLASS))

    return bool(kernel32.SetPriorityClass(handle, _BELOW_NORMAL_PRIORITY_CLASS))


def _lower_priority_posix(level: str) -> bool:
    # Utile en développement sous Linux/macOS ; sans équivalent exact des
    # classes Windows, on se contente de « nice ».
    try:
        os.nice(19 if level == "inactive" else 10)
        return True
    except (AttributeError, OSError):
        return False


def lower_priority(level: str = "basse") -> bool:
    """Abaisse la priorité du processus courant.

    Renvoie True si le réglage a été appliqué. Ne lève jamais : un poste qui
    refuse le changement de priorité doit continuer à océriser normalement.
    """
    level = (level or "basse").strip().lower()
    if level not in PRIORITY_LEVELS:
        logger.warning(
            "Niveau de priorité inconnu (%s) : valeurs possibles %s. "
            "Réglage ignoré.", level, ", ".join(PRIORITY_LEVELS),
        )
        return False
    if level == "normale":
        return False

    try:
        applied = (
            _lower_priority_windows(level) if os.name == "nt"
            else _lower_priority_posix(level)
        )
    except Exception:  # noqa: BLE001 - jamais bloquant
        logger.debug("Impossible d'abaisser la priorité du processus.", exc_info=True)
        return False

    if applied:
        logger.info("Priorité du processus abaissée (niveau « %s »).", level)
    return applied
