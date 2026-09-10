"""Analyse des journaux : combien de fois chaque PDF a-t-il été traité ?

Un journal volumineux n'est pas anodin : il raconte ce que le service a
réellement fait. Un même PDF qui revient des dizaines de fois est la signature
d'une boucle de retraitement.

Le dépouillement porte aussi sur les lignes **répétées**. C'est indispensable
pour ``service-err.log``, où atterrit ce qu'écrivent Ghostscript et Tesseract :
on n'y trouve pas de « Traitement : », mais le même avertissement recopié des
centaines de milliers de fois. Les lignes sont normalisées — horodatage retiré,
chiffres remplacés par ``#`` — de sorte que deux occurrences du même message sur
des pages différentes se regroupent.

L'analyse lit les fichiers **ligne à ligne** : un journal de plusieurs dizaines
de méga-octets se traite sans charger quoi que ce soit en mémoire.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

# Noms des journaux produits par Scribe et par le service Windows (NSSM),
# archives de rotation comprises (scribe.log.1, service-out.log_*, ...).
MOTIFS_JOURNAUX = ("scribe.log*", "service-out.log*", "service-err.log*")

_HORODATAGE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+\w*\s*")
_CHIFFRES = re.compile(r"\d+")
_LONGUEUR_MOTIF = 110

_MARQUEUR_TRAITEMENT = "Traitement : "
_MARQUEUR_TERMINE = "Terminé : "
_MARQUEUR_IGNORE = "Déjà recherchable, aucun traitement : "


def journaux(dossier: Path) -> list[Path]:
    """Tous les fichiers de journal présents dans le dossier de données."""
    trouves: set[Path] = set()
    for motif in MOTIFS_JOURNAUX:
        trouves.update(p for p in dossier.glob(motif) if p.is_file())
    return sorted(trouves)


def _chemin_traite(ligne: str) -> str | None:
    """Extrait le PDF d'une ligne « Traitement : ... »."""
    position = ligne.find(_MARQUEUR_TRAITEMENT)
    if position == -1:
        return None
    reste = ligne[position + len(_MARQUEUR_TRAITEMENT):].strip()
    # Les versions récentes ajoutent « (jusqu'à N cœur(s)) » en fin de ligne.
    suffixe = reste.rfind(" (jusqu'à ")
    if suffixe != -1:
        reste = reste[:suffixe]
    return reste or None


def _motif(ligne: str) -> str:
    """Normalise une ligne pour regrouper ses répétitions.

    L'horodatage et le niveau sont retirés, les nombres remplacés par « # » :
    « page 12 : résolution estimée à 300 dpi » et « page 47 : résolution estimée
    à 200 dpi » deviennent alors un seul et même motif.
    """
    ligne = _HORODATAGE.sub("", ligne.strip())
    return _CHIFFRES.sub("#", ligne)[:_LONGUEUR_MOTIF]


def analyser(fichiers: list[Path]) -> dict:
    """Dépouille les journaux. Ne lève jamais sur un fichier illisible."""
    traitements: Counter[str] = Counter()
    motifs: Counter[str] = Counter()
    lignes = 0
    octets = 0
    termines = 0
    ignores = 0
    premiere: str | None = None
    derniere: str | None = None

    for fichier in fichiers:
        try:
            octets += fichier.stat().st_size
            with fichier.open("r", encoding="utf-8", errors="replace") as flux:
                for ligne in flux:
                    lignes += 1
                    if ligne[:4].isdigit():
                        horodatage = ligne[:19]
                        if premiere is None or horodatage < premiere:
                            premiere = horodatage
                        if derniere is None or horodatage > derniere:
                            derniere = horodatage
                    motif = _motif(ligne)
                    if motif:
                        motifs[motif] += 1
                    chemin = _chemin_traite(ligne)
                    if chemin is not None:
                        traitements[chemin] += 1
                    elif _MARQUEUR_TERMINE in ligne:
                        termines += 1
                    elif _MARQUEUR_IGNORE in ligne:
                        ignores += 1
        except OSError:
            continue

    total = sum(traitements.values())
    distincts = len(traitements)
    repetes = {c: n for c, n in traitements.items() if n > 1}
    return {
        "fichiers_journaux": len(fichiers),
        "octets": octets,
        "lignes": lignes,
        "premiere": premiere,
        "derniere": derniere,
        "traitements": total,
        "pdf_distincts": distincts,
        "pdf_repetes": len(repetes),
        "traitements_en_trop": total - distincts,
        "termines": termines,
        "ignores": ignores,
        "palmares": traitements.most_common(10),
        "motifs": motifs.most_common(5),
    }
