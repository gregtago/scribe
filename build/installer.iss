; ---------------------------------------------------------------------------
; Installeur de Scribe (Inno Setup 6).
; Compilation : ISCC.exe build\installer.iss  (lancé depuis la racine du dépôt)
;
; Attendus au moment de la compilation :
;   ..\dist\scribe\   (sortie PyInstaller : scribe.exe + _internal)
;   ..\vendor\             (tesseract\, gs\  -> voir fetch-vendor.ps1)
;   nssm.exe               (dans ce dossier build\)
; ---------------------------------------------------------------------------

#define AppName "Scribe"
#define AppVersion "1.1.0"
#define AppPublisher "Grégoire Tagot"
#define ServiceName "Scribe"

[Setup]
AppId={{9F3B2C1A-7E4D-4B6A-9C2E-0A1B2C3D4E5F}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\Scribe
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=Scribe-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
PrivilegesRequired=admin
WizardStyle=modern
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\icon.ico
; Page de conditions générales d'utilisation à accepter pendant l'installation.
LicenseFile=..\assets\CGU.txt
AppPublisherURL=mailto:gregoiretagot@tagot.notaires.fr

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Files]
Source: "..\dist\scribe\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\dist\scribe-tray\*"; DestDir: "{app}\tray"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\vendor\*"; DestDir: "{app}\vendor"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "nssm.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\icon.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\CGU.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\assets\THIRD-PARTY-LICENSES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Démarrage automatique de l'icône de progression à l'ouverture de session.
Name: "{commonstartup}\Scribe"; Filename: "{app}\tray\scribe-tray.exe"
; Raccourcis dans le menu Démarrer.
Name: "{group}\Progression de Scribe"; Filename: "{app}\tray\scribe-tray.exe"
Name: "{group}\Journal de Scribe"; Filename: "{commonappdata}\Scribe\scribe.log"
Name: "{group}\Conditions d'utilisation"; Filename: "{app}\CGU.txt"
Name: "{group}\Licences des composants tiers"; Filename: "{app}\THIRD-PARTY-LICENSES.txt"
Name: "{group}\Désinstaller {#AppName}"; Filename: "{uninstallexe}"

[Run]
; 1. Génère la configuration par défaut (dossier surveillé choisi par l'utilisateur).
Filename: "{app}\scribe.exe"; Parameters: "--init-config ""{code:GetWatchDir}"""; \
    StatusMsg: "Configuration..."; Flags: runhidden waituntilterminated
; 2. Installe et configure le service Windows via NSSM.
Filename: "{app}\nssm.exe"; Parameters: "install {#ServiceName} ""{app}\scribe.exe"""; \
    Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppDirectory ""{app}"""; \
    Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} DisplayName ""{#AppName}"""; \
    Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} Description ""Reconnaissance de texte (OCR) automatique sur les PDF."""; \
    Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} Start SERVICE_AUTO_START"; \
    Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppExit Default Restart"; \
    Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppStdout ""{commonappdata}\Scribe\service-out.log"""; \
    Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppStderr ""{commonappdata}\Scribe\service-err.log"""; \
    Flags: runhidden waituntilterminated
; Rotation des fichiers de sortie du service. SANS ces trois réglages, NSSM
; écrit dans service-out.log / service-err.log SANS AUCUNE LIMITE : ces
; fichiers atteignaient plusieurs dizaines de méga-octets. On les fait tourner
; à 2 Mo, à chaud (AppRotateOnline), sans avoir à arrêter le service.
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppRotateFiles 1"; \
    Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppRotateOnline 1"; \
    Flags: runhidden waituntilterminated
Filename: "{app}\nssm.exe"; Parameters: "set {#ServiceName} AppRotateBytes 2000000"; \
    Flags: runhidden waituntilterminated
; 3. Démarre le service immédiatement.
Filename: "{app}\nssm.exe"; Parameters: "start {#ServiceName}"; \
    Flags: runhidden waituntilterminated
; 4. Lance l'icône de progression tout de suite (case à cocher en fin d'install).
Filename: "{app}\tray\scribe-tray.exe"; Description: "Afficher l'icône de progression maintenant"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/im scribe-tray.exe /f"; Flags: runhidden; RunOnceId: "KillTray"
Filename: "{app}\nssm.exe"; Parameters: "stop {#ServiceName}"; Flags: runhidden waituntilterminated; RunOnceId: "StopSvc"
Filename: "{app}\nssm.exe"; Parameters: "remove {#ServiceName} confirm"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveSvc"

[Code]
var
  WatchPage: TInputDirWizardPage;

procedure InitializeWizard;
begin
  WatchPage := CreateInputDirPage(wpSelectDir,
    'Dossier à surveiller',
    'Quel dossier contient vos fichiers PDF ?',
    'Le service surveillera ce dossier et TOUS ses sous-dossiers. Chaque PDF '
    + 'scanné (image) y sera automatiquement transformé en PDF texte '
    + 'recherchable, l''original étant remplacé.'#13#10#13#10
    + 'Sélectionnez le dossier racine, puis cliquez sur Suivant.',
    False, '');
  WatchPage.Add('');
  WatchPage.Values[0] := ExpandConstant('{userdocs}\PDF');
end;

function GetWatchDir(Param: String): String;
begin
  Result := WatchPage.Values[0];
end;
