# Smart & Green Cube — Home Assistant Integration

Steuert **Smart & Green „Cube"**-Leuchten (BLE-RGBW, Hersteller-Stack *Linkio LMP*)
direkt aus Home Assistant – **ohne Cloud, ohne Gateway**, über den Bluetooth-Adapter
des HA-Hosts oder einen **ESPHome-Bluetooth-Proxy**.

Die Integration spricht das proprietäre LMP-Protokoll der App
„Smart & Green – Mesh" nach. Die dafür nötigen Mesh-Schlüssel werden **einmalig aus
dem verschlüsselten App-Export (`.lap`) gelesen** – du musst nichts von Hand ausrechnen.

> Reverse-engineered für die private Interoperabilität eigener, gekaufter Hardware.
> Kein offizielles Produkt von Smart & Green / Linkio.

## Features

- Ein `light`-Entity pro Cube (An/Aus, Helligkeit, Farbe/HS)
- Optional ein Gruppen-Entity „Alle" (Broadcast an alle Cubes)
- Läuft transparent über vorhandene **ESPHome-Bluetooth-Proxys**
- Auto-Discovery: Cubes werden per BLE-Advertisement erkannt

## Installation

**Über HACS (Custom Repository):**
1. HACS → Integrationen → ⋮ → *Benutzerdefinierte Repositories*
2. `https://github.com/benji2k2/ha_smart_and_green` als *Integration* hinzufügen
3. „Smart & Green Cube" installieren, Home Assistant neu starten

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

## Voraussetzungen

- Home Assistant 2024.4+ mit aktiver **Bluetooth**-Integration
- Ein Bluetooth-Adapter am HA-Host **oder** ein ESPHome-Gerät mit
  `bluetooth_proxy` (aktive Verbindungen) in Funkreichweite der Cubes
- Verschlüsselung der Cubes im PRIVATE-Key-Modus (Standard)

## Wie es funktioniert (Kurzfassung)

Die App nutzt kein Bluetooth-SIG-Mesh, sondern **LMP (Linkio Mesh Protocol)**.
Steuer-Frames gehen als GATT-Write auf Characteristic
`00005002-0000-1000-8000-00805f9b34fb` (Service `41c15000-…`). Der 16-Byte-Payload
wird mit `payload XOR AES128-ECB(keyCrypt1, nonce)` verschlüsselt. Details der
Analyse liegen im Entwicklungs-Repo.

## Sicherheit / Datenschutz

Die `.lap`-Datei und die extrahierten Schlüssel sind **Geheimnisse deiner
Installation**. Sie werden nur lokal in der Config-Entry gespeichert und **nie**
in dieses Repository committed (siehe `.gitignore`).

## Lizenz

MIT – siehe [LICENSE](LICENSE).
