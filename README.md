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

---

Die Integration spricht das proprietäre **LMP (Linkio Mesh Protocol)** der App
„Smart & Green – Mesh" direkt über BLE. Die nötigen Mesh-Schlüssel liest sie
**einmalig aus dem verschlüsselten App-Export (`.lap`)** – kein Hex-Gefummel.
Die Steuerung läuft über den Bluetooth-Adapter des HA-Hosts oder einen
**ESPHome-Bluetooth-Proxy** in Funkreichweite der Cubes.

> Reverse-engineered für die private Interoperabilität eigener, gekaufter Hardware.
> Kein offizielles Produkt von Smart & Green / Linkio. Alle Marken gehören ihren Eigentümern.

## Features

- Ein `light`-Entity pro Cube: **An/Aus, Helligkeit, Farbe (HS)**
- Optionales Gruppen-Entity **„Alle"** (Broadcast an alle Cubes)
- Transparente Nutzung vorhandener **ESPHome-Bluetooth-Proxys**
- **Auto-Discovery** der Cubes per BLE-Advertisement (Company-ID `0x04AA`)
- Schlüssel-Import direkt aus dem `.lap`-Export der App

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

Frühe Version (v0.1.0). Protokoll, Verschlüsselung und Frame-Format sind **am
echten Gerät verifiziert**; die HA-Laufzeit (Config-Flow, Write über Proxy,
Gruppen-Broadcast) profitiert noch von Feld-Tests. Der Zustand ist derzeit
*optimistic* (kein Rücklesen). Rückmeldungen/Issues willkommen.

## Sicherheit / Datenschutz

Die `.lap`-Datei und die extrahierten Schlüssel sind **Geheimnisse deiner
Installation**. Sie werden nur lokal in der Config-Entry gespeichert und **nie**
in dieses Repository committed (siehe `.gitignore`).

## Icons

Das Integrations-Icon liegt im Repo unter
`custom_components/smartgreen_cube/brand/` (`icon.png` 256px, `icon@2x.png` 512px,
`logo.png`). Das Motiv ist das Herstellerlogo aus der Original-App.

## Lizenz

MIT – siehe [LICENSE](LICENSE).
