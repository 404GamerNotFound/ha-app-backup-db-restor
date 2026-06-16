# Home Assistant Backup DB Restore App Repository

Dieses Repository enthaelt ein Grundgeruest fuer eine Home Assistant App
(frueher Add-on genannt).

## Apps

- [`backup_db_restore`](backup_db_restore/README.md) - Ingress-App zum Analysieren
  von Home-Assistant-Backups und zum Import von Recorder-History mit optionalen
  Long-Term-Statistics.

## Lokale Entwicklung

Die App kann in Home Assistant als lokales App-Repository getestet werden. Kopiere
den App-Ordner in das lokale App-Verzeichnis von Home Assistant oder fuege dieses
Git-Repository in Home Assistant unter **Settings > Apps > App store** als
Repository hinzu.

Fuer einen schnellen Docker-Build ausserhalb von Home Assistant:

```sh
cd backup_db_restore
docker build -t local/backup-db-restore .
```

Die App kann SQLite-Recorder-Datenbanken oder vollstaendige Home-Assistant-
Backups lesen, Entitaeten anzeigen, Import-Dry-Runs ausfuehren und History-Daten
in die aktuelle Recorder-Datenbank importieren. Vor Schreibimporten wird eine
Sicherung der aktuellen DB erzeugt; diese Sicherungen koennen in der UI wieder
zurueckgespielt werden.
