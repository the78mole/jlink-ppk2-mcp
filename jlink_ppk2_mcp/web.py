"""
FastAPI-Web-UI für die PPK2-Steuerung und Stromanzeige.

Endpunkte:
  GET  /                    → HTML-Dashboard
  GET  /api/status          → JSON-Statusübersicht (Verbindung, Spannung, Versorgung)
  POST /api/power           → Spannung und Versorgungsstatus setzen
  GET  /api/current         → Einmalige Strommessung (JSON)
  GET  /api/current/stream  → Server-Sent Events: Live-Strommessung
"""

import asyncio
import json
import logging
import time
from typing import AsyncGenerator

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from jlink_ppk2_mcp.main import MAX_SPANNUNG_MV, manager

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastAPI-App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="PPK2 Web-UI",
    description="Web-Oberfläche zur Steuerung des Nordic PPK2",
    version="0.1.0",
)


# ---------------------------------------------------------------------------
# Datenschemas
# ---------------------------------------------------------------------------

class PowerRequest(BaseModel):
    """Anfrage zum Setzen von Spannung und Versorgungsstatus."""

    voltage_mv: int = Field(..., ge=0, le=MAX_SPANNUNG_MV, description="Spannung in mV (0–3600)")
    state: bool | None = Field(None, description="True = ein, False = aus, None = unverändert")


# ---------------------------------------------------------------------------
# HTML-Dashboard (inline, kein Template-Engine erforderlich)
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>PPK2 Steuerung</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: "Segoe UI", system-ui, sans-serif;
      background: #0f172a;
      color: #e2e8f0;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 2rem 1rem;
      gap: 1.5rem;
    }
    h1 { font-size: 1.6rem; font-weight: 700; color: #38bdf8; letter-spacing: .04em; }
    .card {
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 1rem;
      padding: 1.5rem 2rem;
      width: 100%;
      max-width: 500px;
    }
    .card h2 { font-size: 1rem; font-weight: 600; color: #94a3b8; margin-bottom: 1rem; text-transform: uppercase; letter-spacing: .06em; }

    /* Statusanzeige */
    .status-grid { display: grid; grid-template-columns: 1fr 1fr; gap: .75rem; }
    .stat { display: flex; flex-direction: column; gap: .25rem; }
    .stat label { font-size: .75rem; color: #64748b; text-transform: uppercase; letter-spacing: .05em; }
    .stat .value { font-size: 1.4rem; font-weight: 700; }
    .connected    { color: #4ade80; }
    .disconnected { color: #f87171; }
    .on  { color: #4ade80; }
    .off { color: #f87171; }

    /* Stromanzeige */
    #current-display {
      font-size: 3rem;
      font-weight: 800;
      text-align: center;
      color: #38bdf8;
      letter-spacing: -.02em;
      padding: .5rem 0;
      min-height: 4.5rem;
    }
    .unit { font-size: 1.2rem; font-weight: 400; color: #94a3b8; margin-left: .25rem; }

    /* Steuerung */
    .control-row { display: flex; flex-direction: column; gap: .5rem; margin-bottom: 1rem; }
    .control-row label { font-size: .85rem; color: #94a3b8; display: flex; justify-content: space-between; }
    input[type=range] {
      width: 100%;
      accent-color: #38bdf8;
      height: 6px;
    }
    .btn-row { display: flex; gap: .75rem; margin-top: .25rem; }
    button {
      flex: 1;
      padding: .65rem 1rem;
      border: none;
      border-radius: .5rem;
      font-size: .9rem;
      font-weight: 600;
      cursor: pointer;
      transition: opacity .15s;
    }
    button:hover { opacity: .85; }
    #btn-on  { background: #16a34a; color: #fff; }
    #btn-off { background: #dc2626; color: #fff; }
    #btn-apply { background: #0284c7; color: #fff; flex: 2; }

    .msg { font-size: .8rem; color: #94a3b8; margin-top: .75rem; min-height: 1.2em; text-align: center; }
    .msg.ok  { color: #4ade80; }
    .msg.err { color: #f87171; }
  </style>
</head>
<body>
  <h1>⚡ PPK2 Steuerung</h1>

  <!-- Statusübersicht -->
  <div class="card">
    <h2>Status</h2>
    <div class="status-grid">
      <div class="stat">
        <label>PPK2</label>
        <span class="value" id="stat-connected">–</span>
      </div>
      <div class="stat">
        <label>Versorgung</label>
        <span class="value" id="stat-power">–</span>
      </div>
      <div class="stat">
        <label>Spannung</label>
        <span class="value" id="stat-voltage">–</span>
      </div>
    </div>
  </div>

  <!-- Live-Stromanzeige -->
  <div class="card">
    <h2>Aktueller Strom</h2>
    <div id="current-display">–<span class="unit">µA</span></div>
    <div style="display:flex; gap:1rem; justify-content:center; margin-top:.25rem;">
      <span class="stat" style="text-align:center">
        <label style="font-size:.7rem;color:#64748b">MIN</label>
        <span id="stat-min" style="font-size:1rem;color:#94a3b8">–</span>
      </span>
      <span class="stat" style="text-align:center">
        <label style="font-size:.7rem;color:#64748b">MAX</label>
        <span id="stat-max" style="font-size:1rem;color:#94a3b8">–</span>
      </span>
    </div>
  </div>

  <!-- Steuerung -->
  <div class="card">
    <h2>Steuerung</h2>
    <div class="control-row">
      <label>
        Spannung
        <strong id="voltage-label">3300 mV</strong>
      </label>
      <input type="range" id="voltage-slider" min="0" max="3600" step="100" value="3300">
    </div>
    <div class="btn-row">
      <button id="btn-on">EIN</button>
      <button id="btn-apply">Spannung setzen</button>
      <button id="btn-off">AUS</button>
    </div>
    <div class="msg" id="ctrl-msg"></div>
  </div>

  <script>
    // -----------------------------------------------------------------------
    // Hilfsfunktionen
    // -----------------------------------------------------------------------
    function fmt(val) {
      if (val === null || val === undefined) return '–';
      if (Math.abs(val) >= 1000) return (val / 1000).toFixed(2) + ' mA';
      return val.toFixed(1) + ' µA';
    }

    function setMsg(text, ok) {
      const el = document.getElementById('ctrl-msg');
      el.textContent = text;
      el.className = 'msg ' + (ok ? 'ok' : 'err');
    }

    // -----------------------------------------------------------------------
    // Statusabfrage (alle 3 s)
    // -----------------------------------------------------------------------
    async function fetchStatus() {
      try {
        const r = await fetch('/api/status');
        const d = await r.json();
        const conn = document.getElementById('stat-connected');
        conn.textContent   = d.ppk2_verbunden ? 'Verbunden' : 'Getrennt';
        conn.className     = 'value ' + (d.ppk2_verbunden ? 'connected' : 'disconnected');
        const pw = document.getElementById('stat-power');
        pw.textContent = d.power_on ? 'EIN' : 'AUS';
        pw.className   = 'value ' + (d.power_on ? 'on' : 'off');
        document.getElementById('stat-voltage').textContent = d.voltage_mv + ' mV';
        // Schieberegler synchronisieren
        document.getElementById('voltage-slider').value = d.voltage_mv;
        document.getElementById('voltage-label').textContent = d.voltage_mv + ' mV';
      } catch (_) {}
    }
    fetchStatus();
    setInterval(fetchStatus, 3000);

    // -----------------------------------------------------------------------
    // Live-Strom via SSE
    // -----------------------------------------------------------------------
    const evtSource = new EventSource('/api/current/stream');
    evtSource.addEventListener('current', (e) => {
      const d = JSON.parse(e.data);
      if (d.fehler) {
        document.getElementById('current-display').innerHTML =
          '<span style="font-size:1rem;color:#f87171">' + d.fehler + '</span>';
        return;
      }
      const avg = d.durchschnitt_ua;
      let display;
      if (Math.abs(avg) >= 1000) {
        display = (avg / 1000).toFixed(3) + '<span class="unit">mA</span>';
      } else {
        display = avg.toFixed(1) + '<span class="unit">µA</span>';
      }
      document.getElementById('current-display').innerHTML = display;
      document.getElementById('stat-min').textContent = fmt(d.min_ua);
      document.getElementById('stat-max').textContent = fmt(d.max_ua);
    });
    evtSource.onerror = () => {
      document.getElementById('current-display').innerHTML =
        '<span style="font-size:1rem;color:#64748b">Warte auf PPK2…</span>';
    };

    // -----------------------------------------------------------------------
    // Steuerung
    // -----------------------------------------------------------------------
    const slider = document.getElementById('voltage-slider');
    slider.addEventListener('input', () => {
      document.getElementById('voltage-label').textContent = slider.value + ' mV';
    });

    async function sendPower(state) {
      const voltage_mv = parseInt(slider.value, 10);
      try {
        const r = await fetch('/api/power', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ voltage_mv, state }),
        });
        const d = await r.json();
        setMsg(d.meldung || JSON.stringify(d), r.ok);
        fetchStatus();
      } catch (err) {
        setMsg('Netzwerkfehler: ' + err.message, false);
      }
    }

    document.getElementById('btn-on').addEventListener('click',    () => sendPower(true));
    document.getElementById('btn-off').addEventListener('click',   () => sendPower(false));
    document.getElementById('btn-apply').addEventListener('click', () => {
      // Spannung setzen ohne den aktuellen Versorgungsstatus zu ändern
      sendPower(null);
    });
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# API-Endpunkte
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> str:
    """Liefert das HTML-Dashboard."""
    return _DASHBOARD_HTML


@app.get("/api/status")
async def get_status() -> dict:
    """Gibt den aktuellen PPK2-Status zurück."""
    return {
        "ppk2_verbunden": manager.ppk2 is not None,
        "voltage_mv":     manager.voltage_mv,
        "power_on":       manager.power_on,
    }


@app.post("/api/power")
async def post_power(req: PowerRequest) -> dict:
    """Setzt Spannung und Versorgungsstatus des DUT.

    Akzeptiert ``state=null`` (JSON ``null``) um nur die Spannung zu ändern,
    ohne den aktuellen Ein/Aus-Status zu modifizieren.
    """
    # Verbindung sicherstellen
    try:
        manager.ppk2_sicherstellen()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"PPK2 nicht erreichbar: {exc}") from exc

    # Spannung setzen
    try:
        manager.ppk2.set_source_voltage(req.voltage_mv)  # type: ignore[union-attr]
        manager.voltage_mv = req.voltage_mv
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Spannungsfehler: {exc}") from exc

    # Versorgung schalten (state=None → aktuellen Zustand behalten)
    ziel_state = req.state if req.state is not None else manager.power_on
    try:
        manager.ppk2.toggle_DUT_power(ziel_state)  # type: ignore[union-attr]
        manager.power_on = ziel_state
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Schaltefehler: {exc}") from exc

    status_text = "eingeschaltet" if ziel_state else "ausgeschaltet"
    return {
        "meldung":    f"Spannung: {req.voltage_mv} mV, Ausgang: {status_text}",
        "voltage_mv": req.voltage_mv,
        "power_on":   ziel_state,
    }


@app.get("/api/current")
async def get_current() -> dict:
    """Führt eine einmalige Strommessung durch und gibt das Ergebnis zurück."""
    try:
        manager.ppk2_sicherstellen()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"PPK2 nicht erreichbar: {exc}") from exc

    try:
        return manager.ppk2_strom_lesen(dauer_ms=200)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Messfehler: {exc}") from exc


@app.get("/api/current/stream")
async def stream_current() -> StreamingResponse:
    """Server-Sent Events: sendet alle 500 ms eine Strommessung."""

    async def generator() -> AsyncGenerator[str, None]:
        while True:
            try:
                manager.ppk2_sicherstellen()
                data = manager.ppk2_strom_lesen(dauer_ms=400)
            except Exception as exc:
                # Vollständige Fehlermeldung nur serverseitig protokollieren
                log.warning("Strommessung fehlgeschlagen: %s", exc)
                data = {"fehler": "PPK2 nicht verfügbar"}

            # SSE-Format: "event: current\ndata: {...}\n\n"
            yield f"event: current\ndata: {json.dumps(data)}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":   "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

def main() -> None:
    """Startet den FastAPI-Web-Server (Einstiegspunkt für `uv tool install`)."""
    import logging as _logging

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    uvicorn.run(
        "jlink_ppk2_mcp.web:app",
        host="127.0.0.1",
        port=8080,
        reload=False,
    )


if __name__ == "__main__":
    main()
