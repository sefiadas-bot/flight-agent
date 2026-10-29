#!/usr/bin/env python3
"""
dashboard.py - Flight Agent monitoring dashboard.

Run:    python dashboard.py          (opens http://localhost:8080)
Run:    python dashboard.py 9000     (custom port)

Reads:  searches.json               (repo root - current directory)
        ~/.flight_agent/state.json  (or FLIGHT_AGENT_STATE_FILE)
Writes: searches.json               (when you save changes in the UI)

No extra dependencies - uses only the Python standard library.
The next GitHub Actions run picks up any changes to searches.json automatically.
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
SEARCHES_FILE = Path("searches.json")
STATE_FILE = Path(
    os.environ.get(
        "FLIGHT_AGENT_STATE_FILE",
        str(Path.home() / ".flight_agent" / "state.json"),
    )
)

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def load_searches() -> list:
    if SEARCHES_FILE.exists():
        try:
            return json.loads(SEARCHES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_searches(searches: list) -> None:
    SEARCHES_FILE.write_text(
        json.dumps(searches, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def get_api_data() -> dict:
    state = load_state()
    searches = load_searches()

    usage = state.get("monthly_usage", {})

    seen_raw = state.get("seen_deals", {})
    deals = []
    for did, info in sorted(seen_raw.items(), key=lambda x: x[1].get("ts", 0), reverse=True):
        ts = info.get("ts", 0)
        seen_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        expires_dt = seen_dt + timedelta(days=60)
        deals.append({
            "id": did,
            "label": info.get("label", did),
            "seen_at": seen_dt.strftime("%Y-%m-%d %H:%M UTC"),
            "expires_at": expires_dt.strftime("%Y-%m-%d"),
        })

    return {
        "searches": searches,
        "quota": {
            "count": usage.get("count", 0),
            "month": usage.get("month", ""),
            "max": 2000,
        },
        "seen_deals": deals,
        "searches_file": str(SEARCHES_FILE.resolve()),
        "state_file": str(STATE_FILE),
        "generated_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            body = _HTML.encode("utf-8")
            self._respond(200, "text/html; charset=utf-8", body)
        elif self.path == "/api/data":
            body = json.dumps(get_api_data(), ensure_ascii=False).encode("utf-8")
            self._respond(200, "application/json; charset=utf-8", body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        if self.path == "/api/searches":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                searches = json.loads(raw)
                save_searches(searches)
                self._respond(200, "application/json", b'{"ok":true}')
            except Exception as exc:
                body = json.dumps({"error": str(exc)}).encode()
                self._respond(400, "application/json", body)
        else:
            self.send_response(404)
            self.end_headers()

    def _respond(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass  # suppress per-request noise


# ---------------------------------------------------------------------------
# Embedded HTML / CSS / JS (no separate files needed)
# ---------------------------------------------------------------------------

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Flight Agent Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body { font-family: system-ui, sans-serif; }
    .mono { font-family: ui-monospace, monospace; }
    input:focus { outline: none; box-shadow: 0 0 0 2px #3b82f6; }
  </style>
</head>
<body class="bg-slate-100 min-h-screen">

<!-- Header -->
<header class="bg-blue-700 text-white px-6 py-4 flex items-center justify-between shadow-md">
  <div class="flex items-center gap-3">
    <span class="text-2xl">&#9992;</span>
    <span class="text-xl font-bold tracking-wide">Flight Agent Dashboard</span>
  </div>
  <div class="flex items-center gap-4 text-sm">
    <span id="last-updated" class="opacity-70">Loading&hellip;</span>
    <button onclick="loadData()" class="bg-white/20 hover:bg-white/30 rounded px-3 py-1 text-xs">
      Refresh
    </button>
  </div>
</header>

<main class="max-w-6xl mx-auto px-4 py-6 space-y-6">

  <!-- Quota -->
  <div class="bg-white rounded-xl shadow p-5">
    <div class="flex items-center justify-between mb-2">
      <h2 class="font-semibold text-slate-700">Monthly Quota</h2>
      <span id="quota-text" class="text-sm text-slate-500 mono">— / —</span>
    </div>
    <div class="bg-slate-200 rounded-full h-3">
      <div id="quota-bar" class="bg-blue-500 h-3 rounded-full transition-all duration-500" style="width:0%"></div>
    </div>
    <p id="quota-month" class="text-xs text-slate-400 mt-1"></p>
  </div>

  <!-- Searches -->
  <div class="bg-white rounded-xl shadow p-5">
    <div class="flex items-center justify-between mb-4">
      <h2 class="font-semibold text-slate-700">Searches
        <span id="searches-count" class="ml-2 text-xs bg-blue-100 text-blue-700 rounded-full px-2 py-0.5"></span>
      </h2>
      <button onclick="openModal()" class="bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors">
        + Add search
      </button>
    </div>
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr class="text-left text-slate-500 border-b text-xs uppercase tracking-wide">
            <th class="pb-2 pr-4 font-medium">Origin</th>
            <th class="pb-2 pr-4 font-medium">Destination</th>
            <th class="pb-2 pr-4 font-medium">Max price</th>
            <th class="pb-2 pr-4 font-medium">Depart by</th>
            <th class="pb-2 pr-4 font-medium">Date pairs</th>
            <th class="pb-2 font-medium"></th>
          </tr>
        </thead>
        <tbody id="searches-body">
          <tr><td colspan="6" class="py-8 text-center text-slate-400">Loading&hellip;</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Seen deals -->
  <div class="bg-white rounded-xl shadow p-5">
    <div class="flex items-center justify-between mb-4">
      <h2 class="font-semibold text-slate-700">Seen Deals
        <span id="deals-count" class="ml-2 text-xs bg-slate-100 text-slate-600 rounded-full px-2 py-0.5"></span>
      </h2>
      <span class="text-xs text-slate-400">Deals already notified &mdash; won't be sent again until expired</span>
    </div>
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr class="text-left text-slate-500 border-b text-xs uppercase tracking-wide">
            <th class="pb-2 pr-6 font-medium">Deal</th>
            <th class="pb-2 pr-6 font-medium">First seen</th>
            <th class="pb-2 font-medium">Expires</th>
          </tr>
        </thead>
        <tbody id="deals-body">
          <tr><td colspan="3" class="py-8 text-center text-slate-400">Loading&hellip;</td></tr>
        </tbody>
      </table>
    </div>
  </div>

</main>

<!-- Add / Edit modal -->
<div id="modal" class="hidden fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
  <div class="bg-white rounded-2xl shadow-2xl w-full max-w-lg max-h-[90vh] overflow-y-auto">
    <div class="p-6">
      <div class="flex items-center justify-between mb-5">
        <h3 id="modal-title" class="text-lg font-semibold text-slate-800">Add search</h3>
        <button onclick="closeModal()" class="text-slate-400 hover:text-slate-600 text-xl leading-none">&times;</button>
      </div>
      <form id="search-form" onsubmit="saveSearch(event)" class="space-y-4">
        <input type="hidden" id="edit-index" value="-1"/>
        <div class="grid grid-cols-2 gap-4">
          <div>
            <label class="block text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">Origin (IATA)</label>
            <input id="f-origin" type="text" maxlength="3" required placeholder="TLV"
              class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm mono uppercase"/>
          </div>
          <div>
            <label class="block text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">Destination</label>
            <input id="f-dest" type="text" maxlength="3" required placeholder="AMS or ALL"
              class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm mono uppercase"/>
            <p class="text-xs text-slate-400 mt-1">Use ALL for all European cities</p>
          </div>
          <div>
            <label class="block text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">Max price (USD)</label>
            <input id="f-price" type="number" min="50" max="9999" required placeholder="650"
              class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"/>
          </div>
          <div>
            <label class="block text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">Depart by (hour)</label>
            <input id="f-hour" type="number" min="0" max="23" required placeholder="14"
              class="w-full border border-slate-300 rounded-lg px-3 py-2 text-sm"/>
            <p class="text-xs text-slate-400 mt-1">24-hour clock (14 = 2 PM)</p>
          </div>
        </div>

        <div>
          <div class="flex items-center justify-between mb-2">
            <label class="text-xs font-semibold text-slate-500 uppercase tracking-wide">Date pairs</label>
            <button type="button" onclick="addDatePair()"
              class="text-blue-600 hover:text-blue-800 text-xs font-medium">+ Add pair</button>
          </div>
          <div id="date-pairs" class="space-y-2"></div>
          <p class="text-xs text-slate-400 mt-1">Outbound date &rarr; Return date</p>
        </div>

        <div id="modal-error" class="hidden text-sm text-red-600 bg-red-50 rounded-lg px-3 py-2"></div>

        <div class="flex gap-3 pt-1">
          <button type="submit"
            class="flex-1 bg-blue-600 hover:bg-blue-700 text-white font-semibold py-2.5 rounded-lg text-sm transition-colors">
            Save
          </button>
          <button type="button" onclick="closeModal()"
            class="flex-1 bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold py-2.5 rounded-lg text-sm transition-colors">
            Cancel
          </button>
        </div>
      </form>
    </div>
  </div>
</div>

<script>
'use strict';

let searches = [];
let refreshHandle = null;
const REFRESH_MS = 30000;

// ---- Data ----
async function loadData() {
  try {
    const r = await fetch('/api/data');
    const d = await r.json();
    searches = d.searches || [];
    renderSearches();
    renderDeals(d.seen_deals || []);
    renderQuota(d.quota || {});
    document.getElementById('last-updated').textContent =
      'Updated ' + (d.generated_at || '');
  } catch (e) {
    console.error('loadData failed', e);
  }
}

function scheduleRefresh() {
  clearTimeout(refreshHandle);
  refreshHandle = setTimeout(() => loadData().then(scheduleRefresh), REFRESH_MS);
}

// ---- Render ----
function renderQuota(q) {
  const pct = q.max > 0 ? Math.min(100, Math.round(q.count / q.max * 100)) : 0;
  const bar = document.getElementById('quota-bar');
  bar.style.width = pct + '%';
  bar.className = 'h-3 rounded-full transition-all duration-500 ' +
    (pct > 80 ? 'bg-red-500' : pct > 50 ? 'bg-yellow-500' : 'bg-blue-500');
  document.getElementById('quota-text').textContent =
    q.count.toLocaleString() + ' / ' + q.max.toLocaleString() + '  (' + pct + '%)';
  document.getElementById('quota-month').textContent =
    q.month ? 'Current month: ' + q.month : '';
}

function renderSearches() {
  document.getElementById('searches-count').textContent = searches.length;
  const tb = document.getElementById('searches-body');
  if (!searches.length) {
    tb.innerHTML = '<tr><td colspan="6" class="py-8 text-center text-slate-400">No searches configured. Click &ldquo;Add search&rdquo; to get started.</td></tr>';
    return;
  }
  tb.innerHTML = searches.map((s, i) => {
    const pairs = (s.date_pairs || [])
      .map(p => '<span class="mono text-xs">' + esc(p[0]) + ' &rarr; ' + esc(p[1]) + '</span>')
      .join('<br/>');
    const destBadge = s.destination === 'ALL'
      ? '<span class="bg-purple-100 text-purple-700 text-xs rounded px-1.5 py-0.5 mono font-bold">ALL</span>'
      : '<span class="mono font-bold text-slate-800">' + esc(s.destination) + '</span>';
    return `<tr class="border-b last:border-0 hover:bg-slate-50 group">
      <td class="py-3 pr-4"><span class="mono font-bold text-blue-700">${esc(s.origin)}</span></td>
      <td class="py-3 pr-4">${destBadge}</td>
      <td class="py-3 pr-4 text-green-700 font-medium mono">$${s.max_price}</td>
      <td class="py-3 pr-4 text-slate-600 mono">${s.max_depart_hour}:00</td>
      <td class="py-3 pr-4 leading-relaxed">${pairs || '<span class="text-slate-400 text-xs">none</span>'}</td>
      <td class="py-3 text-right whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity">
        <button onclick="editSearch(${i})"
          class="text-blue-600 hover:text-blue-800 text-xs font-medium mr-3">Edit</button>
        <button onclick="deleteSearch(${i})"
          class="text-red-500 hover:text-red-700 text-xs font-medium">Delete</button>
      </td>
    </tr>`;
  }).join('');
}

function renderDeals(deals) {
  document.getElementById('deals-count').textContent = deals.length;
  const tb = document.getElementById('deals-body');
  if (!deals.length) {
    tb.innerHTML = '<tr><td colspan="3" class="py-8 text-center text-slate-400">No deals recorded yet &mdash; will populate after the first run.</td></tr>';
    return;
  }
  tb.innerHTML = deals.map(d => {
    const expiry = new Date(d.expires_at);
    const soon = expiry - Date.now() < 7 * 86400e3;
    const expiryClass = soon ? 'text-orange-500 font-medium' : 'text-slate-500';
    return `<tr class="border-b last:border-0 hover:bg-slate-50">
      <td class="py-2.5 pr-6 font-medium">${esc(d.label)}</td>
      <td class="py-2.5 pr-6 text-slate-500 mono text-xs">${esc(d.seen_at)}</td>
      <td class="py-2.5 mono text-xs ${expiryClass}">${esc(d.expires_at)}${soon ? ' (soon)' : ''}</td>
    </tr>`;
  }).join('');
}

// ---- Modal ----
function openModal(idx = -1) {
  const s = idx >= 0 ? searches[idx] : {};
  document.getElementById('edit-index').value = idx;
  document.getElementById('modal-title').textContent = idx < 0 ? 'Add search' : 'Edit search';
  document.getElementById('f-origin').value = s.origin || '';
  document.getElementById('f-dest').value = s.destination || '';
  document.getElementById('f-price').value = s.max_price ?? 650;
  document.getElementById('f-hour').value = s.max_depart_hour ?? 14;
  document.getElementById('date-pairs').innerHTML = '';
  document.getElementById('modal-error').classList.add('hidden');
  (s.date_pairs && s.date_pairs.length ? s.date_pairs : [['', '']]).forEach(p => addDatePair(p[0], p[1]));
  document.getElementById('modal').classList.remove('hidden');
  document.getElementById('f-origin').focus();
}

function closeModal() {
  document.getElementById('modal').classList.add('hidden');
}

function addDatePair(out = '', ret = '') {
  const row = document.createElement('div');
  row.className = 'flex items-center gap-2';
  row.innerHTML =
    '<input type="date" value="' + esc(out) + '" required' +
    '  class="flex-1 border border-slate-300 rounded-lg px-2.5 py-1.5 text-sm mono"/>' +
    '<span class="text-slate-400 text-sm">&rarr;</span>' +
    '<input type="date" value="' + esc(ret) + '" required' +
    '  class="flex-1 border border-slate-300 rounded-lg px-2.5 py-1.5 text-sm mono"/>' +
    '<button type="button" onclick="this.closest(\'div\').remove()"' +
    '  class="text-red-400 hover:text-red-600 text-xl leading-none w-6 flex-shrink-0">&times;</button>';
  document.getElementById('date-pairs').appendChild(row);
}

async function saveSearch(e) {
  e.preventDefault();
  const idx = parseInt(document.getElementById('edit-index').value, 10);
  const pairDivs = document.getElementById('date-pairs').querySelectorAll('div');
  const date_pairs = Array.from(pairDivs).map(row => {
    const ins = row.querySelectorAll('input[type=date]');
    return [ins[0].value, ins[1].value];
  }).filter(p => p[0] && p[1]);

  if (!date_pairs.length) {
    showModalError('Add at least one date pair.');
    return;
  }

  const entry = {
    origin:           document.getElementById('f-origin').value.toUpperCase().trim(),
    destination:      document.getElementById('f-dest').value.toUpperCase().trim(),
    max_price:        parseInt(document.getElementById('f-price').value, 10),
    max_depart_hour:  parseInt(document.getElementById('f-hour').value, 10),
    date_pairs,
  };

  if (idx >= 0) searches[idx] = entry; else searches.push(entry);

  const ok = await persistSearches();
  if (ok) {
    closeModal();
    renderSearches();
  }
}

function editSearch(idx) { openModal(idx); }

function deleteSearch(idx) {
  const s = searches[idx];
  if (!confirm('Delete search: ' + s.origin + ' \u2192 ' + s.destination + '?')) return;
  searches.splice(idx, 1);
  persistSearches().then(() => renderSearches());
}

async function persistSearches() {
  try {
    const r = await fetch('/api/searches', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(searches),
    });
    if (!r.ok) {
      const err = await r.json();
      showModalError('Save failed: ' + (err.error || 'unknown'));
      return false;
    }
    return true;
  } catch (e) {
    showModalError('Save failed: ' + e.message);
    return false;
  }
}

function showModalError(msg) {
  const el = document.getElementById('modal-error');
  el.textContent = msg;
  el.classList.remove('hidden');
}

// ---- Util ----
function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ---- Init ----
document.getElementById('modal').addEventListener('click', e => {
  if (e.target === document.getElementById('modal')) closeModal();
});

loadData().then(scheduleRefresh);
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not SEARCHES_FILE.exists():
        print(f"Warning: {SEARCHES_FILE} not found — run from the flight-agent repo root.")

    url = f"http://localhost:{PORT}"
    print(f"  Dashboard  {url}")
    print(f"  Searches   {SEARCHES_FILE.resolve()}")
    print(f"  State      {STATE_FILE}")
    print("  Press Ctrl+C to stop.\n")

    def _open_browser() -> None:
        import time as _t
        _t.sleep(0.6)
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()

    with http.server.ThreadingHTTPServer(("", PORT), _Handler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
