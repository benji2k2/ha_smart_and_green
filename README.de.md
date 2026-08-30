<p align="center">
  <img src="custom_components/smartgreen_cube/brand/icon.png" width="128" alt="Smart & Green Cube" />
</p>

<h1 align="center">Smart &amp; Green Cube — Home Assistant Integration</h1>

<p align="center">
  Lokale Steuerung der <b>Smart &amp; Green „Cube"</b>-Leuchten (BLE-RGBW, Linkio-LMP)
  direkt aus Home Assistant – ohne Cloud, ohne Hersteller-Gateway.
</p>

<p align="center">
  <img alt="HACS Custom" src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg">
  <img alt="Home Assistant 2024.4+" src="https://img.shields.io/badge/Home%20Assistant-2024.4%2B-41BDF5.svg">
  <img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-green.svg">
</p>

<p align="center"><a href="README.md">English version</a></p>

---

Die Integration spricht das proprietäre **LMP (Linkio Mesh Protocol)** der App
„Smart & Green – Mesh" direkt über BLE. Die nötigen Mesh-Schlüssel liest sie
**einmalig aus dem verschlüsselten App-Export (`.lap`)** – kein Hex-Gefummel.
Die Steuerung läuft über den Bluetooth-Adapter des HA-Hosts oder einen
**ESPHome-Bluetooth-Proxy** in Funkreichweite der Cubes.

> Reverse-engineered für die private Interoperabilität eigener, gekaufter Hardware.
> Kein offizielles Produkt von Smart & Green / Linkio. Alle Marken gehören ihren Eigentümern.

## Features

- Ein `light`-Entity pro Cube: **An/Aus, Helligkeit, Farbe (HS), Farbtemperatur**
- **Quittierte Befehle** — der Cube bestätigt jeden Schaltvorgang, verlorene
  werden erkannt und wiederholt
- **Zustand übersteht Neustarts** (gespeicherter Zustand statt „alles aus")
- Optionales Gruppen-Entity **„Alle"** — ein Broadcast erreicht über das Mesh
  alle Cubes und braucht nur *eine* Verbindung, ist also deutlich schneller als
  einzelnes Schalten
- **Diagnosesensoren** je Cube: Signalstärke, zuletzt gesehen, verwendeter
  Proxy sowie die Modul-Schalter (Status-LED, Tastensperre, Tiefschlaf) aus der
  importierten Konfiguration — alles ohne je eine Verbindung aufzubauen
- Transparente Nutzung vorhandener **ESPHome-Bluetooth-Proxys**
- **Auto-Discovery** der Cubes per BLE-Advertisement (Company-ID `0x04AA`)
- Schlüssel-Import direkt aus dem `.lap`-Export der App

### Warum der erste Druck lange dauert

Mit der eigenen Zeitmessung der Integration nachgewiesen: **Das Warten auf ein
Advertisement ist nicht das Problem** — Home Assistant hatte in allen
beobachteten Fällen binnen 0,0 s ein verbindbares Gerät. Die gesamte Verzögerung
steckt im *Aufbau der Verbindung* über den Proxy.

Zwei Dinge machen ihn langsam, beide sind adressiert:

- **Konkurrierende Verbindungen.** Zwei kurz nacheinander geschaltete Cubes
  bauten bisher gleichzeitig zwei Verbindungen auf und nahmen sich gegenseitig
  die wenigen Slots des Proxys weg — ein Versuch verbrannte 127 s und war nach
  dem Rauswurf des anderen in 15 s erfolgreich. Verbindungen werden jetzt
  nacheinander aufgebaut, und ein Befehl, der die Verbindung des anderen Cubes
  offen vorfindet, läuft darüber.
- **Schwaches Signal.** Die Versuche liefen bei −87 und −96 dBm. Nahe dem
  Rauschpegel braucht eine Verbindung mehr Wiederholungen und bricht öfter ab,
  ein näherer Proxy hilft also.

  Spätere Messungen relativieren das: Mit serialisierten Verbindungen stand der
  Link in drei kalten Durchläufen nach 0,3–4,2 s — bei −90 bis −95 dBm. Auf
  diese Entfernungen war nicht das Signal die Ursache für träges Schalten.

  Beim Lesen der Signalstärke Vorsicht: Sie wird an *angekommenen*
  Advertisements gemessen. Durch ein Hindernis gehen die schwachen verloren und
  nur die zufällig starken werden gemessen — ein schlechter Link kann daher
  fast denselben Wert melden wie ein guter. Der Wert ist eine Untergrenze der
  Qualität, kein Maß dafür.

Was die Integration tut, damit es nicht stört:

- Die Anzeige schaltet **sofort** um, gesendet wird im Hintergrund — man muss
  nicht mehrfach drücken. Bestätigt der Cube den Befehl nicht, wird die Anzeige
  nachträglich zurückgesetzt.
- Die Verbindung bleibt standardmäßig **zwei Minuten** offen, damit
  Folgebefehle keinen neuen Verbindungsaufbau bezahlen. Einstellbar über
  *Konfigurieren* an der Integration: länger heißt, der nächste Befehl wirkt
  sofort; kürzer heißt, der Funk des Cubes schläft früher. `0` trennt direkt
  nach jedem Befehl.
- LMP ist ein Mesh: **jede** offene Verbindung kann einen Befehl an **jeden**
  Cube weiterreichen. Ist ein Cube bereits verbunden, laufen Befehle für den
  anderen darüber und sparen die Wartezeit komplett — genauso arbeitet die
  Hersteller-App.
- Schnelle Änderungen (Ziehen am Regler) werden zusammengefasst.

#### Warum der Aufbau lange dauert und Befehle dann schnell sind

Ein schwacher Link zeigt ein typisches Muster: Die erste Verbindung zieht sich,
danach ist jeder Befehl sofort da. Das ist keine Eigenheit dieser Integration,
sondern die Funktionsweise von BLE.

**Der Verbindungsaufbau muss auf Anhieb gelingen.** Der Proxy muss ein
Advertisement empfangen *und* seine Verbindungsanfrage muss den Cube binnen
150 µs auf demselben Kanal erreichen. Geht eines davon verloren, ist der ganze
Versuch hinfällig, bis das nächste Werbeereignis kommt. Advertising nutzt zudem
nur drei feste Kanäle, von denen einer mitten im WLAN-Kanal 6 liegt — es gibt
kein Ausweichen.

**In einer bestehenden Verbindung ist es umgekehrt.** BLE springt über 37
Datenkanäle und blendet schlechte laufend aus, und die Verbindungsschicht
wiederholt, bis ein Paket durchkommt. Ein Verlust kostet ein
Verbindungsintervall — Millisekunden — und keinen neuen Anlauf.

Auf einem Grenzlink braucht der Aufbau also mehrere Treffer **hintereinander**
(die Wahrscheinlichkeiten multiplizieren sich, daher die Minuten), ein Befehl
dagegen nur **einen** Treffer irgendwann. Zwei praktische Folgen: Eine lange
Haltezeit ist auf einem schlechten Link weit mehr wert als auf einem guten, und
das Weiterreichen über eine bestehende Verbindung umgeht den fragilen Teil
vollständig.

## Reichweite beachten

Die Cubes sind BLE-Geräte mit kleiner Antenne. Unter **−75 dBm** wird die
Verbindung unzuverlässig: Sie kommt oft noch zustande, bricht dann aber während
der Service-Discovery ab — das sieht wie ein sporadischer Softwarefehler aus,
ist aber schlicht Funkreichweite. Fünf Meter durch eine massive Wand reichen
erfahrungsgemäß **nicht**.

Der Sensor **Signalstärke** zeigt den aktuellen Wert, und unter −75 dBm warnt
die Integration im Protokoll. Abhilfe ist immer ein Bluetooth-Proxy näher am
Cube — Home Assistant wählt automatisch den mit dem besten Empfang.

### Was es (noch) nicht gibt

- **Kein Rücklesen von An/Aus und Farbe.** Der Zustand steht weder im
  Advertisement (dort nur der Netzwerkzustand), noch beantwortet die Firmware
  eine Abfrage: `DEVICE_DATA_GET` (0x42) wird mit einem undokumentierten Code
  abgelehnt. Die App bekommt den Zustand offenbar als Ereignis
  (`LMP_EVENT_DEVICE_DATA`, 0x92) — im Test war keines auszulösen. Der Zustand
  in Home Assistant ist deshalb *optimistic* (`assumed_state`).

  Dafür wird jeder Befehl **quittiert**: Der Cube bestätigt mit `STATUS_ACK`,
  ob er ihn ausgeführt hat. Ein verlorener Befehl fällt damit sofort auf und
  wird wiederholt, statt still zu scheitern.
- **Kein Akkustand.** Obwohl es Akkuleuchten sind, beantwortet die Firmware die
  Abfrage nicht: `LMP_COMMAND_MODULE_BATTERY_STATUS_GET` (0x2D) wird mit
  `LMP_ERR_NOT_SUPPORTED` quittiert, `0x2C` bleibt unbeantwortet, und die
  Antwort auf `MODULE_INFO_GET` enthält kein Akkufeld. Am Gerät geprüft mit
  Firmware 2.9.0 / API 4.

  Die Hersteller-App kann es ebenso wenig anzeigen: Sie hat einen Empfänger
  für ein Akku-Ereignis, das niemand sendet, und ihre Akku-Anzeige ist im
  Quelltext auskommentiert. LMP ist ein Plattform-Protokoll, das auch
  Batteriesensoren abdeckt — die Opcodes existieren, dieses Gerät setzt sie
  nur nicht um.

### Hinweis zu den Modul-Schaltern

Status-LED, Tastensperre und Tiefschlaf stammen aus der **importierten
Konfiguration** — einer Momentaufnahme vom Zeitpunkt des Exports. Die Cubes
senden diese Einstellungen nicht, und sie live abzufragen würde zusätzliche
Frames auf die Funkstrecke bringen; das unterlässt die Integration bewusst.
Jede Entität trägt ein `source`-Attribut, das darauf hinweist. Für die
aktuellen Werte auf Zuruf: `tools/ble_test.py props`.

Tiefschlaf ist absichtlich **kein** Schalter. Die App schaltet ihn nur ein, und
ein Cube im Tiefschlaf erwacht ausschließlich durch einen Tastendruck am Gerät
— eine fehlgeleitete Automatisierung würde die Lampe unerreichbar machen.

## Voraussetzungen

- Home Assistant **2024.4+** mit aktiver **Bluetooth**-Integration
- Bluetooth-Adapter am HA-Host **oder** ein ESPHome-Gerät mit
  `bluetooth_proxy` (aktive Verbindungen) in Reichweite der Cubes
- Cubes im PRIVATE-Key-Modus (Standard der App)

## Installation

**Über HACS (Custom Repository):**

1. HACS → Integrationen → ⋮ → *Benutzerdefinierte Repositories*
2. `https://github.com/benji2k2/ha_smart_and_green` als Kategorie **Integration** hinzufügen
3. „Smart & Green Cube" installieren → Home Assistant neu starten

**Manuell:** Ordner `custom_components/smartgreen_cube/` nach
`<config>/custom_components/` kopieren und HA neu starten.

## Einrichtung

1. In der **Smart-&-Green-App** die Konfiguration exportieren
   (erzeugt eine passwortgeschützte `.lap`-Datei) und aufs HA-Gerät bringen.
2. In HA: *Einstellungen → Geräte & Dienste → Integration hinzufügen →
   „Smart & Green Cube"*.
3. **„Konfigurationsdatei (.lap) importieren"** wählen, Datei hochladen und das
   **Export-Passwort** eingeben.
4. Die Integration entschlüsselt die Datei, liest Schlüssel + Cube-/Gruppenliste
   und legt die Lampen automatisch an.

Kein Export zur Hand? Über **„Schlüssel manuell eingeben"** lassen sich
`keyCrypt1` und `nounceAESCrypt` (je 16 Byte Hex) direkt eintragen; die Cubes
werden dann per BLE-Scan gefunden.

## Wie es funktioniert (Kurzfassung)

Kein Bluetooth-SIG-Mesh, sondern **LMP** von Linkio SAS. Steuer-Frames gehen als
GATT-Write auf Characteristic `00005002-0000-1000-8000-00805f9b34fb`
(Service `41c15000-…`). Der 16-Byte-Payload wird mit
`payload XOR AES128-ECB(keyCrypt1, nonce)` verschlüsselt. Das Motiv des
Integrations-Icons stammt aus der Original-App (RGBW-„Cube").

## Status

Protokoll, Verschlüsselung, Frame-Format und Advertisement-Aufbau sind **am
echten Gerät verifiziert**, ebenso die Steuerung im Alltag (An/Aus, Helligkeit,
Farbe, Farbtemperatur) über einen ESPHome-Proxy und der Gruppen-Broadcast an
`FF:FF`. Der Zustand ist *optimistic* — siehe „Was es (noch) nicht gibt".
Rückmeldungen/Issues willkommen.

## Sicherheit / Datenschutz

Die `.lap`-Datei und der extrahierte Schlüssel (`keyCrypt1` + `nonce`) sind
**Geheimnisse deiner Installation**. Sie werden **nie** in dieses Repository
committed (siehe `.gitignore`).

**Speicherung in Home Assistant:** Der Schlüssel wird – wie alle
Integrations-Geheimnisse in HA (WLAN-Passwörter, Tokens, API-Keys) – in der
Config-Entry unter `<config>/.storage/core.config_entries` als **Klartext-JSON**
abgelegt. Home Assistant verschlüsselt diese Daten **nicht** at-rest; das
Bedrohungsmodell setzt einen vertrauenswürdigen Host voraus.

Praktisch bedeutet das:

- Wer Dateisystem-Zugriff auf den HA-Host hat, kann den Schlüssel lesen.
- Der Schlüssel landet in **Backups** – aktiviere daher **verschlüsselte Backups**.
- Der Schlüssel wird nie geloggt und in **Diagnose-Downloads maskiert**
  (`diagnostics.py`).

Einordnung: Es handelt sich um ein **lokales BLE-Steuergeheimnis** – missbrauchbar
nur in Funkreichweite bzw. über dein Proxy-Netz, nicht als Cloud-Zugang. Derselbe
Schlüssel liegt ohnehin bereits im App-Export, auf dem Smartphone und in den Cubes.

## Tests

```bash
python3 tests/run.py
```

Keine Home-Assistant-Installation nötig, siehe [tests/README.md](tests/README.md).

## Icons

Das Integrations-Icon liegt im Repo unter
`custom_components/smartgreen_cube/brand/` (`icon.png` 256px, `icon@2x.png` 512px,
`logo.png`). Das Motiv ist das Herstellerlogo aus der Original-App.

## Lizenz

MIT – siehe [LICENSE](LICENSE).
