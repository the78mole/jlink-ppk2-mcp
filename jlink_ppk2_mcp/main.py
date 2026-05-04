"""
MCP-Server für Nordic PPK2 (Power Profiler Kit II) und SEGGER J-Link.

Bietet folgende MCP-Tools:
  - flash_firmware : Hex-File via J-Link flashen
  - set_power      : PPK2-Spannung steuern (Source Mode)
  - get_power_metrics : Durchschnittlichen Stromverbrauch messen
  - read_rtt       : RTT-Logs vom J-Link lesen
"""

import logging
import os
import threading
import time
from typing import Any

import pylink
from mcp.server.fastmcp import FastMCP
from ppk2_api.ppk2_api import PPK2_API

# Logger für dieses Modul
log = logging.getLogger(__name__)

# Maximale Ausgangsspannung in Millivolt (Sicherheitsgrenze)
MAX_SPANNUNG_MV = 3600


# ---------------------------------------------------------------------------
# Hardware-Manager
# ---------------------------------------------------------------------------

class HardwareManager:
    """Verwaltet die Verbindungen zu PPK2 und J-Link."""

    def __init__(self) -> None:
        # PPK2-Instanz (None = nicht verbunden)
        self.ppk2: PPK2_API | None = None
        # J-Link-Instanz (None = nicht verbunden)
        self.jlink: pylink.JLink | None = None
        # Puffer für gesammelte RTT-Nachrichten
        self.rtt_puffer: list[str] = []
        # Hintergrund-Thread für RTT-Lesen
        self._rtt_thread: threading.Thread | None = None
        # Steuerflag: RTT-Thread aktiv?
        self._rtt_laeuft: bool = False

    # ------------------------------------------------------------------
    # PPK2
    # ------------------------------------------------------------------

    def ppk2_verbinden(self) -> None:
        """Sucht und verbindet das erste verfügbare PPK2-Gerät."""
        ports = PPK2_API.list_devices()
        if not ports:
            raise ConnectionError("Kein PPK2-Gerät gefunden. Bitte USB-Verbindung prüfen.")
        port = ports[0]
        log.info("Verbinde PPK2 auf Port %s …", port)
        self.ppk2 = PPK2_API(port, timeout=1)
        # Kalibrierungskoeffizienten laden
        self.ppk2.get_modifiers()
        # Source-Meter-Modus aktivieren
        self.ppk2.use_source_meter()
        log.info("PPK2 verbunden.")

    def ppk2_sicherstellen(self) -> None:
        """Stellt sicher, dass eine PPK2-Verbindung besteht."""
        if self.ppk2 is None:
            self.ppk2_verbinden()

    # ------------------------------------------------------------------
    # J-Link
    # ------------------------------------------------------------------

    def jlink_verbinden(self, geraet: str = "") -> None:
        """Öffnet die Verbindung zum J-Link-Debugger.

        Args:
            geraet: Optionaler Zielchip-Name (z. B. 'nRF52840_xxAA').
                    Wird benötigt, um RTT und Flash nutzen zu können.
        """
        log.info("Öffne J-Link-Verbindung …")
        self.jlink = pylink.JLink()
        self.jlink.open()
        if geraet:
            self.jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)
            self.jlink.connect(geraet)
            log.info("J-Link verbunden mit Gerät '%s'.", geraet)
        else:
            log.info("J-Link geöffnet (noch kein Zielchip verbunden).")

    def jlink_sicherstellen(self, geraet: str = "") -> None:
        """Stellt sicher, dass eine J-Link-Verbindung besteht."""
        if self.jlink is None:
            self.jlink_verbinden(geraet)

    # ------------------------------------------------------------------
    # RTT-Hintergrund-Thread
    # ------------------------------------------------------------------

    def _rtt_lesen_loop(self) -> None:
        """Interner Loop: Liest kontinuierlich RTT-Daten in den Puffer."""
        try:
            self.jlink.rtt_start()  # type: ignore[union-attr]
            # Kurz warten, bis RTT bereit ist
            time.sleep(0.1)
            while self._rtt_laeuft:
                raw_data: bytes = self.jlink.rtt_read(0, 1024)  # type: ignore[union-attr]
                if raw_data:
                    text = raw_data.decode("utf-8", errors="replace")
                    self.rtt_puffer.append(text)
                time.sleep(0.05)
        except Exception as exc:
            log.error("RTT-Lesefehler: %s", exc)
        finally:
            try:
                self.jlink.rtt_stop()  # type: ignore[union-attr]
            except Exception:
                pass

    def rtt_starten(self) -> None:
        """Startet den RTT-Lese-Thread, falls noch nicht aktiv."""
        if self._rtt_thread and self._rtt_thread.is_alive():
            return  # bereits gestartet
        self._rtt_laeuft = True
        self.rtt_puffer.clear()
        self._rtt_thread = threading.Thread(
            target=self._rtt_lesen_loop,
            name="rtt-leser",
            daemon=True,
        )
        self._rtt_thread.start()
        log.info("RTT-Lese-Thread gestartet.")

    def rtt_stoppen(self) -> None:
        """Stoppt den RTT-Lese-Thread."""
        self._rtt_laeuft = False
        if self._rtt_thread:
            self._rtt_thread.join(timeout=2)
            self._rtt_thread = None
        log.info("RTT-Lese-Thread gestoppt.")

    # ------------------------------------------------------------------
    # Aufräumen
    # ------------------------------------------------------------------

    def trennen(self) -> None:
        """Trennt alle Hardware-Verbindungen ordnungsgemäß."""
        self.rtt_stoppen()
        if self.ppk2 is not None:
            try:
                # DUT-Versorgung sicherheitshalber abschalten
                self.ppk2.toggle_DUT_power(False)
            except Exception as exc:
                log.warning("Fehler beim PPK2-Trennen: %s", exc)
            self.ppk2 = None
        if self.jlink is not None:
            try:
                self.jlink.close()
            except Exception as exc:
                log.warning("Fehler beim J-Link-Trennen: %s", exc)
            self.jlink = None


# ---------------------------------------------------------------------------
# MCP-Server konfigurieren
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "jlink-ppk2-mcp",
    description="MCP-Server zur Steuerung von Nordic PPK2 und SEGGER J-Link",
)

# Globale Hardware-Manager-Instanz
manager = HardwareManager()


# ---------------------------------------------------------------------------
# MCP-Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def flash_firmware(file_path: str) -> str:
    """Flasht ein Hex-File via J-Link auf das Zielgerät.

    Args:
        file_path: Absoluter oder relativer Pfad zur Hex-Datei.

    Returns:
        Statusmeldung über Erfolg oder Fehler.
    """
    # Datei-Existenz prüfen
    if not os.path.isfile(file_path):
        return f"Fehler: Datei nicht gefunden – '{file_path}'"

    # J-Link-Verbindung sicherstellen
    try:
        manager.jlink_sicherstellen()
    except Exception as exc:
        return f"Fehler: J-Link nicht erreichbar – {exc}"

    # Firmware flashen
    try:
        manager.jlink.flash_file(file_path, 0)  # type: ignore[union-attr]
        return f"Firmware erfolgreich geflasht: '{file_path}'"
    except Exception as exc:
        return f"Fehler beim Flashen: {exc}"


@mcp.tool()
def set_power(voltage_mv: int, state: bool) -> str:
    """Steuert die PPK2-Spannungsversorgung im Source-Modus.

    Args:
        voltage_mv: Ausgangsspannung in Millivolt (maximal 3600 mV).
        state: True = Versorgung einschalten, False = ausschalten.

    Returns:
        Statusmeldung über Erfolg oder Fehler.
    """
    # Sicherheits-Check: Spannung darf 3600 mV nicht überschreiten
    if voltage_mv > MAX_SPANNUNG_MV:
        return (
            f"Sicherheitsfehler: {voltage_mv} mV überschreitet das zulässige "
            f"Maximum von {MAX_SPANNUNG_MV} mV."
        )
    if voltage_mv < 0:
        return "Fehler: Spannung darf nicht negativ sein."

    # PPK2-Verbindung sicherstellen
    try:
        manager.ppk2_sicherstellen()
    except Exception as exc:
        return f"Fehler: PPK2 nicht erreichbar – {exc}"

    # Spannung setzen und DUT-Versorgung schalten
    try:
        manager.ppk2.set_source_voltage(voltage_mv)  # type: ignore[union-attr]
        manager.ppk2.toggle_DUT_power(state)          # type: ignore[union-attr]
        status_text = "eingeschaltet" if state else "ausgeschaltet"
        return f"PPK2: Spannung auf {voltage_mv} mV gesetzt, Ausgang {status_text}."
    except Exception as exc:
        return f"Fehler bei der Spannungssteuerung: {exc}"


@mcp.tool()
def get_power_metrics(duration_ms: int) -> dict[str, Any]:
    """Misst den Stromverbrauch des DUT über einen definierten Zeitraum.

    Args:
        duration_ms: Messdauer in Millisekunden.

    Returns:
        Dict mit Messergebnissen (Durchschnitt, Min, Max in µA) oder Fehlermeldung.
    """
    if duration_ms <= 0:
        return {"fehler": "Messdauer muss größer als 0 ms sein."}

    # PPK2-Verbindung sicherstellen
    try:
        manager.ppk2_sicherstellen()
    except Exception as exc:
        return {"fehler": f"PPK2 nicht erreichbar – {exc}"}

    # Messung durchführen
    try:
        manager.ppk2.start_measuring()                    # type: ignore[union-attr]
        time.sleep(duration_ms / 1000.0)
        manager.ppk2.stop_measuring()                     # type: ignore[union-attr]
        samples_raw, _ = manager.ppk2.get_data()          # type: ignore[union-attr]
    except Exception as exc:
        return {"fehler": f"Messfehler: {exc}"}

    # Rohdaten auswerten (jede Probe = 3 Bytes)
    BYTES_PER_SAMPLE = 3
    current_ua: list[float] = []
    for i in range(0, len(samples_raw), BYTES_PER_SAMPLE):
        sample = samples_raw[i : i + BYTES_PER_SAMPLE]
        if len(sample) == BYTES_PER_SAMPLE:
            wert = manager.ppk2.get_sample_value(sample)  # type: ignore[union-attr]
            current_ua.append(wert)

    if not current_ua:
        return {"fehler": "Keine auswertbaren Messdaten erhalten."}

    return {
        "durchschnitt_ua": round(sum(current_ua) / len(current_ua), 2),
        "min_ua":          round(min(current_ua), 2),
        "max_ua":          round(max(current_ua), 2),
        "anzahl_messwerte": len(current_ua),
        "dauer_ms":        duration_ms,
    }


@mcp.tool()
def read_rtt() -> dict[str, Any]:
    """Startet den RTT-Lese-Thread und gibt den aktuellen Puffer-Inhalt zurück.

    Der Thread läuft im Hintergrund weiter; jeder Aufruf leert den Puffer
    und gibt die seit dem letzten Aufruf gesammelten Daten zurück.

    Returns:
        Dict mit RTT-Daten und Status-Informationen.
    """
    # J-Link-Verbindung sicherstellen
    try:
        manager.jlink_sicherstellen()
    except Exception as exc:
        return {"fehler": f"J-Link nicht erreichbar – {exc}", "daten": ""}

    # RTT-Thread starten (falls noch nicht aktiv)
    try:
        manager.rtt_starten()
    except Exception as exc:
        return {"fehler": f"RTT-Start fehlgeschlagen – {exc}", "daten": ""}

    # Puffer auslesen und leeren
    gesammelte_daten = "".join(manager.rtt_puffer)
    manager.rtt_puffer.clear()

    return {
        "daten":          gesammelte_daten,
        "zeichen_anzahl": len(gesammelte_daten),
        "thread_aktiv":   manager._rtt_thread is not None and manager._rtt_thread.is_alive(),
    }


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

def main() -> None:
    """Startet den MCP-Server (Einstiegspunkt für `uv tool install`)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        mcp.run()
    finally:
        # Hardware beim Beenden sauber trennen
        manager.trennen()


if __name__ == "__main__":
    main()
