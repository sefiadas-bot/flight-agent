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
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
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
REQUIRED_ENV = ["NTFY_TOPIC"]


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
        "NTFY_TOPIC": os.environ.get("NTFY_TOPIC", ""),
        "NTFY_SERVER": os.environ.get("NTFY_SERVER", "https://ntfy.sh"),
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
    """Extract all price+flight-detail pairs visible on the current Google Flights page."""
    deals_raw = []
    try:
        cards = page.locator("li").all()
        for card in cards:
            try:
                text = card.inner_text()
                price = parse_price(text)
                if price is None or price < 50 or price > 5000:
                    continue
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
                deals_raw.append({
                    "price":     price,
                    "airline":   airline,
                    "departure": times[0] if times else "",
                    "arrival":   times[1] if len(times) > 1 else "",
                    "duration":  f"{dur.group(1)}h {dur.group(2) or '0'}m" if dur else "?",
                    "stops":     stops,
                })
            except Exception:
                continue
    except Exception:
        pass
    return deals_raw


def find_deals(origin, destination, outbound_date, return_date,
               max_price, max_depart_hour, context):
    """
    Open one Google Flights page and return a list of matching deals.
    Each deal dict includes outbound_date and return_date for correct URL
    generation later (fixes upstream bug where last loop date was reused).
    """
    url = build_google_url(origin, destination, outbound_date, return_date)
    page = context.new_page()
    deals_raw = []
    try:
        page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
        page.goto(url, wait_until="domcontentloaded", timeout=20000)

        # Dismiss consent dialog if present
        for label in ["Accept all", "Reject all", "I agree"]:
            try:
                page.locator(f"button:has-text('{label}')").first.click(timeout=1000)
                page.wait_for_timeout(500)
                break
            except Exception:
                pass

        # Wait for prices to render (up to 10 s)
        try:
            page.wait_for_selector("text=/\\$\\d+/", timeout=10000)
        except Exception:
            pass

        deals_raw = extract_prices(page)

        if not deals_raw:
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


def _rfc2047(value):
    """Base64-encode a header value so non-ASCII characters are safe to send."""
    import base64
    return f"=?utf-8?b?{base64.b64encode(value.encode('utf-8')).decode('ascii')}?="


def send_ntfy(title, body, priority="default", click_url=None):
    """POST a notification to ntfy.sh over HTTPS (port 443)."""
    cfg = _cfg()
    url = f"{cfg['NTFY_SERVER']}/{cfg['NTFY_TOPIC']}"

    # ntfy free tier: 4096-byte body limit
    body_bytes = body.encode("utf-8")
    if len(body_bytes) > 4000:
        body_bytes = body.encode("utf-8")[:3900] + "\n\u2026(truncated)".encode("utf-8")

    req = urllib.request.Request(url, data=body_bytes, method="POST")
    req.add_header("Title", _rfc2047(title))
    req.add_header("Priority", priority)
    req.add_header("Tags", "airplane,ticket")
    if click_url:
        req.add_header("Click", click_url)

    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status not in (200, 201):
            raise RuntimeError(f"ntfy returned HTTP {resp.status}")


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

    send_ntfy(title, body, priority=priority, click_url=click_url)
    log.info("Notification sent via ntfy.sh")


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
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )
        context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
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

                log.info("%s \u2192 %s | max $%d", origin, destination, max_price)
                route_deals: list = []
                errors: list = []

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
                    time.sleep(2)

                route_deals = sorted(route_deals, key=lambda x: x["price"])
                date_strs = ", ".join(f"{o}\u2192{r}" for o, r in date_pairs)
                route_label = (
                    f"{origin} \u2192 {destination} (max ${max_price}) | {date_strs}"
                )

                if route_deals:
                    # Filter out deals already emailed in prior runs
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
                        # Deals exist but all already emailed previously
                        summary_lines.append(
                            f"NO {route_label}  (seen in prior run)"
                        )
                elif errors:
                    summary_lines.append(
                        f"!! {route_label}\n   Error: {errors[0]}"
                    )
                else:
                    summary_lines.append(f"NO {route_label}")
        finally:
            context.close()
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
