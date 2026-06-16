# Changelog

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
