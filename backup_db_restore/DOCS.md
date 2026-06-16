# Backup DB Restore

Diese App stellt eine Ingress-UI bereit, mit der eine Home-Assistant-Recorder-
Datenbank aus einem Backup hochgeladen, analysiert und teilweise in die aktuelle
Instanz importiert werden kann.

## Konfiguration

| Option | Typ | Standard | Beschreibung |
| --- | --- | --- | --- |
| `log_level` | Liste | `info` | Log-Level fuer die App-Ausgabe. |
| `database_path` | String | `/homeassistant_config/home-assistant_v2.db` | Pfad zur Home-Assistant-Datenbank im Container. |
| `max_upload_mb` | Integer | `131072` | Maximale Uploadgroesse in MB, entspricht 128 GB. |
| `create_current_db_backup` | Boolean | `true` | Erstellt vor einem Schreibimport eine SQLite-Sicherung der aktuellen DB unter `/data/current-db-backups`. |

## Eingebundene Verzeichnisse

- `/backup` ist read-only eingebunden und fuer Home-Assistant-Backups gedacht.
- `/share` ist read-write eingebunden und kann fuer manuelle Artefakte genutzt werden.
- `/homeassistant_config` ist read-write eingebunden, damit History-Daten in die
  aktuelle Recorder-Datenbank geschrieben werden koennen.
- `/data` speichert Upload-Cache, Job-nahe Artefakte, Import-Reports und
  Sicherheitskopien persistent.

## Importverhalten

Die App importiert rohe State-History aus der Tabelle `states`. Optional kann sie
zusaetzlich Long-Term-Statistics aus `statistics_meta`, `statistics_short_term`
und `statistics` fuer dasselbe Quell-/Ziel-Mapping uebernehmen. Beim Import muss
eine Quell-Entitaet aus der hochgeladenen Datenbank und eine Ziel-Entitaet aus
der aktuellen Instanz ausgewaehlt werden.

Der Import schreibt keine vollstaendige Home-Assistant-Sicherung zurueck. Er
uebernimmt State-Zeilen, mappt `metadata_id` auf die Ziel-Entitaet und kopiert
referenzierte `state_attributes`, soweit beide Datenbankschemata kompatibel sind.
Bereits vorhandene Zielzeilen mit gleichem Zeitstempel werden standardmaessig
uebersprungen.

Beim Statistikimport wird `statistics_meta.statistic_id` auf die Ziel-Entitaet
gemappt. Bereits vorhandene Statistik-Zeilen mit gleicher Startzeit werden
uebersprungen.

Vor jedem Schreibimport kann die UI eine Vorabpruefung ausfuehren. Diese prueft
Quelle, Ziel, Entity-Mapping, relevante Tabellen und fuehrt einen Dry-Run mit den
aktuellen Importoptionen aus. Das Ergebnis zeigt erwartete neue, uebersprungene
und ersetzte Zeilen an.

Der Import kann optional auf ein Zeitfenster begrenzt werden. Die UI sendet
Start- und Endzeit als ISO-Zeitstempel; Tabellen mit `*_ts`-Spalten werden
numerisch gefiltert, Tabellen mit Text-Zeitspalten per ISO-String.

Fuer Duplikate stehen zwei Strategien bereit:

- `skip`: Zielzeilen mit gleicher Ziel-Entitaet und gleichem Zeitstempel bleiben
  erhalten und werden uebersprungen.
- `replace`: Nur Zielzeilen mit gleicher Ziel-Entitaet und gleichem Zeitstempel
  werden vor dem Einfuegen entfernt und dadurch ersetzt.

Jeder Pruef- oder Schreibimport kann als JSON-Report unter
`/data/import-reports` gespeichert werden. Die UI zeigt die letzten Reports an,
inklusive Quell-/Ziel-Entitaet und importierten Zeilen.

Vollstaendige Home-Assistant-Backups koennen hochgeladen werden, solange sie als
Tar-Archiv vorliegen. Die App durchsucht das Archiv und verschachtelte
`tar`/`tar.gz`/`tgz`-Dateien nach einer SQLite-Datenbank, bevorzugt nach
`home-assistant_v2.db`.

Alternativ kann in der UI ein bereits auf dem Home-Assistant-Geraet gespeichertes
Backup aus `/backup` ausgewaehlt werden. Die App liest dieses Backup read-only,
extrahiert nur die gefundene Recorder-Datenbank in den Cache und fuehrt danach
dieselben Analyse- und Importaktionen aus wie bei einem Upload.

Die Entitaetenliste wird serverseitig paginiert und gefiltert, damit auch grosse
Recorder-Datenbanken mit vielen Entitaeten bedienbar bleiben.

Auch die Liste gespeicherter Backups aus `/backup` wird serverseitig paginiert
und gefiltert. Dadurch bleiben Systeme mit vielen Backup-Dateien bedienbar.

## Beschaedigte Quell-Datenbanken

Die App arbeitet beim Lesen hochgeladener oder aus Backups extrahierter
Quell-Datenbanken im Best-Effort-Modus. Wenn `PRAGMA integrity_check` Fehler
meldet oder einzelne Tabellenbereiche nicht lesbar sind, wird die Datenbank nicht
automatisch verworfen, solange der SQLite-Header gueltig ist und lesbare Tabellen
vorhanden sind.

Technisch werden Lesefehler pro Bereich gesammelt:

- `read_errors` enthaelt die betroffenen Tabellen oder Pruefschritte.
- `partial: true` kennzeichnet Analyse-, Entity- oder Importergebnisse, die nur
  aus den lesbaren Bereichen stammen.
- `source_warnings` wird in Import-Reports uebernommen, wenn die Quelle
  Integritaetswarnungen hatte.

Entity- und Statistiklisten verwenden Metadaten-Tabellen wie `states_meta` und
`statistics_meta` als Fallback. Wenn ein aggregierter Scan ueber `states`,
`statistics_short_term` oder `statistics` fehlschlaegt, koennen dadurch trotzdem
noch vorhandene Entitaeten angezeigt und gezielt importiert werden.

Grenze dieses Modus: Wenn SQLite eine defekte Datenpage waehrend eines Scans
meldet, kann die App den Scan an dieser Stelle sauber beenden und bereits
gelesene Zeilen verwenden. Sie kann aber keine physisch nicht mehr lesbaren
Zeilen rekonstruieren.

Bei Uploads zeigt die UI den Browser-Uploadfortschritt an. Fuer serverseitige
Aktionen wie Backup-Extraktion, Cache-Aktualisierung und Import wird ein
Arbeitsstatus mit Ablauf-Log angezeigt. Serverseitige Langlaeufer werden als Jobs
ausgefuehrt und ueber `/api/jobs/{id}` gepollt. Jobs halten Status, Fortschritt,
Logzeilen, Ergebnis oder Fehler fuer mehrere Stunden im Speicher.

## Restore aktueller DB-Sicherungen

Vor Schreibimporten erzeugt die App, sofern `create_current_db_backup` aktiv ist,
eine SQLite-Sicherung der aktuellen Recorder-Datenbank unter
`/data/current-db-backups`.

Diese Sicherungen koennen in der UI angezeigt und zurueckgespielt werden. Vor dem
Restore wird nochmals eine Sicherung des aktuell vorhandenen DB-Stands erzeugt.
Nach einem Restore ist ein Home-Assistant-Neustart empfohlen, damit Recorder und
History sauber mit dem extern geaenderten DB-Zustand weiterarbeiten.

## API-Uebersicht

- `PUT /api/upload?async=1`: speichert den Upload und startet einen Analyse-Job.
- `POST /api/jobs`: startet Jobs fuer `load_backup`, `refresh_cache`, `import`
  und `restore_current_db`.
- `GET /api/jobs/{id}`: liefert Status, Fortschritt, Log, Ergebnis oder Fehler.
- `POST /api/import/preview`: fuehrt eine read-only Vorabpruefung aus.
- `GET /api/mapping/suggestions`: liefert Ziel-Entity-Vorschlaege.
- `GET /api/current-db-backups`: listet automatisch erzeugte aktuelle-DB-Sicherungen.
- `GET /api/reports` und `GET /api/reports/{id}`: listen und lesen Import-Reports.

## Sicherheitshinweise

1. Vor produktivem Import sollte ein Home-Assistant-Backup vorhanden sein.
2. Wenn die aktuelle Datenbank gesperrt ist, bricht der Import ab.
3. Bei sehr grossen Datenbanken kann die Analyse laenger dauern.
4. Bei Schema-Unterschieden zwischen alter und aktueller Home-Assistant-Version
   kann ein Import fehlschlagen.
