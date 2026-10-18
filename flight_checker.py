import os
import re
import json
import time
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from playwright.sync_api import sync_playwright

EMAIL_FROM         = os.environ["EMAIL_FROM"]
EMAIL_TO           = os.environ["EMAIL_TO"]
EMAIL_APP_PASSWORD = os.environ["EMAIL_APP_PASSWORD"]

EXPLORE_DESTINATIONS = [
    # Germany
    "MUC", "FRA", "BER", "DUS", "HAM", "STR", "CGN", "NUE", "HAJ", "BRE",
    # Popular Europe
    "AMS", "BCN", "FCO", "VIE", "PRG", "BUD", "ATH",
    "WAW", "MAD", "LIS", "SOF", "LHR", "LGW", "ARN",
    "DUB", "ZRH", "MXP", "NAP", "OPO", "VAR", "SKG",
    "LJU", "ZAG", "KRK", "SZG",
]

def load_searches():
    with open("searches.json") as f:
        return json.load(f)

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
    """Extract all price+time pairs visible on the current Google Flights page."""
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
                stops = 0 if re.search(r'nonstop', text, re.IGNORECASE) else (
                    int(m.group(1)) if (m := re.search(r'(\d+)\s+stop', text, re.IGNORECASE)) else "?")
                dur = re.search(r'(\d+)\s*hr?\s*(?:(\d+)\s*min?)?', text, re.IGNORECASE)
                airline = next((l.strip() for l in text.splitlines()
                                if l.strip() and not re.search(r'[\$\d:]', l) and len(l.strip()) > 2), "?")
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


def find_deals(origin, destination, outbound_date, return_date, max_price, max_depart_hour, context):
    url = build_google_url(origin, destination, outbound_date, return_date)
    deals_raw = []
    page = context.new_page()
    try:
        page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})

        # Load page — domcontentloaded is faster and sufficient; networkidle hangs on SPAs
        page.goto(url, wait_until="domcontentloaded", timeout=20000)

        # Dismiss consent dialog if present
        for label in ["Accept all", "Reject all", "I agree"]:
            try:
                page.locator(f"button:has-text('{label}')").first.click(timeout=1000)
                page.wait_for_timeout(500)
                break
            except Exception:
                pass

        # Wait for prices to render (up to 10s)
        try:
            page.wait_for_selector("text=/\\$\\d+/", timeout=10000)
        except Exception:
            pass

        deals_raw = extract_prices(page)

        # Save debug screenshot if empty (uploaded as CI artifact)
        if not deals_raw:
            shot = f"debug_{origin}_{destination}_{outbound_date}.png"
            page.screenshot(path=shot, full_page=True)
            print(f"    -> no results; screenshot saved: {shot}")
    finally:
        page.close()

    # Filter and deduplicate
    deals = []
    seen = set()
    for d in sorted(deals_raw, key=lambda x: x["price"]):
        if d["price"] > max_price:
            continue
        if max_depart_hour is not None and d["departure"]:
            h = parse_hour(d["departure"])
            if h is not None and h > max_depart_hour:
                continue
        key = (d["price"], d["departure"])
        if key not in seen:
            seen.add(key)
            deals.append(d)
    return deals


def send_email(subject, body):
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(body, "plain", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
        server.send_message(msg)

def main():
    raw_searches = load_searches()

    searches = []
    for s in raw_searches:
        if s["destination"].upper() == "ALL":
            for dest in EXPLORE_DESTINATIONS:
                searches.append({**s, "destination": dest})
        else:
            searches.append(s)

    print(f"Loaded {len(raw_searches)} config(s) → {len(searches)} search(es).")

    today         = datetime.now().strftime("%d %b %Y")
    any_deals     = False
    summary       = [f"Flight Check — {today}\n"]
    deal_sections = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ]
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
        # Hide webdriver fingerprint
        context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        for search in searches:
            origin          = search["origin"]
            destination     = search["destination"]
            max_price       = search["max_price"]
            max_depart_hour = search.get("max_depart_hour", 23)
            date_pairs      = search["date_pairs"]

            print(f"\n{origin} -> {destination} | max ${max_price}")
            route_deals = []
            errors      = []

            for outbound_date, return_date in date_pairs:
                outbound_date = outbound_date.strip()
                return_date   = return_date.strip()
                print(f"  Checking {outbound_date} / {return_date} ...")
                try:
                    found = find_deals(origin, destination, outbound_date, return_date,
                                       max_price, max_depart_hour, context)
                    route_deals.extend(found)
                    print(f"    -> {len(found)} deal(s)")
                except Exception as e:
                    errors.append(str(e))
                    print(f"    -> Error: {e}")
                time.sleep(2)

            route_deals = sorted(route_deals, key=lambda x: x["price"])
            date_strs   = ", ".join(f"{o}→{r}" for o, r in date_pairs)
            route_line  = f"{origin} → {destination} (max ${max_price}) | {date_strs}"

            if route_deals:
                any_deals = True
                best = route_deals[0]
                summary.append(
                    f"✅ {route_line}\n"
                    f"   Best: ${best['price']} | {best['airline']} | "
                    f"{best['departure']}→{best['arrival']} | {best['stops']} stop(s)"
                )
                section = [f"{'='*55}", f"✈  {origin} → {destination}  |  max ${max_price}", f"{'='*55}"]
                for i, d in enumerate(route_deals, 1):
                    url = build_google_url(origin, destination, outbound_date, return_date)
                    section.append(
                        f"\n#{i}  ${d['price']} round trip\n"
                        f"    Airline  : {d['airline']}\n"
                        f"    Depart   : {d['departure']}  →  Arrive: {d['arrival']}\n"
                        f"    Duration : {d['duration']}  |  Stops: {d['stops']}\n"
                        f"    Book     : {url}\n"
                    )
                deal_sections.append("\n".join(section))
            elif errors:
                summary.append(f"⚠️  {route_line}\n   Error: {errors[0]}")
            else:
                summary.append(f"❌ {route_line}")

        context.close()
        browser.close()

    summary.append("\n🎉 Book now — prices change fast!" if any_deals else "\n😔 No deals today.")
    body = "\n\n".join(summary)
    if deal_sections:
        body += "\n\n\n" + "\n\n".join(deal_sections)

    subject = f"✈ Flight Deals Found — {today}" if any_deals else f"✈ No Deals Today — {today}"
    send_email(subject, body)
    print(f"\nEmail sent → {EMAIL_TO}")

if __name__ == "__main__":
    main()
