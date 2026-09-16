; Установщик Pickline (Inno Setup 6). Собирается в CI:
;   ISCC.exe /DAppVersion=<версия> /DFileVersion=<x.x.x.x> installer\pickline.iss
;
; Зачем установщик, если есть один exe: скачанный файл Windows метит
; «из интернета», и при каждом запуске показывает «Вы уверены, что хотите
; открыть?», а SmartScreen - «Windows защитила ваш ПК». Файлы, записанные
; установщиком, метки не несут - предупреждений при запуске нет вообще,
; SmartScreen остаётся только один раз, на сам установщик.
;
; Ставится в профиль пользователя без прав администратора; данные
; приложения живут отдельно, в %LOCALAPPDATA%\Pickline (см. app/paths.py),
; и при обновлении или удалении не трогаются.

#ifndef AppVersion
  #define AppVersion "0.0"
#endif
#ifndef FileVersion
  #define FileVersion "0.0.0.0"
#endif

[Setup]
AppId={{7C2E7D2A-6B7B-4E3E-9C1B-3F5A1D2E9B01}
AppName=Pickline
AppVersion={#AppVersion}
AppVerName=Pickline {#AppVersion}
AppPublisher=Pickline
AppPublisherURL=https://github.com/Dtkek/pickline
AppSupportURL=https://github.com/Dtkek/pickline/issues
AppUpdatesURL=https://github.com/Dtkek/pickline/releases/tag/latest
VersionInfoVersion={#FileVersion}
DefaultDirName={localappdata}\Programs\Pickline
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=pickline-setup
SetupIconFile=..\app\icon.ico
UninstallDisplayIcon={app}\pickline.exe
UninstallDisplayName=Pickline
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"

[Files]
Source: "..\dist\pickline.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Pickline"; Filename: "{app}\pickline.exe"
Name: "{autodesktop}\Pickline"; Filename: "{app}\pickline.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\pickline.exe"; Description: "Запустить Pickline"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; лог пишется рядом с exe при каждом запуске
Type: files; Name: "{app}\pickline.log"
