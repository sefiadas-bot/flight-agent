"""
flight_checker.py - Daily flight deal monitor.

Searches Google Flights via Playwright, deduplicates deals against
previous runs, and sends one push notification per day via ntfy.sh
(plain HTTPS — works through corporate firewalls).

Required env vars : NTFY_TOPIC
Optional env vars : NTFY_SERVER               (default: https://ntfy.sh)
                    SEND_NO_DEALS_NOTIFICATION (default: true)
                    MAX_MONTHLY_REQUESTS       (default: 2000)
                    DEDUP_TTL_DAYS             (default: 60)
                    FLIGHT_AGENT_STATE_FILE
                      (default: ~/.flight_agent/state.json)
"""

import hashlib
import json
import logging
import os
import random
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Logging  (no secrets in log lines)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State-file path  (module-level; tests may override fc._STATE_FILE)
# ---------------------------------------------------------------------------
_STATE_FILE = Path(
    os.environ.get(
        "FLIGHT_AGENT_STATE_FILE",
        str(Path.home() / ".flight_agent" / "state.json"),
    )
)

# ---------------------------------------------------------------------------
# "ALL" destination expansion list  (unchanged from original)
# ---------------------------------------------------------------------------
EXPLORE_DESTINATIONS = [
    # Germany
    "MUC", "FRA", "BER", "DUS", "HAM", "STR", "CGN", "NUE", "HAJ", "BRE",
    # Popular Europe
    "AMS", "BCN", "FCO", "VIE", "PRG", "BUD", "ATH",
    "WAW", "MAD", "LIS", "SOF", "LHR", "LGW", "ARN",
    "DUB", "ZRH", "MXP", "NAP", "OPO", "VAR", "SKG",
    "LJU", "ZAG", "KRK", "SZG",
]

# ---------------------------------------------------------------------------
# Environment validation  (call once at startup; does not run on import)
# ---------------------------------------------------------------------------
REQUIRED_ENV = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]


def validate_env():
    """Exit with a clear message if any required env var is missing."""
    missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)


def _cfg():
    """Return runtime configuration from environment variables."""
    raw_no_notif = os.environ.get("SEND_NO_DEALS_NOTIFICATION", "").strip().lower()
    send_no_deals = raw_no_notif not in ("false", "0", "no")  # default: true

    raw_max = os.environ.get("MAX_MONTHLY_REQUESTS", "").strip()
    max_monthly = int(raw_max) if raw_max.isdigit() else 2000

    raw_ttl = os.environ.get("DEDUP_TTL_DAYS", "").strip()
    ttl_days = int(raw_ttl) if raw_ttl.isdigit() else 60

    return {
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHAT_ID": os.environ.get("TELEGRAM_CHAT_ID", ""),
        "SEND_NO_DEALS_NOTIFICATION": send_no_deals,
        "MAX_MONTHLY_REQUESTS": max_monthly,
        "DEDUP_TTL_DAYS": ttl_days,
    }


# ---------------------------------------------------------------------------
# State management  (deduplication + monthly quota tracking)
# ---------------------------------------------------------------------------

def load_state():
    """Load persisted state from disk; return empty dict on missing/corrupt file."""
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("State file unreadable (%s); starting fresh.", exc)
    return {}


def save_state(state):
    """Persist state to disk, creating parent directories as needed."""
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def prune_state(state, ttl_days=None):
    """Remove seen-deal entries older than ttl_days; mutates and returns state."""
    if ttl_days is None:
        ttl_days = _cfg()["DEDUP_TTL_DAYS"]
    cutoff = datetime.now(tz=timezone.utc).timestamp() - ttl_days * 86400
    seen = state.get("seen_deals", {})
    state["seen_deals"] = {k: v for k, v in seen.items() if v.get("ts", 0) > cutoff}
    return state


def deal_id(origin, destination, outbound_date, return_date, airline, price):
    """Stable 12-char hex identifier for a deal (used for cross-run dedup)."""
    raw = "|".join([
        str(origin), str(destination),
        str(outbound_date), str(return_date),
        str(airline).lower().strip(), str(price),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def is_seen(state, did):
    """Return True if this deal ID was already emailed in a prior run."""
    return did in state.get("seen_deals", {})


def mark_seen(state, did, label=""):
    """Record a deal as seen so it won't be emailed again."""
    state.setdefault("seen_deals", {})[did] = {
        "ts": datetime.now(tz=timezone.utc).timestamp(),
        "label": label,
    }


# ---------------------------------------------------------------------------
# Monthly quota guard
# ---------------------------------------------------------------------------

def quota_ok(state, max_requests=None):
    """Return True when this month's request count is below the configured limit."""
    if max_requests is None:
        max_requests = _cfg()["MAX_MONTHLY_REQUESTS"]
    now = datetime.now(tz=timezone.utc)
    ym = f"{now.year}-{now.month:02d}"
    usage = state.setdefault("monthly_usage", {"month": ym, "count": 0})
    if usage.get("month") != ym:
        # New calendar month — reset counter
        state["monthly_usage"] = {"month": ym, "count": 0}
        return True
    return usage["count"] < max_requests


def charge_quota(state, n=1):
    """Increment this month's request counter; returns new total."""
    now = datetime.now(tz=timezone.utc)
    ym = f"{now.year}-{now.month:02d}"
    usage = state.setdefault("monthly_usage", {"month": ym, "count": 0})
    if usage.get("month") != ym:
        state["monthly_usage"] = {"month": ym, "count": n}
        return n
    usage["count"] += n
    return usage["count"]


# ---------------------------------------------------------------------------
# Searches
# ---------------------------------------------------------------------------

def load_searches():
    """Load search configurations from searches.json."""
    with open("searches.json", encoding="utf-8") as f:
        return json.load(f)


# Recognised flexibility tokens and the day-offsets they generate.
_FLEXIBILITY_OFFSETS: dict[str, list[int]] = {
    "+1":   [0, 1],
    "-1":   [-1, 0],
    "+/-1": [-1, 0, 1],
    "+/-2": [-2, -1, 0, 1, 2],
    "+/-3": [-3, -2, -1, 0, 1, 2, 3],
}


def expand_date_pairs(date_pairs: list, flexibility: str | None) -> list:
    """Return an expanded list of [outbound, return] date pairs.

    The flexibility string shifts the *whole trip* (outbound and return move
    together by the same offset) so the trip length stays constant.

    flexibility values:
        None / "none"  →  date_pairs unchanged
        "+1"           →  original  + original+1 day
        "-1"           →  original-1 day + original
        "+/-1"         →  -1 day, exact, +1 day  (3 variants per pair)
        "+/-2"         →  -2 … +2              (5 variants per pair)
        "+/-3"         →  -3 … +3              (7 variants per pair)

    Duplicate pairs (e.g. when two original pairs overlap after shifting) are
    removed while preserving order.
    """
    if not flexibility or flexibility == "none":
        return date_pairs

    offsets = _FLEXIBILITY_OFFSETS.get(flexibility)
    if offsets is None:
        return date_pairs

    expanded: list = []
    seen: set = set()
    for pair in date_pairs:
        outbound = date.fromisoformat(str(pair[0]).strip())
        return_d = date.fromisoformat(str(pair[1]).strip())
        for delta in offsets:
            new_pair = (
                (outbound + timedelta(days=delta)).isoformat(),
                (return_d  + timedelta(days=delta)).isoformat(),
            )
            if new_pair not in seen:
                seen.add(new_pair)
                expanded.append(list(new_pair))
    return expanded


# ---------------------------------------------------------------------------
# Google Flights scraping  (no paid API; browser automation via Playwright)
# ---------------------------------------------------------------------------

def build_google_url(origin, destination, outbound_date, return_date):
    return (
        f"https://www.google.com/travel/flights?hl=en&curr=USD"
        f"#flt={origin}.{destination}.{outbound_date}"
        f"*{destination}.{origin}.{return_date};c:USD;e:1;sd:1;t:f"
    )


def parse_price(text):
    m = re.search(r'\$(\d{1,4}(?:,\d{3})?)', text)
    return int(m.group(1).replace(",", "")) if m else None


def parse_hour(time_str):
    for fmt in ("%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(time_str.strip(), fmt).hour
        except ValueError:
            continue
    return None


def extract_prices(page):
    """Extract all price+flight-detail pairs visible on the current Google Flights page.

    Tries ``li`` elements first (most common), then ``[role='listitem']`` as a
    fallback in case Google's markup changes.
    """
    deals_raw = []

    def _parse_card(text):
        price = parse_price(text)
        if price is None or price < 50 or price > 5000:
            return None
        times = re.findall(r'\d{1,2}:\d{2}\s*(?:AM|PM)', text, re.IGNORECASE)
        stops_match = re.search(r'(\d+)\s+stop', text, re.IGNORECASE)
        stops = (
            0 if re.search(r'nonstop', text, re.IGNORECASE)
            else (int(stops_match.group(1)) if stops_match else "?")
        )
        dur = re.search(r'(\d+)\s*hr?\s*(?:(\d+)\s*min?)?', text, re.IGNORECASE)
        airline = next(
            (ln.strip() for ln in text.splitlines()
             if ln.strip() and not re.search(r'[\$\d:]', ln) and len(ln.strip()) > 2),
            "?",
        )
        return {
            "price":     price,
            "airline":   airline,
            "departure": times[0] if times else "",
            "arrival":   times[1] if len(times) > 1 else "",
            "duration":  f"{dur.group(1)}h {dur.group(2) or '0'}m" if dur else "?",
            "stops":     stops,
        }

    for selector in ("li", "[role='listitem']"):
        try:
            cards = page.locator(selector).all()
            for card in cards:
                try:
                    deal = _parse_card(card.inner_text())
                    if deal is not None:
                        deals_raw.append(deal)
                except Exception:
                    continue
        except Exception:
            continue
        if deals_raw:
            break  # stop at first selector that produces results

    return deals_raw


def find_deals(origin, destination, outbound_date, return_date,
               max_price, max_depart_hour, context):
    """
    Open one Google Flights page and return a list of matching deals.
    Each deal dict includes outbound_date and return_date for correct URL
    generation later (fixes upstream bug where last loop date was reused).

    Navigation strategy:
    1. Load the base Google Flights page so the React SPA initialises and
       any consent dialogs can be dismissed.
    2. Navigate to the search URL via ``window.location.href`` (a true
       client-side hash-router navigation that avoids a full reload) rather
       than a second ``page.goto()``.  Fall back to ``page.goto()`` if the
       JS approach raises.
    3. Wait up to 25 s for prices to render, then extract.
    """
    url = build_google_url(origin, destination, outbound_date, return_date)
    page = context.new_page()
    deals_raw = []
    try:
        page.set_extra_http_headers({
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        })

        # Step 1: load the base page so the SPA initialises.
        try:
            page.goto(
                "https://www.google.com/travel/flights?hl=en&curr=USD",
                wait_until="domcontentloaded",
                timeout=25000,
            )
            page.wait_for_timeout(2000)
        except Exception:
            pass

        # Dismiss consent dialog
        for label in ["Accept all", "Reject all", "I agree"]:
            try:
                page.locator(f"button:has-text('{label}')").first.click(timeout=2000)
                page.wait_for_timeout(800)
                break
            except Exception:
                pass

        # Step 2: navigate to the specific search URL.
        # Using window.location.href triggers the React hash-router natively
        # (fires popstate/hashchange) without a full page reload, which is
        # less detectable than a Playwright goto() on the same origin.
        nav_ok = False
        try:
            page.evaluate("u => { window.location.href = u; }", url)
            nav_ok = True
        except Exception:
            pass

        if not nav_ok:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
            except Exception:
                pass

        # Wait for the SPA's async API call to complete and prices to render.
        # networkidle gives the SPA time to finish XHR/fetch calls.
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass  # networkidle may never fire on pages with polling; that is OK

        prices_visible = False
        try:
            page.wait_for_selector("text=/\\$\\d{2,4}/", timeout=25000)
            prices_visible = True
        except Exception:
            pass

        page.wait_for_timeout(2000)  # extra stabilisation

        # --- diagnostic logging (always) ---
        try:
            cur_url = page.url
            cur_title = page.title()
            log.info(
                "  [nav] prices_visible=%s | url=%.90s | title=%.60s",
                prices_visible, cur_url, cur_title,
            )
        except Exception:
            pass

        deals_raw = extract_prices(page)

        if not deals_raw:
            # Log a snippet of page body text to help distinguish
            # "homepage loaded" from "results loaded but selector wrong"
            try:
                snippet = page.inner_text("body")
                snippet = " ".join(snippet.split())[:400]
                log.info("  [page-snippet] %s", snippet)
            except Exception:
                pass
            shot = f"debug_{origin}_{destination}_{outbound_date}.png"
            try:
                page.screenshot(path=shot, full_page=True, timeout=5000)
                log.info("No results; debug screenshot saved: %s", shot)
            except Exception:
                pass
    finally:
        page.close()

    # Filter, deduplicate within this page, and attach date context to each deal
    deals = []
    seen_keys: set = set()
    for d in sorted(deals_raw, key=lambda x: x["price"]):
        if d["price"] > max_price:
            continue
        if max_depart_hour is not None and d["departure"]:
            h = parse_hour(d["departure"])
            if h is not None and h > max_depart_hour:
                continue
        key = (d["price"], d["departure"])
        if key not in seen_keys:
            seen_keys.add(key)
            deals.append({**d, "outbound_date": outbound_date, "return_date": return_date})
    return deals


def find_deals_with_retry(origin, destination, outbound_date, return_date,
                           max_price, max_depart_hour, context, max_retries=3):
    """
    Wrap find_deals with exponential back-off retry for transient failures.
    Raises the last exception after max_retries unsuccessful attempts.
    Does NOT retry on permanent errors (authentication, etc.) — those will
    still propagate and be caught by the caller.
    """
    delay = 5
    for attempt in range(max_retries):
        try:
            return find_deals(
                origin, destination, outbound_date, return_date,
                max_price, max_depart_hour, context,
            )
        except Exception as exc:
            if attempt < max_retries - 1:
                log.warning(
                    "Attempt %d/%d failed for %s\u2192%s %s: %s. Retrying in %ds \u2026",
                    attempt + 1, max_retries, origin, destination, outbound_date, exc, delay,
                )
                time.sleep(delay)
                delay *= 2
            else:
                raise


# ---------------------------------------------------------------------------
# Notification (ntfy.sh — plain HTTPS, works through corporate firewalls)
# ---------------------------------------------------------------------------

def build_notification_body(today, summary_lines, deal_sections):
    """Assemble a compact plain-text notification body."""
    parts = [f"Flight Check \u2013 {today}", ""]
    parts.extend(summary_lines)
    if deal_sections:
        parts += ["", ""]
        parts.append("\n\n".join(deal_sections))
        parts.append("")
        parts.append("** Book now \u2013 prices change fast!")
    else:
        parts.append("")
        parts.append("// No new deals today.")
    return "\n".join(parts)


def send_telegram(title, body, click_url=None):
    """Send a message via Telegram Bot API over HTTPS (port 443)."""
    cfg = _cfg()
    token = cfg["TELEGRAM_BOT_TOKEN"]
    chat_id = cfg["TELEGRAM_CHAT_ID"]

    # Telegram MarkdownV2 requires escaping special chars; use plain HTML instead
    text = f"<b>{title}</b>\n\n{body}"
    if click_url:
        text += f'\n\n<a href="{click_url}">Book now</a>'

    # Telegram max message length is 4096 chars
    if len(text) > 4000:
        text = text[:3900] + "\n\u2026(truncated)"

    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")

    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Telegram API returned HTTP {resp.status}")


# ---------------------------------------------------------------------------
# Reporting helper (separated so main() can call it on early quota exit too)
# ---------------------------------------------------------------------------

def _send_report(today, summary_lines, deal_sections, any_new_deals):
    """Build and optionally send the daily summary notification via ntfy.sh."""
    cfg = _cfg()
    if not any_new_deals and not cfg["SEND_NO_DEALS_NOTIFICATION"]:
        log.info("No new deals and SEND_NO_DEALS_NOTIFICATION=false; skipping notification.")
        return

    body = build_notification_body(today, summary_lines, deal_sections)

    # Use the booking URL of the first deal as the tap-to-open link
    click_url = None
    for section in deal_sections:
        m = re.search(r'https://\S+', section)
        if m:
            click_url = m.group(0)
            break

    if any_new_deals:
        deal_count = sum(1 for ln in summary_lines if ln.startswith("OK "))
        title = f"\u2708 {deal_count} Deal{'s' if deal_count != 1 else ''} Found \u2013 {today}"
        priority = "high"
    else:
        title = f"\u2708 No New Deals \u2013 {today}"
        priority = "min"

    send_telegram(title, body, click_url=click_url)
    log.info("Notification sent via Telegram")


# ---------------------------------------------------------------------------
# Browser stealth script (module-level so it can be applied to every new
# browser context without duplication)
# ---------------------------------------------------------------------------
_STEALTH_JS = """
// 1. webdriver flag
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// 2. languages
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});

// 3. device memory & hardware concurrency (typical laptop values)
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});

// 4. connection info
try {
    Object.defineProperty(navigator, 'connection', {
        get: () => ({effectiveType: '4g', type: 'wifi', downlink: 10, rtt: 50, saveData: false}),
    });
} catch(e) {}

// 5. realistic plugins (Chrome PDF Plugin / Viewer / Native Client)
(function() {
    function makeMime(type, desc, suffix) {
        return {type, description: desc, suffixes: suffix};
    }
    function makePlugin(name, desc, filename, mimes) {
        const p = Object.create(Plugin.prototype);
        Object.defineProperty(p, 'name',        {get: () => name});
        Object.defineProperty(p, 'description', {get: () => desc});
        Object.defineProperty(p, 'filename',    {get: () => filename});
        Object.defineProperty(p, 'length',      {get: () => mimes.length});
        mimes.forEach((m, i) => { p[i] = m; Object.defineProperty(m, 'enabledPlugin', {get: () => p}); });
        p.item       = i    => p[i] ?? null;
        p.namedItem  = name => mimes.find(m => m.type === name) ?? null;
        return p;
    }
    const pdf  = makeMime('application/x-google-chrome-pdf', 'Portable Document Format', 'pdf');
    const nacl = makeMime('application/x-nacl', 'Native Client Executable', '');
    const pnacl= makeMime('application/x-pnacl', 'Portable Native Client Executable', '');
    const plugins = [
        makePlugin('Chrome PDF Plugin',  'Portable Document Format', 'internal-pdf-viewer', [pdf]),
        makePlugin('Chrome PDF Viewer',  '',                          'mhjfbmdgcfjbbpaeojofohoefgiehjai', [pdf]),
        makePlugin('Native Client',      '',                          'internal-nacl-plugin', [nacl, pnacl]),
    ];
    const arr = Object.create(PluginArray.prototype);
    Object.defineProperty(arr, 'length', {get: () => plugins.length});
    plugins.forEach((p, i) => { arr[i] = p; });
    arr.item       = i    => arr[i] ?? null;
    arr.namedItem  = name => plugins.find(p => p.name === name) ?? null;
    arr.refresh    = () => {};
    Object.defineProperty(navigator, 'plugins', {get: () => arr});
})();

// 6. permissions (Notification.permission)
try {
    const origQuery = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (params) =>
        params.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : origQuery(params);
} catch(e) {}

// 7. chrome object (full runtime stub)
window.chrome = {
    app: {
        isInstalled: false,
        InstallState: {DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed'},
        RunningState: {CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running'},
    },
    runtime: {
        OnInstalledReason: {
            CHROME_UPDATE: 'chrome_update', INSTALL: 'install',
            SHARED_MODULE_UPDATE: 'shared_module_update', UPDATE: 'update',
        },
        PlatformOs: {ANDROID: 'android', CROS: 'cros', LINUX: 'linux', MAC: 'mac', WIN: 'win'},
        PlatformArch: {ARM: 'arm', ARM64: 'arm64', X86_32: 'x86-32', X86_64: 'x86-64'},
        RequestUpdateCheckStatus: {NO_UPDATE: 'no_update', THROTTLED: 'throttled', UPDATE_AVAILABLE: 'update_available'},
        connect: () => {},
        sendMessage: () => {},
    },
};

// 8. screen dimensions (consistent with 1920x1080 viewport)
try {
    Object.defineProperty(screen, 'width',       {get: () => 1920});
    Object.defineProperty(screen, 'height',      {get: () => 1080});
    Object.defineProperty(screen, 'availWidth',  {get: () => 1920});
    Object.defineProperty(screen, 'availHeight', {get: () => 1040});
    Object.defineProperty(screen, 'colorDepth',  {get: () => 24});
    Object.defineProperty(screen, 'pixelDepth',  {get: () => 24});
    Object.defineProperty(window, 'outerWidth',  {get: () => 1920});
    Object.defineProperty(window, 'outerHeight', {get: () => 1080});
} catch(e) {}

// 9. WebGL vendor / renderer (Intel, a common corporate machine)
try {
    const origGetParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris Plus Graphics OpenGL Engine';
        return origGetParam.call(this, p);
    };
} catch(e) {}

// 10. iframe contentWindow.navigator.webdriver
try {
    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
        get: function() {
            const w = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow').get.call(this);
            if (w) try { Object.defineProperty(w.navigator, 'webdriver', {get: () => undefined}); } catch(e) {}
            return w;
        },
    });
} catch(e) {}
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    validate_env()
    cfg = _cfg()

    # Load persisted state and expire old entries
    state = load_state()
    prune_state(state)

    log.info(
        "Monthly requests used so far: %d / %d",
        state.get("monthly_usage", {}).get("count", 0),
        cfg["MAX_MONTHLY_REQUESTS"],
    )

    # Expand searches (resolve "ALL" destinations)
    raw_searches = load_searches()
    searches = []
    for s in raw_searches:
        if s["destination"].upper() == "ALL":
            for dest in EXPLORE_DESTINATIONS:
                searches.append({**s, "destination": dest})
        else:
            searches.append(s)

    log.info(
        "Loaded %d config(s) \u2192 %d search(es).", len(raw_searches), len(searches)
    )

    today = datetime.now(tz=timezone.utc).strftime("%d %b %Y")
    summary_lines: list = []
    deal_sections: list = []
    any_new_deals = False
    any_error = False
    quota_hit = False

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--window-size=1920,1080",
            ],
        )

        try:
            for search in searches:
                if quota_hit:
                    break

                origin = search["origin"]
                destination = search["destination"]
                max_price = search["max_price"]
                max_depart_hour = search.get("max_depart_hour", 23)
                date_pairs = search["date_pairs"]

                flexibility = search.get("date_flexibility", "none")
                date_pairs = expand_date_pairs(date_pairs, flexibility)

                log.info(
                    "%s \u2192 %s | max $%d | %d date pair(s)%s",
                    origin, destination, max_price, len(date_pairs),
                    f" [flex {flexibility}]" if flexibility and flexibility != "none" else "",
                )
                route_deals: list = []
                errors: list = []

                # Fresh browser context per route: each route gets its own
                # cookie jar and session storage so Google cannot correlate
                # back-to-back searches within a single browser session.
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="America/New_York",
                )
                context.add_init_script(_STEALTH_JS)

                try:
                    for outbound_date, return_date in date_pairs:
                        outbound_date = outbound_date.strip()
                        return_date = return_date.strip()

                        # Hard stop before exceeding monthly allowance
                        if not quota_ok(state, cfg["MAX_MONTHLY_REQUESTS"]):
                            log.warning(
                                "Monthly request limit (%d) reached. "
                                "Stopping searches early.",
                                cfg["MAX_MONTHLY_REQUESTS"],
                            )
                            quota_hit = True
                            break

                        log.info("  Checking %s / %s \u2026", outbound_date, return_date)
                        try:
                            found = find_deals_with_retry(
                                origin, destination, outbound_date, return_date,
                                max_price, max_depart_hour, context,
                            )
                            charge_quota(state)
                            route_deals.extend(found)
                            log.info("  \u2192 %d deal(s) found", len(found))
                        except Exception as exc:
                            errors.append(str(exc))
                            any_error = True
                            log.error("  \u2192 Error: %s", exc)
                        # Short random pause between date-pair loads (human-like)
                        time.sleep(random.uniform(3, 7))
                finally:
                    context.close()

                # ---- route-level summary (browser context no longer needed) ----
                route_deals = sorted(route_deals, key=lambda x: x["price"])
                date_strs = ", ".join(f"{o}\u2192{r}" for o, r in date_pairs)
                route_label = (
                    f"{origin} \u2192 {destination} (max ${max_price}) | {date_strs}"
                )

                if route_deals:
                    # Filter out deals already reported in prior runs
                    new_deals = []
                    for d in route_deals:
                        did = deal_id(
                            origin, destination,
                            d["outbound_date"], d["return_date"],
                            d["airline"], d["price"],
                        )
                        if not is_seen(state, did):
                            new_deals.append(d)
                            mark_seen(
                                state, did,
                                f"{origin}\u2192{destination} ${d['price']}",
                            )

                    if new_deals:
                        any_new_deals = True
                        best = new_deals[0]
                        summary_lines.append(
                            f"OK {route_label}\n"
                            f"   Best: ${best['price']} | {best['airline']} | "
                            f"{best['departure']}\u2192{best['arrival']} | "
                            f"{best['stops']} stop(s)"
                        )
                        section = [
                            "=" * 55,
                            f"~  {origin} \u2192 {destination}  |  max ${max_price}",
                            "=" * 55,
                        ]
                        found_at = datetime.now(tz=timezone.utc).strftime("%H:%M UTC")
                        for i, d in enumerate(new_deals, 1):
                            url = build_google_url(
                                origin, destination,
                                d["outbound_date"], d["return_date"],
                            )
                            section.append(
                                f"\n#{i}  ${d['price']} round trip\n"
                                f"    Dates    : {d['outbound_date']} \u2192 {d['return_date']}\n"
                                f"    Airline  : {d['airline']}\n"
                                f"    Depart   : {d['departure']}  \u2192  Arrive: {d['arrival']}\n"
                                f"    Duration : {d['duration']}  |  Stops: {d['stops']}\n"
                                f"    Found at : {found_at}\n"
                                f"    Book     : {url}\n"
                            )
                        deal_sections.append("\n".join(section))
                    else:
                        # Deals exist but all already reported previously
                        summary_lines.append(
                            f"NO {route_label}  (seen in prior run)"
                        )
                elif errors:
                    summary_lines.append(
                        f"!! {route_label}\n   Error: {errors[0]}"
                    )
                else:
                    summary_lines.append(f"NO {route_label}")

                # Human-like inter-route pause (skip after the last route)
                if not quota_hit and search is not searches[-1]:
                    delay = random.uniform(5, 15)
                    log.info("Pausing %.0fs before next route \u2026", delay)
                    time.sleep(delay)
        finally:
            browser.close()

    save_state(state)

    log.info(
        "Monthly requests used this month: %d / %d",
        state.get("monthly_usage", {}).get("count", 0),
        cfg["MAX_MONTHLY_REQUESTS"],
    )

    _send_report(today, summary_lines, deal_sections, any_new_deals)

    if any_error:
        sys.exit(1)


if __name__ == "__main__":
    main()
