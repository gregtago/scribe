"""Point d'entrée du service : python -m scribe [config.toml]."""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from . import journal, paths, resources
from .config import load_config
from .logging_setup import setup_logging
from .state import ProcessedStore
from .status import StatusReporter
from .watcher import OcrService

DEFAULT_CONFIG_TEMPLATE = """\
# Configuration de Scribe (générée à l'installation).
watch_dir = "{watch_dir}"
languages = ["fra"]
keep_backup = false
backup_dir = "_originaux"
optimize = 1
deskew = true
rotate_pages = true
use_polling = true
stable_seconds = 5
rescan_seconds = 1800
poll_interval = 15
skip_if_text = true
jobs = 0
priority = "basse"
log_file = "scribe.log"
"""


def write_default_config(watch_dir: str) -> Path:
    """Écrit un config.toml par défaut dans le dossier de données.

    Utilisé par l'installeur (option --init-config). Ne remplace pas une
    configuration déjà présente. Crée le dossier surveillé s'il manque.
    """
    cfg_path = paths.data_dir() / "config.toml"
    try:
        Path(watch_dir).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if not cfg_path.exists():
        safe = watch_dir.replace("\\", "/")
        cfg_path.write_text(
            DEFAULT_CONFIG_TEMPLATE.format(watch_dir=safe), encoding="utf-8"
        )
    return cfg_path


def _analyser_journal(dossier: Path) -> int:
    """Dépouille les journaux : combien de fois chaque PDF a-t-il été traité ?

    Un journal volumineux s'explique. Un même PDF qui revient des dizaines de
    fois signale une boucle de retraitement — chaque passage consommant
    processeur, disque et synchronisation pour rien.
    """
    fichiers = journal.journaux(dossier)
    if not fichiers:
        print(f"Aucun journal trouvé dans {dossier}.")
        return 0

    r = journal.analyser(fichiers)
    print("Scribe — analyse des journaux")
    print("-" * 52)
    print(f"  {'fichiers dépouillés':<24}: {r['fichiers_journaux']}")
    print(f"  {'volume':<24}: {r['octets'] / 1_048_576:.1f} Mio "
          f"({r['lignes']} lignes)")
    if r["premiere"]:
        print(f"  {'période':<24}: du {r['premiere']} au {r['derniere']}")
    print(f"  {'traitements lancés':<24}: {r['traitements']}")
    print(f"  {'PDF distincts concernés':<24}: {r['pdf_distincts']}")
    print(f"  {'écartés sans OCR':<24}: {r['ignores']}")

    en_trop = r["traitements_en_trop"]
    if en_trop > 0:
        part = 100 * en_trop / r["traitements"] if r["traitements"] else 0
        print()
        print(f"  {r['pdf_repetes']} PDF ont été traités plusieurs fois, soit "
              f"{en_trop} traitements en trop ({part:.0f} % du total).")
        print("  Les plus repris :")
        for chemin, nombre in r["palmares"]:
            if nombre > 1:
                print(f"    {nombre:>5} fois  {chemin}")
    else:
        print("\n  Aucun PDF traité deux fois : pas de boucle de retraitement.")

    # Lignes répétées : c'est la seule lecture utile de service-err.log, où
    # Ghostscript et Tesseract recopient le même avertissement des milliers
    # de fois sans qu'aucun « Traitement : » n'y figure.
    motifs = [(m, n) for m, n in r["motifs"] if n > 1]
    if motifs and r["lignes"]:
        print("\n  Lignes les plus répétées (chiffres masqués par « # ») :")
        for motif, nombre in motifs:
            part = 100 * nombre / r["lignes"]
            print(f"    {nombre:>7} fois ({part:4.1f} %)  {motif}")
    return 0


def _diagnostic(config, state_path: Path, purge: bool = False) -> int:
    """Affiche l'état du registre et les réglages de ressources effectifs.

    Sert à répondre à la question « pourquoi Scribe travaille-t-il autant ? »
    sans avoir à ouvrir le journal : taille du registre, nombre d'entrées,
    entrées devenues obsolètes, et réglages réellement appliqués.
    """
    store = ProcessedStore(state_path)
    stats = store.stats()

    def ligne(intitule: str, valeur) -> None:
        print(f"  {intitule:<22}: {valeur}")

    if config.use_polling:
        scrutation = f"toutes les {config.poll_interval:.0f} s"
    else:
        scrutation = "événements natifs"

    print("Scribe — diagnostic")
    print("-" * 52)
    ligne("dossier surveillé", config.watch_dir)
    print("Registre des fichiers traités")
    ligne("emplacement", stats["path"])
    ligne("taille", f"{stats['size_bytes'] / 1024:.1f} Kio")
    ligne("fichiers mémorisés", stats["entries"])
    ligne("entrées obsolètes", f"{stats['missing']} (fichier disparu)")
    ligne("sans empreinte", f"{stats['without_hash']} (format antérieur)")
    fichiers = journal.journaux(state_path.parent)
    total = sum(f.stat().st_size for f in fichiers if f.exists())
    print("Journaux")
    ligne("fichiers", len(fichiers))
    ligne("volume total", f"{total / 1_048_576:.1f} Mio")
    for f in sorted(fichiers, key=lambda p: -p.stat().st_size)[:3]:
        ligne(f"  {f.name}"[:22], f"{f.stat().st_size / 1_048_576:.1f} Mio")
    print("Ressources")
    ligne("cœurs pour l'OCR", f"{config.effective_jobs} sur {os.cpu_count()}")
    ligne("priorité du processus", config.priority)
    ligne("scrutation du dossier", scrutation)
    ligne("analyse complète", f"toutes les {config.rescan_seconds:.0f} s")
    ligne("compression (optimize)", config.optimize)
    ligne("redressement (deskew)", config.deskew)
    ligne("rotation des pages", config.rotate_pages)
    ligne("saut si texte présent", config.skip_if_text)

    if purge:
        print(f"\nPurge : {store.prune()} entrée(s) retirée(s) du registre.")
    elif stats["missing"]:
        print("\nAstuce : --purger-registre retire les entrées obsolètes.")

    store.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scribe",
        description="Service de fond : océrise les PDF d'un dossier (image -> texte).",
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=None,
        help="Chemin du fichier de configuration (défaut : détecté automatiquement).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Traite les fichiers présents puis s'arrête (pas de surveillance).",
    )
    parser.add_argument(
        "--init-config",
        metavar="DOSSIER",
        help="Écrit un config.toml par défaut pour ce dossier surveillé, puis quitte.",
    )
    parser.add_argument(
        "--diagnostic",
        action="store_true",
        help="Affiche l'état du registre et les réglages de ressources, puis quitte.",
    )
    parser.add_argument(
        "--purger-registre",
        action="store_true",
        help="Retire du registre les entrées dont le fichier n'existe plus, puis quitte.",
    )
    parser.add_argument(
        "--analyser-journal",
        action="store_true",
        help="Dépouille les journaux : combien de fois chaque PDF a été traité.",
    )
    args = parser.parse_args(argv)

    # Rendre les moteurs OCR embarqués (vendor/) visibles avant tout appel OCR.
    paths.configure_engines()

    if args.init_config is not None:
        cfg = write_default_config(args.init_config)
        print(f"Configuration écrite : {cfg}")
        return 0

    config_path = args.config or paths.default_config_path()
    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Erreur de configuration : {exc}", file=sys.stderr)
        return 2

    # Journal et fichier d'état dans un emplacement fiable en écriture.
    log_path = Path(config.log_file)
    if not log_path.is_absolute():
        log_path = paths.data_dir() / log_path
    state_path = log_path.with_name(".ocr_state.json")

    if args.analyser_journal:
        return _analyser_journal(log_path.parent)

    if args.diagnostic or args.purger_registre:
        return _diagnostic(config, state_path, purge=args.purger_registre)

    logger = setup_logging(log_path)
    logger.info("Démarrage de Scribe.")

    # Scribe travaille en tâche de fond : il s'efface devant les logiciels que
    # l'on utilise réellement. Les processus enfants (Tesseract, Ghostscript)
    # héritent de cette priorité.
    resources.lower_priority(config.priority)

    state = ProcessedStore(state_path)
    reporter = StatusReporter(paths.data_dir() / "status.json")
    service = OcrService(config, state, reporter, control_dir=paths.data_dir())

    def _handle_signal(signum, _frame):
        logger.info("Signal %s reçu, arrêt en cours...", signum)
        service.stop()
        reporter.stop()
        state.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass  # certains contextes (thread) n'autorisent pas les signaux

    if args.once:
        service.run_once()
        reporter.stop()
        state.stop()
        logger.info("Mode --once terminé.")
        return 0

    try:
        service.run()
    except KeyboardInterrupt:
        service.stop()
    finally:
        reporter.stop()
        state.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
