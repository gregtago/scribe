# Journal des versions — Scribe

Toutes les évolutions notables de Scribe sont consignées ici.
Format inspiré de [Keep a Changelog](https://keepachangelog.com/fr/).

## [1.1.0] — 2026-09-10

Version consacrée à la consommation de ressources. Scribe faisait travailler
le poste beaucoup plus que nécessaire, y compris lorsqu'il n'avait rien à
faire.

### Corrigé
- **Boucle de retraitement.** Le registre des fichiers déjà traités ne
  reposait que sur la taille et la date de modification. Or la synchronisation
  OneDrive, les sauvegardes et les antivirus retouchent la date sans toucher
  au contenu : Scribe croyait le fichier modifié, le réocérisait, ce qui
  changeait réellement sa date, et le même PDF repartait indéfiniment au
  traitement. Le registre compare désormais aussi l'**empreinte du contenu**,
  et répare son entrée au lieu de relancer l'OCR. C'est ce qui expliquait que
  le nombre de fichiers à traiter augmente sans qu'aucun document n'arrive.
- **Compteurs faussés.** Un fichier seulement contrôlé était compté comme un
  fichier traité. Les PDF réellement océrisés, ceux déjà à jour et ceux en
  erreur sont maintenant distingués.
- **Ré-analyse d'un PDF que Scribe vient lui-même de réécrire** : l'écriture
  du résultat déclenchait un nouveau tour de file pour rien.

### Ajouté
- Réglage **`jobs`** : nombre de cœurs accordés à l'OCR. Par défaut la moitié
  des cœurs — auparavant OCRmyPDF les prenait **tous**, et le poste se figeait
  pendant le traitement d'un gros PDF.
- Réglage **`priority`** (`basse` par défaut, `inactive`, `normale`) : abaisse
  la priorité processeur et les entrées/sorties du service. Tesseract et
  Ghostscript en héritent.
- Réglage **`poll_interval`** : intervalle de scrutation du dossier.
- Commandes **`--diagnostic`** (réglages appliqués et état du registre) et
  **`--purger-registre`**.

### Modifié
- **Contrôle préalable avant OCR** : si toutes les pages portent déjà du texte,
  Scribe s'arrête en quelques millisecondes au lieu de lancer Tesseract et
  Ghostscript, et surtout ne réécrit pas le fichier — donc ne relance pas la
  synchronisation OneDrive.
- **Scrutation du dossier toutes les 15 s** au lieu d'une fois par seconde
  (valeur par défaut de watchdog, jamais explicitée). Chaque tour relisant
  toute l'arborescence, c'était la cause principale du disque qui travaillait
  en permanence.
- **Analyse complète toutes les 30 min** au lieu de 5, et seulement quand la
  file d'attente est vide.
- Le registre est écrit en **format compact**, de façon **groupée**, et ses
  entrées obsolètes sont **purgées** au démarrage : il était réécrit en entier
  à chaque fichier et grossissait sans fin.
- L'app de la barre des tâches ne redessine plus sa fenêtre chaque seconde
  quand celle-ci est masquée ou que rien n'a changé.
- Des **tests de non-régression** (`tests/test_ressources.py`) figent ces
  garde-fous et tournent à chaque compilation. Ils simulent le moteur OCR et
  ne demandent donc ni Tesseract ni Ghostscript.

[1.1.0]: https://github.com/gregtago/scribe/releases/tag/v1.1.0

## [1.0.0] — 2026-07-11

Première version publiée. Scribe est un logiciel Windows qui océrise
automatiquement, en tâche de fond, les PDF scannés d'un dossier.

### Fonctionnalités
- **Service Windows** (`Scribe`) qui surveille un dossier et tous ses
  sous-dossiers et transforme les PDF « image » en PDF **texte recherchable**
  (moteur OCRmyPDF / Tesseract). L'original est **remplacé** sur place.
- **Installeur `.exe` autonome** : Tesseract (français) et Ghostscript sont
  embarqués, aucun prérequis à installer. L'assistant demande le dossier à
  surveiller et installe/démarre le service.
- **Icône dans la barre des tâches** avec **fenêtre de progression** : nombre
  de PDF traités / restants, fichier en cours, barre d'avancement et liste des
  derniers fichiers traités.
- **Priorité aux nouveaux fichiers** : un PDF déposé passe devant le lot de
  fichiers déjà présents au démarrage.
- Traitement **idempotent** (jamais de double OCR), remplacement **atomique**,
  redressement et rotation automatiques des pages, **journal** détaillé.
- Compilation automatisée et **auto-testée** sur Windows (GitHub Actions).

### Notes
- L'installeur n'est pas signé numériquement : Windows affiche un avertissement
  SmartScreen à la première exécution (voir le README, section « Installeur non
  signé »).

[1.0.0]: https://github.com/gregtago/pdfpdf/releases/tag/v1.0.0
