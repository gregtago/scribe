"""Suivi des fichiers déjà traités (évite les doublons et les boucles).

Pourquoi ce registre est délicat
--------------------------------
Le dossier surveillé est synchronisé (OneDrive). Or la synchronisation, les
sauvegardes et les antivirus **touchent la date de modification** des fichiers
sans en changer le contenu. Un registre fondé sur la seule date de
modification déclare alors le fichier « modifié », Scribe le réocérise, ce qui
change réellement sa date... et la boucle est bouclée : le même PDF est
retraité indéfiniment, en pure perte.

La signature retenue est donc à deux étages :

1. **Voie rapide** — taille + date de modification identiques : rien à faire,
   aucune lecture du fichier.
2. **Voie de secours** — la taille est identique mais la date a bougé : on
   calcule l'empreinte du contenu. Si elle correspond, le fichier n'a pas
   changé, seule sa date a été retouchée : on **répare** l'entrée du registre
   et on n'océrise pas. C'est ce qui casse la boucle.

Une taille différente signifie un vrai changement : aucune empreinte n'est
calculée, le fichier repart à l'OCR.

Le fichier JSON est écrit en format compact et de façon groupée (au plus une
écriture toutes les quelques secondes), et les entrées dont le fichier a
disparu sont purgées : sans cela le registre grossit sans fin et chaque PDF
traité le réécrit en entier.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger("scribe.state")

_HASH_CHUNK = 1024 * 1024   # 1 Mio


class ProcessedStore:
    """Petit registre persistant (JSON) des fichiers déjà traités."""

    def __init__(
        self,
        path: str | Path,
        flush_interval: float = 3.0,
        flush_every: int = 200,
    ) -> None:
        self._path = Path(path)
        self._flush_interval = flush_interval
        self._flush_every = flush_every
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._dirty = False
        self._unsaved = 0
        self._stop = threading.Event()
        self._load()
        self._thread = threading.Thread(
            target=self._flush_loop, name="scribe-state", daemon=True
        )
        self._thread.start()

    # -- chargement / écriture -------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Registre illisible, reconstruction : %s", self._path)
            return
        if isinstance(data, dict):
            self._data = data

    def _write_locked(self) -> None:
        """Écrit le registre. Le verrou doit être détenu par l'appelant."""
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            # Format compact (pas d'indentation) : le registre d'une étude
            # compte des milliers d'entrées, l'indentation triplait sa taille.
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            tmp.replace(self._path)
            self._dirty = False
            self._unsaved = 0
        except OSError:
            logger.warning("Écriture du registre impossible : %s", self._path)

    def _flush_loop(self) -> None:
        while not self._stop.wait(self._flush_interval):
            with self._lock:
                if self._dirty:
                    self._write_locked()

    def flush(self) -> None:
        """Force l'écriture immédiate (arrêt du service)."""
        with self._lock:
            if self._dirty:
                self._write_locked()

    def stop(self) -> None:
        self._stop.set()
        self.flush()

    # -- signatures -------------------------------------------------------
    @staticmethod
    def _stat_signature(pdf: Path) -> dict:
        st = pdf.stat()
        return {"size": st.st_size, "mtime": int(st.st_mtime)}

    @staticmethod
    def _content_hash(pdf: Path) -> str:
        h = hashlib.sha256()
        with pdf.open("rb") as fh:
            for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
                h.update(chunk)
        return h.hexdigest()

    # -- interrogation ----------------------------------------------------
    def is_processed(self, pdf: Path, key: str | None = None) -> bool:
        """Vrai si ce PDF a déjà été traité et que son contenu n'a pas changé."""
        key = key or str(pdf.resolve())
        with self._lock:
            known = self._data.get(key)
        if known is None:
            return False

        try:
            sig = self._stat_signature(pdf)
        except OSError:
            return False

        if sig["size"] != known.get("size"):
            return False                      # vrai changement : à retraiter
        if sig["mtime"] == known.get("mtime"):
            return True                       # voie rapide : rien n'a bougé

        # Même taille, date différente : la synchronisation (ou un antivirus)
        # a probablement retouché le fichier sans en changer le contenu.
        known_hash = known.get("sha256")
        if not known_hash:
            # Entrée d'une version antérieure, sans empreinte : on l'enrichit
            # sans réocériser, le contenu ayant toutes les chances d'être bon.
            self._store(key, {**sig, "sha256": self._safe_hash(pdf)})
            return True

        current = self._safe_hash(pdf)
        if current is not None and current == known_hash:
            logger.debug(
                "Date de modification retouchée mais contenu identique, "
                "aucun retraitement : %s", pdf,
            )
            self._store(key, {**sig, "sha256": known_hash})   # réparation
            return True
        return False

    def _safe_hash(self, pdf: Path) -> str | None:
        try:
            return self._content_hash(pdf)
        except OSError:
            return None

    # -- enregistrement ---------------------------------------------------
    def _store(self, key: str, sig: dict) -> None:
        with self._lock:
            self._data[key] = sig
            self._dirty = True
            self._unsaved += 1
            if self._unsaved >= self._flush_every:
                self._write_locked()

    def mark(self, pdf: Path, key: str | None = None) -> None:
        """Enregistre la signature actuelle du fichier comme « traité »."""
        key = key or str(pdf.resolve())
        try:
            sig = self._stat_signature(pdf)
        except OSError:
            return
        sig["sha256"] = self._safe_hash(pdf)
        self._store(key, sig)

    # -- entretien --------------------------------------------------------
    def prune(self) -> int:
        """Retire les entrées dont le fichier n'existe plus. Renvoie le nombre."""
        with self._lock:
            keys = list(self._data)
        gone = [k for k in keys if not Path(k).exists()]
        if not gone:
            return 0
        with self._lock:
            for k in gone:
                self._data.pop(k, None)
            self._dirty = True
            self._write_locked()
        logger.info("Registre : %d entrée(s) obsolète(s) purgée(s).", len(gone))
        return len(gone)

    def stats(self) -> dict:
        """Chiffres du registre, pour le diagnostic."""
        # On copie sous verrou, puis on interroge le disque en dehors : sur un
        # gros registre, tester l'existence de chaque fichier prendrait le
        # verrou pendant plusieurs secondes.
        with self._lock:
            snapshot = dict(self._data)
        entries = len(snapshot)
        missing = sum(1 for k in snapshot if not Path(k).exists())
        without_hash = sum(1 for v in snapshot.values() if not v.get("sha256"))
        try:
            size = self._path.stat().st_size
        except OSError:
            size = 0
        return {
            "path": str(self._path),
            "size_bytes": size,
            "entries": entries,
            "missing": missing,
            "without_hash": without_hash,
        }
