# DRSSTC Interrupter

Flask-Steuerung für einen Raspberry Pi mit pigpio: t_ON/t_OFF, Burst/BPS,
Einzelpulse und konvertierte MIDI-Dateien. Die angegebene Hardware ist eine
CM400DY-24NF-Fullbridge. CW wurde vollständig entfernt.

## Softwaregrenzen

Die gemeinsamen Grenzwerte in `signal_limits.py` sind:

- t_ON maximal **200 µs**;
- t_OFF mindestens **5 ms**, auch für MIDI;
- Duty Cycle maximal **1 %**.

Die vorhandenen Obergrenzen wurden nicht erhöht. Sie sind keine bestätigten
sicheren Betriebswerte für die Fullbridge. Zwischenkreisspannung, Spitzenstrom,
Gate-Ansteuerung, Überstromabschaltung und das gemessene Schaltverhalten müssen
vor einem Betrieb an der Anlage separat geprüft werden.

Die PWM-Frequenz wird zuerst auf ganze Hertz abgerundet. Anschließend wird der
Duty-Wert abgerundet, damit die rechnerische Pulsbreite nicht über die angeforderte
Einschaltzeit steigt. Die API meldet die tatsächlich gesetzten nominalen Werte
mit eindeutigen Einheiten zurück. Die Darstellung im Browser ist eine Berechnung,
kein Oszilloskop-Messsignal. Hardware-Taktquantisierung ist separat zu messen.

Unzulässige manuelle Einstellungen, fehlende Werte, NaN, Unendlich und Frequenzen
unter 1 Hz liefern HTTP 400, bevor der Ausgang geändert wird. Null als t_ON oder
Duty Cycle stoppt den Ausgang. Einzelpulse sind auch bei wiederholten Anfragen
und wechselnden Pulsbreiten begrenzt; zu frühe Anfragen liefern HTTP 429. Pulse
über 100 µs verwenden die Wave-API statt `gpio_trigger`.

## Betriebsmodi und Status

`output_controller.py` verwaltet alle Interrupter-Ausgaben mit einer gemeinsamen
Sperre. Ein Moduswechsel stoppt den bisherigen Ausgang und macht dessen
MIDI-Abbruchtoken dauerhaft ungültig. Alte Threads dürfen weder neue Pulse
abgeben noch den neuen Modus beim Beenden abschalten oder als Fehler markieren.
Vor einer neuen Ausgabe wird eine LOW-Pause für die vorherige und die folgende
Pulsbreite eingehalten, mindestens 5 ms. Identische MIDI-PWM-Einstellungen werden
nicht unnötig erneut gestartet.

- `GET /output_status`: aktueller Modus, nominale Einstellungen und Ausgangsfehler.
- `POST /stop_output`: alle Interrupter-Ausgaben stoppen; die Leistungsrelais bleiben
  dabei unverändert. Die Oberfläche bietet dafür **Ausgang STOP**.
- `POST /stop_midi`: nur stoppen, wenn MIDI aktuell den Ausgang besitzt. Ein
  verspäteter MIDI-STOP stoppt keinen bereits übernommenen Burst-Modus.
- `/burst_status` und `/playback_status` werden aus demselben Controller abgeleitet.
- JSON-Antworten auf angemeldete POST-Anfragen enthalten einen `output`-Status.

Die Oberfläche verwirft verspätete Statusantworten und Antworten eines alten
Serverstarts. GPIO-Fehler werden als `fault` angezeigt. Jeder Abschaltweg wird
versucht, selbst wenn ein anderer GPIO-Aufruf fehlschlägt. Im Produktivbetrieb
führt ein Ausgangsfehler außerdem zu Abschaltversuchen an beiden Leistungsrelais.
Nach wiederhergestellter GPIO-Verbindung kann ein ausdrücklicher STOP oder ein
neuer Modusauftrag den Fehlerzustand zurücksetzen.

## Anmeldung und Verbindungsüberwachung

Alle Steuerungs- und Statusrouten erfordern eine angemeldete Sitzung. Zustandsändernde
Browser-Anfragen benötigen zusätzlich einen CSRF-Token; fremde Origins werden
abgewiesen. Sitzungs-Cookies sind HttpOnly, SameSite=Strict und im TLS-Betrieb Secure.
Anmeldeversuche werden begrenzt. Ohne Anmeldekonfiguration bleibt die Steuerung
gesperrt. Nur Tests können den Zugriffsschutz ausdrücklich umgehen.

Die Oberfläche sendet jede Sekunde einen Heartbeat. Der erste gültige
Steuerungsauftrag übernimmt eine fünfsekündige Steuerungsfrist. Jeder Seitenstart
hat eine eigene zufällige Fensterkennung: Ein anderer Tab mit derselben Anmeldung
kann die Frist des steuernden Tabs nicht verlängern. Andere Fenster dürfen weiterhin
STOP und Power-OFF anfordern. Abmeldung des steuernden Fensters schaltet ab.

Nach Ablauf von **5 Sekunden** ohne Heartbeat werden Softstart, beide Relais und
Interrupter abgeschaltet. Ein verspäteter Heartbeat schaltet nichts wieder ein.
Fehlgeschlagene Abschaltversuche werden vom Watchdog erneut versucht. Der frühere
Ping einer fest eingetragenen Handy-IP wurde entfernt.

Das ist ein Software-Watchdog. Ein Prozessabsturz, SIGKILL oder eine ausgefallene
GPIO-Verbindung kann seine Abschaltung verhindern. Eine unabhängige elektrische
Abschaltung und Strombegrenzung kann damit nicht ersetzt werden.

## Leistungsrelais und Softstart

`POST /toggle_power` erwartet einen echten JSON-Boolean, etwa `{"power": true}`.
Der Softstart läuft im Hintergrund; Fortschritt liefert `/softstart_status`.
Wiederholtes Einschalten startet keine zweite Sequenz. Einschalten startet keine
Interrupter-Ausgabe und räumt alte Ausgaben vor der Vorladung auf.

`{"power": false}` bricht die Ladezeit ab, stoppt die Ausgaben und versucht beide
Relais abzuschalten. Alte Einschaltaufträge können danach keine Volllast aktivieren.
`power: true` im Status bedeutet, dass mindestens ein Leistungsrelais aktiv ist,
auch während des Softstarts; es bestätigt nicht, dass Volllast bereits bereit ist.

## MIDI-Bibliothek

Die Pfade sind unabhängig vom aktuellen Arbeitsverzeichnis. Nur einfache
Dateinamen im vorgesehenen Ordner werden akzeptiert. Absolute Pfade, Traversal,
Symlinks und nicht reguläre Dateien sind gesperrt. Größe und vollständige
5-Byte-Ereignisse werden vor dem Moduswechsel geprüft; die geprüften Bytes werden
an den Wiedergabethread übergeben, ohne die Datei erneut über ihren Namen zu öffnen.

Format: `<HBBB` = Verzögerung in ms, Ereignis, Note/Datenbyte, Velocity/Datenbyte.
NOTE_ON, NOTE_OFF und die in vorhandenen Dateien enthaltenen Pitch-Bend-Ereignisse
sind zulässig. Datei-Pitch-Bend behält seine Verzögerung, verändert aber keine
GPIO-Ausgabe. NOTE_ON mit Velocity 0 wird als NOTE_OFF behandelt.

MIDI-Pulsbreiten werden bei Bedarf für die Duty- und Pausengrenzen reduziert.
Nicht mögliche Frequenzen werden ausgelassen und die vorherige Note wird
stummgeschaltet. Ohne Transposition sind mit diesen Grenzen Töne ab G#3 nicht
verfügbar. Die Pitch-Steuerung unterstützt -12 bis +12 Halbtonschritte ab der
nächsten Note; auch danach gelten dieselben Pulsgrenzen. Eine Änderung der
maximalen MIDI-Einschaltzeit wird auf einen aktuell laufenden PWM-Ton angewendet.

Von den 87 vorhandenen Bibliotheksdateien sind 86 nicht leer und geprüft. Der
leere Platzhalter `TheEmptinessMachine` bleibt erhalten, wird aber nicht angeboten.
Dateiendungen bleiben bei Auswahl und Wiedergabe erhalten.

## Produktivstart vorbereiten

Unterstützt wird ausschließlich **ein GPIO-steuernder Prozess**. Waitress verwendet
mehrere Threads in diesem Prozess. Die Prozesssperre verhindert einen zweiten Start
mit demselben Sperrpfad, bevor er pigpio verbindet. Alle Starts müssen den gemeinsamen
Pfad `/run/drsstc/controller.lock` verwenden. Mehrere Gunicorn-Worker oder Flask-ReLoader
sind keine unterstützten Startmethoden. Der Import von `main.py` verbindet weder
GPIO noch startet er Hintergrundthreads.

Die folgenden Schritte sind eine Vorlage für den späteren Raspberry-Pi-Betrieb;
Dateien aus `deploy/` wurden nicht installiert und kein Dienst wurde aktiviert.

1. Projekt nach `/opt/drsstc` bereitstellen; Python 3.12 wurde getestet.
2. Virtuelle Umgebung erstellen und `python -m pip install -r requirements-resolved.txt`
   sowie `python -m pip check` darin ausführen.
3. Einen Dienstbenutzer `drsstc` und die Verzeichnisse `/var/lib/drsstc`,
   `/etc/drsstc` und `/run/drsstc` mit passenden Eigentümern einrichten.
4. Als Dienstbenutzer `python credentials.py /var/lib/drsstc/auth.json` ausführen.
   Das Passwort wird verdeckt zweimal abgefragt. Die Datei enthält nur Passwort-Hash
   und Sitzungsschlüssel, gehört dem Dienstbenutzer, ist privat und wird nicht
   überschrieben. Keine Zugangsdaten in Git speichern.
5. TLS-Namen und Zertifikate für den Reverse-Proxy festlegen. In
   `/etc/drsstc/environment` die tatsächliche `DRSSTC_PUBLIC_ORIGIN=https://…`
   eintragen. `deploy/environment.example` und `deploy/nginx.conf.example` sind
   anpassbare Vorlagen; Platzhalter müssen ersetzt werden.
6. Vor einem Hardwarestart gemeinsam den geprüften Stand, GPIO-Polarität und
   den unabhängigen Abschaltpfad an der Anlage abnehmen. Danach erst die
   Dienstvorlage `deploy/drsstc.service` übernehmen und aktivieren.

Der Dienst bindet nur `127.0.0.1:5000`; der Browser greift über den TLS-Proxy zu.
`--allow-local-http` ist eine ausdrückliche Ausnahme nur für lokalen Loopback-Zugriff,
nicht für HTTP über das LAN. Ein HTTPS-Ursprung und diese Ausnahme können nicht
kombiniert werden.

Beim ausdrücklichen Start werden Prozesssperre und Anmeldung geprüft, die lokale
pigpio-Verbindung geprüft, Relais auf AUS und Interrupter auf LOW vorbereitet und
erst danach die Ready-LED gesetzt. Kein Softstart oder Puls wird dabei ausgelöst.
SIGTERM, SIGINT, Serverfehler und normaler Programmabschluss führen zu Abschaltversuchen,
Schließen der pigpio-Verbindung und Freigabe der Prozesssperre. Vor einem Austausch
müssen alte GPIO-steuernde Dienste beendet sein.

`GET /health` meldet nur die Software-Bereitschaft. GPIO-Pegel und diese Bereitschaft
sind keine Messung der tatsächlichen Zwischenkreisspannung oder Relaiskontakte.

## Tests

```sh
python -m pip install -r requirements-resolved.txt
python -m unittest discover -s tests -v
```

Die Tests verwenden simulierte GPIOs, einschließlich Initialisierung, Shutdown,
Moduswechsel, Fristablauf, Fehlerfälle, Passwortprüfung, CSRF und Dateiprüfung.
Ein Test startet Waitress ausschließlich auf Loopback mit simulierten GPIOs und
prüft Anmeldung und gesperrte Hardwarezugriffe über echtes HTTP.

Mit Node.js wird zusätzlich das gerenderte Interface mit simuliertem DOM geprüft:
STOP, genau eine Single-Shot-Anfrage, CSRF-Header, Fensterkennung, Fehleranzeige,
verspätete Antworten und Serverneustart. Ohne Node.js wird nur dieser Test übersprungen.
`.github/workflows/tests.yml` bereitet diese Prüfungen für CI vor.

Historische Varianten mit abweichenden Pins und Relaispolaritäten liegen als
nicht ausführbare Referenzdateien in `archive/`; sie sind nicht Teil des Betriebs.
