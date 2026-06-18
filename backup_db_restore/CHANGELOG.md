# Changelog

## 0.5.12

- Bereich `Top Speicherfresser` in der aktuellen DB-Entity-Ansicht optisch
  ueberarbeitet. Lange Entity-IDs werden nun einzeilig mit Ellipsis und
  Tooltip angezeigt, damit die Karten nicht mehr zeichenweise umbrechen.
- Top-Speicherfresser-Karten haben jetzt einen `Purge`-Shortcut. Der Button
  waehlt die Entity aus und laedt direkt die Purge-Vorschau; geloescht wird
  weiterhin erst nach explizitem Klick auf `Purge ausfuehren`.

## 0.5.11

- Neuer Tab `Einstellungen` ergaenzt. Allgemeine App-Optionen wie
  Datenbankpfad, Cache-Pfad, Konfig-Backup-Pfad, Upload-Limit, Log-Level und
  automatische aktuelle-DB-Sicherung koennen direkt in der Ingress-UI verwaltet
  werden.
- Neue Endpunkte `GET /api/settings` und `POST /api/settings` lesen und
  speichern die allgemeinen Optionen atomar in `/data/options.json`.
- Die Einstellungsseite zeigt freien Speicher fuer den konfigurierten
  Cache-Pfad und das Konfig-Backup-Ziel an. Aenderungen am Cache-Pfad und
  Log-Level werden als neustartpflichtig markiert.
- Pfadvalidierung ergaenzt: Fuer Cache- und Konfig-Backup-Ziele muss der
  Elternordner existieren, damit ein fehlender USB-/Media-Mount nicht
  versehentlich als lokaler Ordner angelegt wird.

## 0.5.10

- Neuer dritter Tab `Konfig-Backup` fuer selektive Sicherungen von
  Automationen, Skripten, Szenen, Blueprints, Dashboards, Helpers/Registries,
  Paketen und optional `secrets.yaml`.
- Neue Supervisor-Option `config_backup_path` ergaenzt. Konfig-Backups koennen
  dadurch getrennt vom DB-Cache z. B. unter `/data`, `/share` oder `/media`
  abgelegt werden.
- Konfig-Backup-Archive enthalten ein `manifest.json` mit App-Version,
  ausgewaehlten Bereichen, fehlenden optionalen Dateien, Dateigroessen und
  SHA256-Pruefsummen.
- Restore-Vorschau fuer Konfig-Backups ergaenzt. Sie vergleicht Manifest-
  Pruefsummen mit der aktuellen Konfiguration und zeigt neue, geaenderte,
  unveraenderte oder konfliktbehaftete Dateien.
- Konfig-Backup-Archive koennen jetzt im Tab heruntergeladen und wieder
  hochgeladen werden. Uploads werden vor der Uebernahme per Manifest,
  erlaubten Archivpfaden, Dateigroessen und SHA256-Pruefsummen validiert.
- Vor jedem Konfig-Restore erstellt die App automatisch ein Safety-Backup der
  aktuell vorhandenen Dateien, die ueberschrieben werden.

## 0.5.9

- Neue Supervisor-Option `cache_path` ergaenzt. Die App legt dort
  `source.db`, `source_meta.json`, optionale Originaldateien sowie die
  Unterordner `uploads` und `tmp` ab.
- `/media` wird read-write eingebunden, damit Cache-Verzeichnisse auf externen
  bzw. USB-Speichern genutzt werden koennen, wenn Home Assistant sie dort
  bereitstellt.
- `/api/status` und die UI zeigen den aktiven Cache-Pfad und den freien
  Speicher am Cache-Ziel an.

## 0.5.8

- Grosse Quell-Datenbanken werden beim automatischen Laden ab 2 GB nur noch per
  Schnellanalyse geprueft. Teure Vollpruefungen wie Integrity-Checks,
  globale Entity-Gruppierungen und grosse Tabellen-Scans werden dadurch nicht
  mehr direkt nach dem Zwischenspeichern erzwungen.
- Der Status-Endpunkt verwendet die beim Laden gespeicherten Cache-Metadaten,
  statt `source.db` bei jedem Refresh erneut zu analysieren.
- Job-Ergebnisse enthalten keine komplette Entity-Liste mehr. Stattdessen werden
  nur Zaehler und Metadaten persistiert, damit `/data/jobs.json` und Ingress-
  Antworten auch bei 33-GB-Datenbanken klein bleiben.
- Die Quell-Entity-Liste liest nun echte Seiten aus `states_meta` und
  `statistics_meta`, anstatt zuerst alle Entitaeten der Quell-DB zu laden.
- Vorabpruefung und Import pruefen die ausgewaehlte Source-Entity per gezieltem
  SQL-Existenzcheck, statt alle Entities in den Speicher zu laden.
- Die aktuelle Home-Assistant-Entity-Liste nutzt die Core-API als primaere
  Quelle; ein Recorder-DB-Fallback wird nur noch verwendet, wenn die API keine
  Entities liefert.
- Grosse lokale Corrupt-DBs werden im Rettungs-Cache per Hardlink/Symlink
  eingebunden, wenn moeglich. Dadurch muss eine 33-GB-Datei nicht noch einmal
  nach `/data/tmp` kopiert werden.
- SQLite-Lesezugriffe verwenden einen `immutable`-Fallback, wenn normales
  Read-only-Oeffnen mit generischen Fehlern wie `Load failed` scheitert.

## 0.5.7

- Corrupt-DB-Auswahl im Import-Tab um Filter und Paging erweitert, damit viele
  `*.corrupt` Dateien besser handhabbar sind.
- Upload-Zeile zeigt jetzt direkt den ausgewaehlten Dateinamen und die Groesse.
- Neue Detailkarte `Geladene Quelle` zeigt Typ, Name, Pfad, Archiv-Mitglied,
  Sidecars, Warnungen und Cache-Zeit der aktuell geladenen Quell-DB.
- Importformular zeigt eine Bereitschaftsanzeige mit den noch fehlenden
  Angaben, bevor eine Vorabpruefung oder ein Schreibimport gestartet wird.
- Mapping-Vorschlaege zeigen nun einen Leerzustand und enthalten Tooltip-Grund
  fuer passende Ziel-Entitaeten.
- Der Webserver-Port kann fuer lokale Tests per `BACKUP_DB_RESTORE_PORT`
  ueberschrieben werden; im Add-on bleibt der Standard `8099`.

## 0.5.6

- Lokale Home-Assistant-Corrupt-Dateien im Verzeichnis der konfigurierten
  Recorder-DB werden erkannt, z. B.
  `home-assistant_v2.db.corrupt.<timestamp>`.
- Passende `-wal.corrupt.<timestamp>`, `-shm.corrupt.<timestamp>` und
  `-journal.corrupt.<timestamp>` Sidecars werden beim Laden eines
  Rettungskandidaten mit in den Quell-Cache kopiert.
- Corrupt-DBs koennen im Tab `Backup & Import` ueber `Defekte Recorder-DBs`
  als Quelle geladen werden. Danach greifen die bestehende Entitaetenliste,
  Vorabpruefung, History-Import und Long-Term-Statistics-Import wie bei einem
  Backup.
- Die Quell-Statuskarte heisst nun `Geladene Quell-DB` und zeigt, ob die
  Quelle aus Upload, Backup oder defekter DB stammt.
- Wenn die Sidecars das Lesen verhindern, versucht die App automatisch einen
  Fallback nur mit der Hauptdatenbank.
- Beim Leeren oder Ersetzen des Quell-Caches werden auch alte
  `source.db-wal`, `source.db-shm` und `source.db-journal` Dateien entfernt.
- Laufende Jobs werden ueber `/api/status` als `active_job` gemeldet, damit die
  UI nach einem Seiten-Reload den Fortschritt und das Ablauf-Log wieder
  aufgreifen kann.
- Job-Metadaten werden in `/data/jobs.json` persistiert. Nach einem
  Server-Neustart werden zuvor laufende Jobs als unterbrochen markiert.
- Upload-, Backup-, Corrupt-DB-, Cache-Refresh- und Import-Jobs koennen jetzt
  kooperativ abgebrochen werden.
- Quell-Cache-Schreiber und Import-Jobs werden serverseitig gegeneinander
  gesperrt, damit kein paralleler Job den geladenen Source-Cache ersetzt.

## 0.5.5

- UI in zwei Arbeitsbereiche geteilt: `Aktuelle DB-Analyse` fuer die bestehende
  Home-Assistant-Recorder-Datenbank und `Backup & Import` fuer Upload,
  Backup-Auswahl, Quell-Entitaeten, Mapping, Import und Reports.
- Aktuelle Datenbankinformationen, Probleme, Loesungsvorschlaege,
  WAL-/Snapshot-Aktionen und aktuelle DB-Sicherungen sind nun gebuendelt im
  Analyse-Tab sichtbar.
- Backup-Status, Backup-Entitaeten, Cache-Aktionen und Import-Reports sind nun
  gebuendelt im Import-Tab sichtbar.
- Status-Badge oben unterscheidet nun eine intakte aktuelle DB ohne geladene
  Backup-Quelle von einem komplett importbereiten Zustand.

## 0.5.4

- Statusbereich eindeutiger gemacht: `Aktuelle Entitaeten` und
  `Backup-Entitaeten` werden nun als getrennte Karten angezeigt.
- Die Backup-Karte zeigt `Keine Backup-DB geladen`, solange keine Quell-DB
  analysiert wurde.
- Die aktuelle Karte zeigt dauerhaft die Entitaeten der aktuellen Instanz bzw.
  aktuellen Recorder-Datenbank.

## 0.5.3

- Statuskarte `Entitaeten` korrigiert: Wenn keine Quell-/Backup-DB geladen ist,
  zeigt die Karte nun die Entitaeten der aktuellen Home-Assistant-Instanz bzw.
  aktuellen Recorder-DB statt `0`.
- Sobald eine Backup-DB geladen ist, wechselt die Karte auf
  `Backup-Entitaeten` und zeigt zusaetzlich die Anzahl aktueller Entitaeten an.

## 0.5.2

- Aktuelle-DB-Diagnose in der UI ergaenzt. Die Statuskarte zeigt nun nicht nur
  `Nicht in Ordnung`, sondern eine eigene Diagnosekarte mit konkreten Problemen,
  Loesungsvorschlaegen und JSON-Details.
- Analyse der aktuellen DB erweitert: `quick_check`, `integrity_check`,
  `foreign_key_check`, `journal_mode`, Page-/Freelist-Zaehler, Schema-Version,
  User-Version und Sidecar-Dateien (`-wal`, `-shm`, `-journal`) werden erfasst.
- Empfehlungen werden aus den Diagnosewerten abgeleitet, z. B. WAL-Checkpoint,
  Snapshot/Sicherung, Home-Assistant-Neustart oder Restore aus HA-Backup.
- Job `snapshot_current_db` ergaenzt: erstellt eine konsistente SQLite-Sicherung
  der aktuellen DB und analysiert diese separat.
- Job `checkpoint_current_db` ergaenzt: fuehrt nach Bestaetigung einen passiven
  WAL-Checkpoint aus und analysiert die DB danach erneut.

## 0.5.1

- Best-Effort-Lesen fuer beschaedigte Quell-Datenbanken ergaenzt.
- Integrity-Fehler oder Lesefehler einzelner Tabellen brechen Analyse, Cache und
  Entity-Liste nicht mehr komplett ab, solange die SQLite-Datei grundsaetzlich
  lesbar ist.
- Entity- und Statistiklisten verwenden Metadaten-Tabellen als Fallback, wenn
  aggregierte Scans ueber `states`, `statistics_short_term` oder `statistics`
  fehlschlagen.
- Import von lesbaren Bereichen ist weiterhin moeglich. Teilimporte enthalten
  `partial`, `source_warnings` und `read_errors` im Ergebnis/Report.
- UI zeigt lesbare, aber defekte Quellen als `Teilweise lesbar` mit Warnungszahl
  statt als kompletten Abbruch an.

## 0.5.0

- Serverseitige Job-API fuer Upload-Analyse, Backup-Laden, Cache-Refresh, Import
  und Restore ergaenzt. Die UI pollt `/api/jobs/{id}` fuer Fortschritt, Status,
  Ergebnis und Logausgabe.
- Import-Reports ergaenzt. Jeder Pruef- oder Schreibimport kann als JSON unter
  `/data/import-reports` nachvollzogen und in der UI angezeigt werden.
- Restore-Funktion fuer automatisch erzeugte aktuelle-DB-Sicherungen ergaenzt.
  Vor dem Zurueckspielen wird erneut eine Sicherung des aktuellen Stands erzeugt.
- Vorabpruefung fuer Importe ergaenzt: Datenbankzustand, Entity-Mapping,
  Schema-Kompatibilitaet, Statistik-Tabellen und erwartete Importzaehler werden
  vor dem Schreibimport geprueft.
- Mapping-Vorschlaege fuer Ziel-Entitaeten ergaenzt. Exakte Treffer, gleiche
  Domain und aehnliche Objekt-IDs werden priorisiert.
- Serverseitiges Paging und Filtern fuer die gespeicherten `/backup`-Dateien
  ergaenzt.
- Zeitfenster-Import fuer State-History und Long-Term-Statistics ergaenzt.
- Duplikatstrategie ergaenzt: vorhandene Ziel-Zeitstempel koennen uebersprungen
  oder gezielt ersetzt werden.

## 0.4.3

- Fortschrittsanzeige fuer Datei-Uploads ergaenzt.
- Ablauf-Log fuer Upload, Backup-Laden, Cache-Aktionen und Import ergaenzt.
- Indeterminierter Arbeitsstatus fuer serverseitige Backup-Extraktion und Analyse
  ergaenzt.

## 0.4.2

- Standard-Uploadlimit auf 128 GB (`131072` MB) angehoben.
- Supervisor-Optionsschema erlaubt nun `max_upload_mb` bis `131072`.

## 0.4.1

- Docker-Build repariert: nicht vorhandenes Alpine-Paket `py3-sqlite3`
  entfernt. Das Python-`sqlite3`-Modul wird ueber `python3` bereitgestellt.

## 0.4.0

- Geraete-Backups aus dem gemounteten `/backup`-Verzeichnis koennen direkt in
  der UI ausgewaehlt und analysiert werden.
- Vollstaendige Home-Assistant-Backups werden weiterhin rekursiv nach der
  Recorder-Datenbank durchsucht.
- Serverseitiges Paging und Filtern fuer Backup-Entitaeten ergaenzt.

## 0.3.0

- Optionalen Import von Long-Term-Statistics ergaenzt.
- Statistik-Mapping fuer `statistics_meta`, `statistics_short_term` und `statistics`
  anhand Quell-/Ziel-Entitaet ergaenzt.
- Doppelte Statistik-Slots werden anhand `metadata_id` und Startzeit uebersprungen.
- UI zeigt Statistik-Zeilen pro Entitaet an.
- Upload-Auswahl um `.backup` und `.tar.gz` fuer vollstaendige Home-Assistant-Backups erweitert.

## 0.2.0

- Web-UI mit Home-Assistant-Ingress ergaenzt.
- Upload und Zwischenspeicher fuer SQLite-Datenbanken und Home-Assistant-Backup-Archive ergaenzt.
- Datenbankpruefung mit SQLite-Integrity-Check ergaenzt.
- Entitaetenanalyse fuer hochgeladene Recorder-Datenbanken ergaenzt.
- Import-Workflow fuer Recorder-State-History mit Quell-/Ziel-Entitaetsmapping ergaenzt.
- Aktuelle Home-Assistant-Datenbank wird vor Schreibimport optional gesichert.

## 0.1.0

- Initiales Home-Assistant-App-Grundgeruest.
- Konfiguration fuer Dry-Run, Backup-Datei, Datenbankpfad und Log-Level.
- Platzhalter-Startskript ohne produktive Restore-Logik.
