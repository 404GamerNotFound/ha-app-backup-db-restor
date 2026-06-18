# Backup DB Restore

Diese App stellt eine Ingress-UI bereit, mit der eine Home-Assistant-Recorder-
Datenbank aus einem Backup hochgeladen, analysiert und teilweise in die aktuelle
Instanz importiert werden kann.

## Konfiguration

| Option | Typ | Standard | Beschreibung |
| --- | --- | --- | --- |
| `log_level` | Liste | `info` | Log-Level fuer die App-Ausgabe. |
| `database_path` | String | `/homeassistant_config/home-assistant_v2.db` | Pfad zur Home-Assistant-Datenbank im Container. |
| `cache_path` | String | `/data/cache` | Beschreibbarer Cache-Pfad fuer geladene Quell-DB, Upload-Zwischenspeicher und temporaere Extraktionen. |
| `config_backup_path` | String | `/data/config-backups` | Beschreibbarer Pfad fuer selektive Home-Assistant-Konfigurationsbackups. |
| `max_upload_mb` | Integer | `131072` | Maximale Uploadgroesse in MB, entspricht 128 GB. |
| `create_current_db_backup` | Boolean | `true` | Erstellt vor einem Schreibimport eine SQLite-Sicherung der aktuellen DB unter `/data/current-db-backups`. |

## Einstellungsseite

Der Tab `Einstellungen` buendelt die allgemeinen App-Optionen in der Ingress-UI.
Bearbeitbar sind Datenbankpfad, Cache-Pfad, Konfig-Backup-Pfad, maximales
Upload-Limit, Log-Level und die automatische aktuelle-DB-Sicherung vor
Schreibimporten.

Die UI liest diese Werte ueber `GET /api/settings` und speichert Aenderungen mit
`POST /api/settings` atomar in `/data/options.json`. Waehrend ein Job laeuft,
werden Aenderungen mit `409 Conflict` abgelehnt, damit Cache- oder
Restore-Prozesse keine Pfade unter den Fuessen verlieren.

Technische Wirkung der Optionen:

- `database_path` wird beim naechsten Status-Refresh direkt fuer die Analyse der
  aktuellen Recorder-Datenbank verwendet.
- `config_backup_path` wird direkt fuer neue Konfig-Backups, Uploads und
  Restore-Vorschauen verwendet.
- `cache_path` wird beim App-Start in feste Laufzeitpfade wie `source.db`,
  `uploads/` und `tmp/` aufgeloest. Eine Aenderung wird gespeichert, aber erst
  nach einem App-/Add-on-Neustart aktiv.
- `log_level` wird vom Startskript gesetzt und ist ebenfalls erst nach Neustart
  voll wirksam.

Fuer `cache_path` und `config_backup_path` muss der Elternordner bereits
existieren. Das verhindert, dass ein nicht eingehangener USB-Stick unter
`/media/...` versehentlich als leerer lokaler Ordner interpretiert wird. Die
Einstellungsseite zeigt den konfigurierten Speicherort, den effektiven aktiven
Pfad, freien Speicher und einen Neustart-Hinweis fuer neustartpflichtige
Aenderungen an.

## Eingebundene Verzeichnisse

- `/backup` ist read-only eingebunden und fuer Home-Assistant-Backups gedacht.
- `/share` ist read-write eingebunden und kann fuer manuelle Artefakte genutzt werden.
- `/media` ist read-write eingebunden und kann fuer externe Medien bzw. USB-
  Speicher genutzt werden, wenn Home Assistant sie dort bereitstellt.
- `/homeassistant_config` ist read-write eingebunden, damit History-Daten in die
  aktuelle Recorder-Datenbank geschrieben werden koennen.
- `/data` speichert standardmaessig den Cache, Job-nahe Artefakte, Job-Status in
  `jobs.json`, Import-Reports und Sicherheitskopien persistent.

## Externer Cache-Pfad

Mit `cache_path` kann der Speicherort fuer grosse Quellartefakte verlegt werden.
Die App legt dort die Cache-Datenbank `source.db`, die Metadaten
`source_meta.json`, optionale Originaldateien sowie die Unterordner `uploads`
und `tmp` an. Dadurch landen auch grosse Browser-Uploads und temporaere
Backup-Extraktionen nicht mehr unter dem internen `/data`, sondern im
konfigurierten Cache-Verzeichnis.

Fuer USB- oder andere externe Speicher muss der Pfad innerhalb des Containers
sichtbar und beschreibbar sein, z. B. `/media/usb/backup-db-restore-cache` oder
`/share/backup-db-restore-cache`. Der Elternordner sollte bereits existieren,
damit die App nicht versehentlich einen fehlenden USB-Mount als normalen Ordner
anlegt. Die UI zeigt den aktiven Cache-Pfad und den dort freien Speicher unter
`Geladene Quelle` an.

## Konfig-Backups

Der Tab `Konfig-Backup` erstellt selektive Sicherungen aus
`/homeassistant_config`, ohne ein vollstaendiges Home-Assistant-Backup zu
erzeugen. Das Zielverzeichnis kommt aus `config_backup_path` und kann wie der
Cache auf `/data`, `/share` oder einem unter `/media` sichtbaren externen
Speicher liegen.

Auswaehlbar sind:

- `automations`: `automations.yaml` und `.storage/automation`
- `scripts`: `scripts.yaml` und `.storage/script`
- `scenes`: `scenes.yaml` und `.storage/scene`
- `blueprints`: `blueprints/`
- `dashboards`: `.storage/lovelace*`
- `helpers`: Entity-/Device-/Area-Registries und Helper-Speicher wie
  `.storage/input_*`, `counter`, `timer`, `schedule`, `group`, `person`, `zone`
- `configuration`: `configuration.yaml`, `customize.yaml`, `packages/`,
  `custom_templates/`
- `secrets`: `secrets.yaml`, nur wenn die Secrets-Checkbox explizit aktiv ist

Jedes Archiv ist ein `tar.gz` mit `manifest.json`. Das Manifest enthaelt
Erstellzeit, App-Version, ausgewaehlte Bereiche, fehlende optionale Dateien,
Dateigroessen und SHA256-Pruefsummen. Die Restore-Vorschau vergleicht diese
Pruefsummen mit der aktuellen Konfiguration und markiert Dateien als `same`,
`changed`, `new` oder `conflict`.

Vorhandene Konfig-Backup-Archive koennen im Tab heruntergeladen und spaeter
wieder hochgeladen werden. Uploads werden vor der Uebernahme geprueft: Die App
akzeptiert nur `tar.gz`-Archive mit Manifest, erwartet ausschliesslich
`manifest.json` und die darin gelisteten `config/...`-Dateien und verifiziert
Dateigroessen sowie SHA256-Pruefsummen.

Vor jedem Konfig-Restore erstellt die App automatisch ein Safety-Backup der
aktuell vorhandenen Dateien, die vom Restore ueberschrieben wuerden. Der Restore
schreibt nur Dateien aus dem Manifest zurueck und entfernt keine zusaetzlichen
aktuellen Dateien. Nach einem Restore ist ein Home-Assistant-Neustart empfohlen,
damit Automationen, Skripte, Dashboards und `.storage`-Daten konsistent neu
geladen werden.

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
Logzeilen, Ergebnis oder Fehler fuer mehrere Stunden in `/data/jobs.json`.
`/api/status` liefert den aktuellsten laufenden Job als `active_job`, sodass die
UI nach einem Seiten-Reload wieder an laufende Upload-Analysen,
Backup-Ladevorgaenge oder Rettungs-Jobs fuer defekte Recorder-DBs ankoppeln
kann. Wenn der Server selbst neu startet, werden zuvor laufende Jobs beim Start
als unterbrochen markiert, weil der Worker-Thread nicht fortgesetzt werden kann.

Laufende Upload-, Backup-, Corrupt-DB-, Cache-Refresh- und Import-Jobs koennen
ueber die UI oder `POST /api/jobs/{id}/cancel` kooperativ abgebrochen werden.
Die Worker pruefen das Abbruchsignal an sicheren Punkten, z. B. vor dem
Ersetzen des Quell-Caches und waehrend laengerer Import-Schleifen. Jobs, die den
Quell-Cache schreiben, werden nicht parallel zu anderen Cache-Schreibern oder
Import-Jobs gestartet; der Server antwortet in diesem Fall mit `409 Conflict`
und dem laufenden `active_job`.

Der Browser-Dateiupload selbst bleibt eine aktive HTTP-Verbindung. Ein Reload
waehrend der eigentlichen Dateiuebertragung bricht diese Verbindung ab; eine
echte Fortsetzung waehrend des Uploads benoetigt ein separates Chunk-/Resume-
Protokoll und eine erneute Dateiauswahl im Browser.

## Restore aktueller DB-Sicherungen

Vor Schreibimporten erzeugt die App, sofern `create_current_db_backup` aktiv ist,
eine SQLite-Sicherung der aktuellen Recorder-Datenbank unter
`/data/current-db-backups`.

Diese Sicherungen koennen in der UI angezeigt und zurueckgespielt werden. Vor dem
Restore wird nochmals eine Sicherung des aktuell vorhandenen DB-Stands erzeugt.
Nach einem Restore ist ein Home-Assistant-Neustart empfohlen, damit Recorder und
History sauber mit dem extern geaenderten DB-Zustand weiterarbeiten.

## Aktuelle DB-Diagnose und Wartung

Die Statuskarte `Aktuelle DB` nutzt nicht nur Dateiexistenz und Groesse, sondern
eine SQLite-Diagnose. Die UI zeigt im Bereich `Aktuelle DB-Diagnose` konkrete
Probleme und abgeleitete Loesungsvorschlaege an.

Erfasst werden unter anderem:

- `PRAGMA integrity_check(20)` und `PRAGMA quick_check(20)`
- `PRAGMA foreign_key_check`
- `journal_mode`, `page_count`, `freelist_count`, `schema_version`, `user_version`
- vorhandene Sidecar-Dateien `home-assistant_v2.db-wal`,
  `home-assistant_v2.db-shm` und `home-assistant_v2.db-journal`
- lesbare Tabellen, State-/Statistik-Zahlen und Lesefehler

Die UI bietet dazu drei Aktionen:

- `Neu pruefen`: laedt `/api/status` erneut und aktualisiert die Diagnose.
- `Snapshot erstellen`: startet den Job `snapshot_current_db`. Dabei wird per
  SQLite Backup API eine konsistente Sicherung der aktuellen DB unter
  `/data/current-db-backups` erzeugt und separat analysiert.
- `WAL-Checkpoint`: startet nach Bestaetigung den Job `checkpoint_current_db` mit
  Modus `PASSIVE`. Das kann helfen, wenn eine grosse `-wal`-Datei vorhanden ist
  oder die Anzeige durch ausstehende WAL-Daten irritiert. Der passive Modus ist
  bewusst konservativ und soll aktive Schreibzugriffe nicht aggressiv blockieren.

Wenn `integrity_check` echte Korruption meldet, ersetzt der WAL-Checkpoint keine
vollstaendige Reparatur. In diesem Fall ist ein Restore aus einem bekannten guten
Home-Assistant-Backup normalerweise die sicherste Loesung.

## API-Uebersicht

- `PUT /api/upload?async=1`: speichert den Upload und startet einen Analyse-Job.
- `POST /api/jobs`: startet Jobs fuer `load_backup`, `refresh_cache`, `import`,
  `restore_current_db`, `snapshot_current_db`, `checkpoint_current_db`,
  `config_backup` und `restore_config_backup`.
- `GET /api/jobs/{id}`: liefert Status, Fortschritt, Log, Ergebnis oder Fehler.
- `POST /api/jobs/{id}/cancel`: fordert den kooperativen Abbruch eines laufenden
  Jobs an.
- `GET /api/status`: liefert App-Status inklusive `active_job`, falls ein Job
  gerade laeuft oder noch in der Warteschlange steht.
- `POST /api/import/preview`: fuehrt eine read-only Vorabpruefung aus.
- `GET /api/mapping/suggestions`: liefert Ziel-Entity-Vorschlaege.
- `GET /api/current-db-backups`: listet automatisch erzeugte aktuelle-DB-Sicherungen.
- `GET /api/reports` und `GET /api/reports/{id}`: listen und lesen Import-Reports.
- `GET /api/config-backups`: listet selektive Konfig-Backup-Archive.
- `GET /api/config-backups/{id}`: liest Manifest und Metadaten eines Konfig-Backups.
- `GET /api/config-backups/{id}/preview`: vergleicht ein Konfig-Backup mit der aktuellen Konfiguration.
- `GET /api/config-backups/{id}/download`: laedt ein Konfig-Backup-Archiv herunter.
- `PUT /api/config-backups/upload`: speichert ein hochgeladenes Konfig-Backup-Archiv nach Manifest- und Pruefsummenvalidierung.
- `GET /api/settings`: liefert allgemeine Optionen, effektive Laufzeitpfade,
  Speicherinformationen und neustartpflichtige Aenderungen.
- `POST /api/settings`: validiert und speichert allgemeine Optionen in
  `/data/options.json`; aktive Jobs blockieren die Aenderung mit `409 Conflict`.

## Sicherheitshinweise

1. Vor produktivem Import sollte ein Home-Assistant-Backup vorhanden sein.
2. Wenn die aktuelle Datenbank gesperrt ist, bricht der Import ab.
3. Bei sehr grossen Datenbanken kann die Analyse laenger dauern.
4. Bei Schema-Unterschieden zwischen alter und aktueller Home-Assistant-Version
   kann ein Import fehlschlagen.
