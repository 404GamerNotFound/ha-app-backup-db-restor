# Backup DB Restore

Backup DB Restore ist eine experimentelle Home Assistant App fuer Analyse,
Zwischenspeicherung und Import von Recorder-History aus Home-Assistant-Backups.

Die App stellt eine Ingress-UI bereit. Dort kann eine SQLite-Datenbank, ein
Home-Assistant-Backup-Archiv oder ein bereits auf dem Geraet gespeichertes Backup
analysiert, zwischengespeichert und per Entitaetsmapping in die aktuelle Instanz
importiert werden.

## Funktionen

- Uploads bis 128 GB und direkte Auswahl gespeicherter `/backup`-Dateien.
- Konfigurierbarer Cache-Pfad, z. B. auf einem unter `/media` oder `/share`
  eingebundenen USB-/externen Speicher.
- Einstellungsseite fuer allgemeine Optionen wie DB-Pfad, Cache-Pfad,
  Konfig-Backup-Ziel, Upload-Limit, Log-Level und automatische DB-Sicherung.
- Selektive Konfig-Backups fuer Automationen, Skripte, Szenen, Blueprints,
  Dashboards, Helpers und optionale Secrets.
- Rekursive Suche nach der Recorder-Datenbank in vollstaendigen HA-Backups.
- Datenbank-Integrity-Check, Entity-Liste mit Paging und Filter.
- Import von State-History mit Quell-/Ziel-Entity-Mapping.
- Optionaler Import von Long-Term-Statistics.
- Vorabpruefung, Zeitfenster, Duplikatstrategie und Mapping-Vorschlaege.
- Serverseitige Jobs mit persistiertem Fortschritt, Ablauf-Log und Abbruch.
- Automatische aktuelle-DB-Sicherungen, Restore-Funktion und Import-Reports.

## Dateien

- `config.yaml` beschreibt die App fuer den Home Assistant Supervisor.
- `Dockerfile` baut den Container auf Basis von `ghcr.io/home-assistant/base`.
- `run.sh` ist der Einstiegspunkt der App.
- `app.py` enthaelt Webserver, Analyse und Importlogik.
- `web/` enthaelt die statische Ingress-UI.
- `DOCS.md` dokumentiert Optionen, Mounts und naechste Schritte.
