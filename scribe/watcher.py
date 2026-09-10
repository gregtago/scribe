"""Surveillance du dossier et file d'attente de traitement.

- Un scan initial océrise les PDF déjà présents.
- Un observateur (watchdog) détecte les nouveaux fichiers.
- Une nouvelle analyse périodique sert de filet de sécurité.
- Un unique thread de travail traite les PDF les uns après les autres
  (l'OCR est gourmand : inutile de saturer la machine).
- Les fichiers déposés en cours de route (détectés en direct) sont prioritaires
  sur le gros lot initial : un PDF ajouté maintenant passe devant la file.
- L'avancement est publié via un StatusReporter (lu par l'app de la barre des
  tâches).

Ménagement des ressources
-------------------------
La scrutation (polling) parcourt RÉCURSIVEMENT tout le dossier surveillé à
chaque tour. Sur une arborescence d'étude synchronisée sur OneDrive, un tour
par seconde — le réglage par défaut de watchdog — fait travailler le disque en
permanence, même quand il n'y a rien à faire. L'intervalle est donc explicite
et réglable (``poll_interval``), et l'analyse complète de sécurité est
nettement plus espacée.

Par ailleurs, chaque PDF océrisé est réécrit sur place : cette réécriture est
elle-même vue comme une modification par l'observateur. Sans précaution, le
fichier revient en file, ce qui gonfle les compteurs et fait tourner le service
à vide. Deux garde-fous s'en chargent : une fenêtre de grâce après nos propres
écritures, et le contrôle du registre AVANT la mise en file.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from . import control
from .config import Config
from .processor import process_pdf
from .state import ProcessedStore
from .status import StatusReporter

logger = logging.getLogger("scribe.watcher")

# Priorités de file (plus petit = traité en premier).
PRIORITY_LIVE = 0      # fichier déposé/détecté en direct
PRIORITY_SCAN = 1      # fichier trouvé lors d'un scan complet

# Durée pendant laquelle on ignore les événements portant sur un fichier que
# Scribe vient lui-même de réécrire (la synchronisation OneDrive peut répercuter
# l'écriture avec un peu de retard).
_SELF_WRITE_GRACE = 60.0


def _is_candidate(path: Path, config: Config) -> bool:
    """Filtre : PDF uniquement, hors dossier de sauvegarde et fichiers temp."""
    if path.suffix.lower() != ".pdf":
        return False
    if path.name.startswith("ocr_") or path.name.endswith(".tmp"):
        return False
    if config.backup_dir and config.backup_dir in path.parts:
        return False
    return True


class _Handler(FileSystemEventHandler):
    """Met en file d'attente chaque PDF créé ou modifié (en priorité)."""

    def __init__(self, enqueue, config: Config) -> None:
        self._enqueue = enqueue
        self._config = config

    def _consider(self, path_str: str) -> None:
        path = Path(path_str)
        if _is_candidate(path, self._config):
            self._enqueue(path, PRIORITY_LIVE)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._consider(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._consider(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._consider(event.dest_path)


class OcrService:
    """Orchestrateur : observateur + file d'attente + thread de travail."""

    def __init__(
        self,
        config: Config,
        state: ProcessedStore,
        reporter: StatusReporter | None = None,
        control_dir=None,
    ) -> None:
        self._config = config
        self._state = state
        self._reporter = reporter
        self._control_dir = control_dir  # dossier contenant le drapeau de pause
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()
        self._self_writes: dict[str, float] = {}
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None

    # -- mise en file d'attente ------------------------------------------
    def _recently_written_by_us(self, key: str) -> bool:
        """Vrai si nous venons nous-mêmes de réécrire ce fichier."""
        now = time.monotonic()
        with self._pending_lock:
            # Purge au passage : ce dictionnaire ne doit pas grossir sans fin.
            expired = [
                k for k, t in self._self_writes.items()
                if now - t > _SELF_WRITE_GRACE
            ]
            for k in expired:
                del self._self_writes[k]
            written_at = self._self_writes.get(key)
        return written_at is not None and now - written_at <= _SELF_WRITE_GRACE

    def enqueue(self, pdf: Path, priority: int = PRIORITY_LIVE) -> bool:
        """Ajoute un PDF à la file, sauf s'il n'y a manifestement rien à faire.

        Renvoie True si le fichier a effectivement été mis en file.

        Le chemin canonique n'est résolu qu'UNE fois puis transporté avec la
        tâche : sur un lecteur réseau ou un dossier synchronisé, ``resolve()``
        coûte un accès disque, et il était auparavant refait quatre fois par
        fichier.
        """
        try:
            key = str(pdf.resolve())
        except OSError:
            return False

        if self._recently_written_by_us(key):
            return False
        # Contrôle AVANT mise en file : évite de faire tourner le worker (et
        # d'incrémenter les compteurs) pour un fichier déjà traité et inchangé.
        if self._state.is_processed(pdf, key):
            return False

        with self._pending_lock:
            if key in self._pending:
                return False
            self._pending.add(key)
        self._queue.put((priority, next(self._seq), pdf, key))
        if self._reporter:
            self._reporter.on_enqueued()
        return True

    # -- attente de stabilité --------------------------------------------
    def _wait_until_stable(self, pdf: Path) -> bool:
        """Attend que la taille du fichier ne bouge plus (copie terminée)."""
        last = -1
        stable_for = 0.0
        step = 1.0
        deadline = time.monotonic() + 120  # abandon après 2 min
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                size = pdf.stat().st_size
            except OSError:
                return False
            if size == last and size > 0:
                stable_for += step
                if stable_for >= self._config.stable_seconds:
                    return True
            else:
                stable_for = 0.0
                last = size
            # Attente interruptible : l'arrêt du service est immédiat.
            if self._stop.wait(step):
                return False
        return False

    # -- pause ------------------------------------------------------------
    def _is_paused(self) -> bool:
        return self._control_dir is not None and control.is_paused(self._control_dir)

    # -- boucle de travail -----------------------------------------------
    def _work_loop(self) -> None:
        paused_announced = False
        while not self._stop.is_set():
            # En pause : on ne retire rien de la file, on attend la reprise.
            if self._is_paused():
                if not paused_announced and self._reporter:
                    self._reporter.set_paused(True)
                    paused_announced = True
                self._stop.wait(1.0)
                continue
            if paused_announced and self._reporter:
                self._reporter.set_paused(False)
                paused_announced = False

            try:
                _prio, _seq, pdf, key = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            status = "ignoré"
            if self._reporter:
                self._reporter.on_start(pdf)
            try:
                if not pdf.exists():
                    status = "disparu"
                    continue
                if self._state.is_processed(pdf, key):
                    status = "déjà fait"
                    continue
                if not self._wait_until_stable(pdf):
                    status = "instable"
                    logger.debug("Fichier instable ou disparu : %s", pdf)
                    continue
                if self._state.is_processed(pdf, key):
                    status = "déjà fait"
                    continue
                processed = process_pdf(pdf, self._config)
                status = "ok" if processed else "ignoré"
                if processed:
                    # Le fichier vient d'être réécrit par nos soins : on neutralise
                    # l'événement de modification que cela va provoquer.
                    with self._pending_lock:
                        self._self_writes[key] = time.monotonic()
                # On ne mémorise QUE si le traitement s'est terminé sans erreur
                # (succès ou saut délibéré). Un échec n'est pas marqué -> réessayé.
                self._state.mark(pdf, key)
            except Exception:  # noqa: BLE001 - un fichier ne doit pas tuer le service
                status = "erreur"
                logger.exception("Erreur inattendue sur %s", pdf)
            finally:
                with self._pending_lock:
                    self._pending.discard(key)
                if self._reporter:
                    self._reporter.on_done(pdf, status)
                self._queue.task_done()

    # -- scan complet -----------------------------------------------------
    def scan_all(self) -> None:
        count = 0
        seen = 0
        for pdf in self._config.watch_dir.rglob("*.pdf"):
            if self._stop.is_set():
                return
            if not _is_candidate(pdf, self._config):
                continue
            seen += 1
            if self.enqueue(pdf, PRIORITY_SCAN):
                count += 1
        if count:
            logger.info(
                "Scan : %d fichier(s) mis en file d'attente sur %d PDF examinés.",
                count, seen,
            )
        else:
            logger.debug("Scan : rien à faire (%d PDF examinés).", seen)

    # -- cycle de vie -----------------------------------------------------
    def _make_observer(self):
        if self._config.use_polling:
            # L'intervalle est EXPLICITE : la valeur par défaut de watchdog
            # (1 seconde) relit toute l'arborescence chaque seconde.
            return PollingObserver(timeout=self._config.poll_interval)
        return Observer()

    def run(self) -> None:
        logger.info("Surveillance du dossier : %s", self._config.watch_dir)
        logger.info(
            "Ressources : %d cœur(s) pour l'OCR, priorité « %s », "
            "scrutation toutes les %.0f s, analyse complète toutes les %.0f s.",
            self._config.effective_jobs, self._config.priority,
            self._config.poll_interval, self._config.rescan_seconds,
        )
        self._worker = threading.Thread(
            target=self._work_loop, name="scribe-worker", daemon=True
        )
        self._worker.start()

        # Entretien du registre au démarrage : les entrées dont le fichier a
        # disparu s'accumulaient indéfiniment et alourdissaient chaque écriture.
        self._state.prune()

        # Scan initial des fichiers déjà présents.
        self.scan_all()

        handler = _Handler(self.enqueue, self._config)
        observer = self._make_observer()
        observer.schedule(handler, str(self._config.watch_dir), recursive=True)
        observer.start()
        logger.info(
            "Service démarré (%s).",
            "polling" if self._config.use_polling else "événements natifs",
        )

        next_rescan = time.monotonic() + self._config.rescan_seconds
        try:
            while not self._stop.is_set():
                if self._config.rescan_seconds <= 0:
                    self._stop.wait(60.0)
                    continue
                # Attente unique jusqu'à la prochaine analyse : plus de réveil
                # du processus chaque seconde pour ne rien faire.
                delay = max(1.0, next_rescan - time.monotonic())
                if self._stop.wait(delay):
                    break
                # Une analyse complète pendant que la file est encore pleine
                # ne sert à rien : elle relit l'arborescence pour rien.
                if self._queue.empty():
                    self._state.prune()
                    self.scan_all()
                next_rescan = time.monotonic() + self._config.rescan_seconds
        finally:
            observer.stop()
            observer.join(timeout=5)
            self._state.flush()
            logger.info("Service arrêté.")

    def run_once(self) -> None:
        """Traite les PDF présents puis s'arrête (sans surveillance)."""
        self._worker = threading.Thread(
            target=self._work_loop, name="scribe-worker", daemon=True
        )
        self._worker.start()
        self.scan_all()
        # Attente de la fin de la file, mais SANS blocage définitif : si le
        # thread de travail venait à mourir (moteur OCR défaillant, par
        # exemple), un simple queue.join() attendrait indéfiniment.
        # On surveille ``unfinished_tasks`` — le compteur sur lequel join()
        # s'appuie — et non ``empty()`` : un fichier retiré de la file n'est
        # pas pour autant traité, il ne l'est qu'au task_done() du worker.
        while self._queue.unfinished_tasks > 0:
            if not self._worker.is_alive():
                logger.error(
                    "Le thread de traitement s'est interrompu : "
                    "arrêt du mode --once."
                )
                break
            time.sleep(0.2)
        self.stop()
        self._worker.join(timeout=5)
        self._state.flush()

    def stop(self) -> None:
        self._stop.set()
