"""Tests de non-régression sur la consommation de ressources.

Ils protègent les deux corrections qui comptent :

1. **Pas de boucle de retraitement.** Un PDF déjà traité dont la seule date de
   modification a été retouchée — ce que fait la synchronisation OneDrive — ne
   doit PAS repartir à l'OCR. C'est le défaut qui faisait grimper sans fin le
   nombre de fichiers à traiter alors qu'aucun document n'arrivait.
2. **Pas d'OCR inutile.** Un PDF portant déjà du texte sur toutes ses pages ne
   doit solliciter ni Tesseract ni Ghostscript.
3. **Pas de journal sans fin.** Un journal hors gabarit doit être mis de côté
   au démarrage — et conservé, jamais supprimé.
4. **Rien ne fuit sur la sortie d'erreur.** Ce qu'écrivent OCRmyPDF, Ghostscript
   et Tesseract doit atterrir dans ``scribe.log``, plafonné, et non dans le
   fichier sans limite où le service redirige ``stderr``.

Le moteur OCR est simulé : ces tests n'ont besoin ni de Tesseract ni de
Ghostscript, et tournent en quelques secondes.

Lancement : python -m tests.test_ressources   (ou pytest tests/)
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import types
from enum import IntEnum
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --- moteur OCR simulé, installé avant l'import de scribe.processor --------
_OCR_CALLS: list[dict] = []


class _ExitCode(IntEnum):
    ok = 0


class _Exceptions:
    class PriorOcrFoundError(Exception):
        pass

    class EncryptedPdfError(Exception):
        pass

    class MissingDependencyError(Exception):
        pass


def _fake_ocr(source, dest, **kwargs):
    _OCR_CALLS.append(kwargs)
    shutil.copy(source, dest)
    Path(dest).write_bytes(Path(dest).read_bytes() + b"\n% couche texte\n")
    return _ExitCode.ok


_fake = types.ModuleType("ocrmypdf")
_fake.ocr = _fake_ocr
_fake.ExitCode = _ExitCode
_fake.exceptions = _Exceptions
sys.modules.setdefault("ocrmypdf", _fake)

from scribe import journal                # noqa: E402
from scribe.config import Config          # noqa: E402
from scribe.logging_setup import archive_if_oversize, setup_logging  # noqa: E402
from scribe.state import ProcessedStore   # noqa: E402
from scribe.status import StatusReporter  # noqa: E402
from scribe.watcher import OcrService     # noqa: E402


# --- fabrication de PDF minimaux ------------------------------------------
def _make_pdfs(folder: Path) -> tuple[Path, Path]:
    """Crée un PDF « scan » (aucune police) et un PDF déjà textuel."""
    import pikepdf

    image = folder / "scan.pdf"
    doc = pikepdf.Pdf.new()
    doc.add_blank_page(page_size=(200, 200))
    doc.save(str(image))

    textuel = folder / "acte.pdf"
    doc = pikepdf.Pdf.new()
    doc.add_blank_page(page_size=(200, 200))
    police = doc.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Font,
        Subtype=pikepdf.Name.Type1,
        BaseFont=pikepdf.Name.Helvetica,
    ))
    pikepdf.Page(doc.pages[0]).add_resource(
        police, pikepdf.Name.Font, pikepdf.Name.F1)
    doc.save(str(textuel))
    return image, textuel


class _Bench:
    """Un dossier surveillé jetable, avec son registre et son rapporteur."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="scribe-test-"))
        self.watch = self.root / "surveille"
        self.watch.mkdir()
        self.scan, self.acte = _make_pdfs(self.watch)
        self.config = Config(watch_dir=self.watch, stable_seconds=1)
        self.store = ProcessedStore(self.root / "registre.json")
        self._n = 0

    def cycle(self) -> dict:
        """Un passage complet du service ; renvoie l'état publié."""
        self._n += 1
        status_path = self.root / f"status-{self._n}.json"
        reporter = StatusReporter(status_path)
        OcrService(self.config, self.store, reporter,
                   control_dir=self.root).run_once()
        reporter.stop()   # force l'écriture du dernier état
        return json.loads(status_path.read_text(encoding="utf-8"))


def test_premier_passage_ocerise_le_scan_et_epargne_le_pdf_textuel():
    bench = _Bench()
    _OCR_CALLS.clear()
    etat = bench.cycle()

    assert etat["ocr"] == 1, "le PDF image aurait dû être océrisé"
    assert etat["skipped"] == 1, "le PDF déjà textuel aurait dû être écarté"
    assert etat["errors"] == 0
    assert len(_OCR_CALLS) == 1, "le PDF déjà textuel a sollicité l'OCR pour rien"
    bench.store.stop()


def test_le_parallelisme_est_borne():
    """OCRmyPDF ne doit jamais recevoir carte blanche sur tous les cœurs."""
    bench = _Bench()
    _OCR_CALLS.clear()
    bench.cycle()

    jobs = _OCR_CALLS[0].get("jobs")
    assert jobs is not None, "'jobs' n'est pas transmis : l'OCR prendrait tous les cœurs"
    assert 1 <= jobs <= (os.cpu_count() or 1)
    bench.store.stop()


def test_une_date_retouchee_ne_relance_pas_l_ocr():
    """Le cœur du correctif : OneDrive retouche les dates, pas le contenu."""
    bench = _Bench()
    bench.cycle()                     # tout est traité et mémorisé

    plus_tard = time.time() + 900
    for pdf in (bench.scan, bench.acte):
        os.utime(pdf, (plus_tard, plus_tard))

    _OCR_CALLS.clear()
    etat = bench.cycle()

    assert etat["total"] == 0, (
        "des fichiers inchangés sont repartis en file : la boucle de "
        "retraitement est de retour"
    )
    assert len(_OCR_CALLS) == 0, "un fichier inchangé a été réocérisé"
    bench.store.stop()


def test_une_vraie_modification_relance_bien_l_ocr():
    """Le garde-fou ne doit pas empêcher de traiter un fichier réellement changé."""
    bench = _Bench()
    bench.cycle()

    bench.scan.write_bytes(bench.scan.read_bytes() + b"\n% contenu different\n")
    _OCR_CALLS.clear()
    etat = bench.cycle()

    assert etat["ocr"] == 1, "un fichier réellement modifié n'a pas été retraité"
    assert len(_OCR_CALLS) == 1
    bench.store.stop()


def test_aucun_fichier_temporaire_abandonne():
    bench = _Bench()
    bench.cycle()

    restes = sorted(p.name for p in bench.watch.iterdir())
    assert restes == ["acte.pdf", "scan.pdf"], f"fichiers inattendus : {restes}"
    bench.store.stop()


def test_le_registre_purge_ses_entrees_obsoletes():
    bench = _Bench()
    bench.cycle()
    assert bench.store.stats()["entries"] == 2

    bench.scan.unlink()
    assert bench.store.prune() == 1
    assert bench.store.stats()["entries"] == 1
    bench.store.stop()


# --- journalisation --------------------------------------------------------
def test_un_journal_hors_gabarit_est_mis_de_cote_et_conserve():
    dossier = Path(tempfile.mkdtemp(prefix="scribe-log-"))
    log = dossier / "scribe.log"
    log.write_bytes(b"ligne de journal\n" * 500_000)   # ~8 Mo
    taille = log.stat().st_size

    archive = archive_if_oversize(log)

    assert archive is not None, "le journal hors gabarit n'a pas été mis de côté"
    assert archive.exists(), "l'ancien journal a disparu : il ne doit jamais être supprimé"
    assert archive.stat().st_size == taille
    assert not log.exists(), "le journal aurait dû être renommé"


def test_un_journal_de_taille_normale_est_laisse_tel_quel():
    dossier = Path(tempfile.mkdtemp(prefix="scribe-log-"))
    log = dossier / "scribe.log"
    log.write_bytes(b"court\n")

    assert archive_if_oversize(log) is None
    assert log.exists()


def test_l_analyse_du_journal_reconnait_une_boucle():
    dossier = Path(tempfile.mkdtemp(prefix="scribe-log-"))
    lignes = []
    for _ in range(30):   # un PDF repris 30 fois : une boucle
        lignes.append("2026-08-15 09:12:33  INFO     "
                      "Traitement : C:/Actes/boucle.pdf (jusqu'à 8 cœur(s))")
    for i in range(5):    # cinq PDF traités une seule fois
        lignes.append(f"2026-08-15 10:00:0{i}  INFO     Traitement : C:/Actes/{i}.pdf")
    (dossier / "scribe.log").write_text("\n".join(lignes), encoding="utf-8")
    # une archive de rotation doit être dépouillée elle aussi
    (dossier / "scribe.log.1").write_text(
        "2026-07-01 08:00:00  INFO     Traitement : C:/Actes/boucle.pdf\n",
        encoding="utf-8")

    fichiers = journal.journaux(dossier)
    assert len(fichiers) == 2, "les archives de rotation doivent être incluses"

    r = journal.analyser(fichiers)
    assert r["traitements"] == 36
    assert r["pdf_distincts"] == 6
    assert r["pdf_repetes"] == 1
    assert r["traitements_en_trop"] == 30
    assert r["palmares"][0] == ("C:/Actes/boucle.pdf", 31)


def test_l_analyse_ne_signale_rien_sur_un_journal_sain():
    dossier = Path(tempfile.mkdtemp(prefix="scribe-log-"))
    (dossier / "scribe.log").write_text(
        "\n".join(f"2026-08-15 10:00:0{i}  INFO     Traitement : C:/Actes/{i}.pdf"
                  for i in range(5)),
        encoding="utf-8")

    r = journal.analyser(journal.journaux(dossier))
    assert r["traitements_en_trop"] == 0
    assert r["pdf_repetes"] == 0


def _journalisation_propre():
    """Remet la journalisation à zéro : le logger racine est global."""
    for cible in (logging.getLogger(), logging.getLogger("scribe")):
        for handler in list(cible.handlers):
            handler.close()
        cible.handlers.clear()


def test_les_messages_d_ocrmypdf_ne_fuient_pas_sur_la_sortie_d_erreur():
    """Le cœur du correctif : 67 Mo de service-err.log venaient de là.

    OCRmyPDF capture la sortie de Ghostscript et de Tesseract puis la réémet
    sur son propre logger. Sans handler, Python bascule sur son handler de
    dernier recours, qui écrit sur stderr — que le service redirige vers un
    fichier sans aucune limite de taille.
    """
    dossier = Path(tempfile.mkdtemp(prefix="scribe-log-"))
    log = dossier / "scribe.log"
    erreurs = io.StringIO()
    try:
        with contextlib.redirect_stderr(erreurs), contextlib.redirect_stdout(io.StringIO()):
            setup_logging(log)
            logging.getLogger("ocrmypdf").error("Ghostscript : zero-size page")
            logging.getLogger("ocrmypdf._exec.ghostscript").warning("invalid xref")
            for handler in logging.getLogger().handlers:
                handler.flush()

        contenu = log.read_text(encoding="utf-8")
        assert "zero-size page" in contenu, "le message d'OCRmyPDF n'est pas dans scribe.log"
        assert "invalid xref" in contenu
        assert "zero-size page" not in erreurs.getvalue(), (
            "le message a fui sur la sortie d'erreur : il gonflerait service-err.log"
        )
    finally:
        _journalisation_propre()


def test_l_analyse_repere_une_ligne_repetee_en_masse():
    """Indispensable pour service-err.log : aucun « Traitement : » n'y figure."""
    dossier = Path(tempfile.mkdtemp(prefix="scribe-log-"))
    lignes = [f"   **** Error: Ignoring zero-size page, page {i}." for i in range(500)]
    lignes += ["   **** Warning: invalid xref entry."] * 3
    (dossier / "service-err.log").write_text("\n".join(lignes), encoding="utf-8")

    r = journal.analyser(journal.journaux(dossier))

    assert r["traitements"] == 0, "ce journal ne contient aucun traitement"
    motif, nombre = r["motifs"][0]
    assert nombre == 500, "les numéros de page doivent être regroupés en un seul motif"
    assert "#" in motif, "les nombres doivent être masqués pour regrouper les répétitions"


def test_setup_logging_n_ecrit_pas_deux_fois_dans_le_fichier():
    """Un message ne doit apparaître qu'une fois dans scribe.log."""
    dossier = Path(tempfile.mkdtemp(prefix="scribe-log-"))
    log = dossier / "scribe.log"
    logger = setup_logging(log)
    logger.info("message temoin unique")
    for handler in logger.handlers:
        handler.flush()

    contenu = log.read_text(encoding="utf-8")
    assert contenu.count("message temoin unique") == 1, contenu
    _journalisation_propre()


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    echecs = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            echecs += 1
            print(f"ÉCHEC   {test.__name__}\n        {exc}")
        else:
            print(f"OK      {test.__name__}")
    print(f"\n{len(tests) - echecs}/{len(tests)} test(s) réussi(s).")
    return 1 if echecs else 0


if __name__ == "__main__":
    raise SystemExit(_main())
