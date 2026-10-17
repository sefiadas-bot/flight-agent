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

def click_field(page, selectors, text=None, timeout=5000):
    """Try multiple selectors until one works, then optionally fill text."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=timeout)
            el.click()
            if text is not None:
                el.fill(text)
            return el
        except Exception:
            continue
    raise Exception(f"None of these selectors worked: {selectors}")


def fill_search_form(page, origin, destination, outbound_date, return_date):
    """Navigate Google Flights by filling the search form like a real user."""
    page.goto("https://www.google.com/travel/flights?hl=en&curr=USD",
              wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(3000)

    # Dismiss cookie consent if present
    for label in ["Accept all", "Reject all", "I agree", "Accept"]:
        try:
            page.locator(f"button:has-text('{label}')").first.click(timeout=1500)
            page.wait_for_timeout(800)
            break
        except Exception:
            pass

    # --- Origin ---
    click_field(page, [
        '[placeholder="Where from?"]',
        '[placeholder="Leaving from?"]',
        '[aria-label="Where from?"]',
        '[aria-label="Leaving from?"]',
        'input[aria-autocomplete="list"]',
    ], text=origin)
    page.wait_for_timeout(1500)
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    page.wait_for_timeout(1000)

    # --- Destination ---
    click_field(page, [
        '[placeholder="Where to?"]',
        '[placeholder="Going to?"]',
        '[aria-label="Where to?"]',
        '[aria-label="Going to?"]',
        '[aria-label="Destination"]',
    ], text=destination)
    page.wait_for_timeout(1500)
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    page.wait_for_timeout(1000)

    # --- Outbound date ---
    click_field(page, [
        '[placeholder="Departure"]',
        '[aria-label="Departure"]',
        '[placeholder="Outbound date"]',
    ])
    page.keyboard.type(outbound_date, delay=60)
    page.wait_for_timeout(800)

    # --- Return date ---
    click_field(page, [
        '[placeholder="Return"]',
        '[aria-label="Return"]',
        '[placeholder="Return date"]',
    ])
    page.keyboard.type(return_date, delay=60)
    page.wait_for_timeout(800)

    # Done / Search
    for btn in ["Done", "Search"]:
        try:
            page.locator(f"button:has-text('{btn}')").first.click(timeout=3000)
            page.wait_for_timeout(500)
        except Exception:
            pass
    page.wait_for_timeout(8000)


def find_deals(origin, destination, outbound_date, return_date, max_price, max_depart_hour, browser):
    deals = []
    page = browser.new_page()
    try:
        page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
        fill_search_form(page, origin, destination, outbound_date, return_date)

        # Results are in list items — collect all text blocks
        cards = page.locator("li").all()
        for card in cards:
            try:
                text = card.inner_text()
                price = parse_price(text)
                if price is None or price > max_price or price < 50:
                    continue

                times = re.findall(r'\d{1,2}:\d{2}\s*(?:AM|PM)', text, re.IGNORECASE)
                dep_time = times[0] if times else ""
                arr_time = times[1] if len(times) > 1 else ""

                if max_depart_hour is not None and dep_time:
                    h = parse_hour(dep_time)
                    if h is not None and h > max_depart_hour:
                        continue

                stops = "?"
                if re.search(r'nonstop', text, re.IGNORECASE):
                    stops = 0
                else:
                    sm = re.search(r'(\d+)\s+stop', text, re.IGNORECASE)
                    if sm:
                        stops = int(sm.group(1))

                dur_m = re.search(r'(\d+)\s*hr?\s*(?:(\d+)\s*min?)?', text, re.IGNORECASE)
                duration = f"{dur_m.group(1)}h {dur_m.group(2) or '0'}m" if dur_m else "?"

                airline = "?"
                for line in text.splitlines():
                    line = line.strip()
                    if line and not re.search(r'[\$\d:]', line) and len(line) > 2:
                        airline = line
                        break

                deals.append({
                    "price": price, "airline": airline,
                    "departure": dep_time, "arrival": arr_time,
                    "duration": duration, "stops": stops,
                })
            except Exception:
                continue
    finally:
        page.close()

    seen, unique = set(), []
    for d in sorted(deals, key=lambda x: x["price"]):
        key = (d["price"], d["departure"])
        if key not in seen:
            seen.add(key)
            unique.append(d)
    return unique

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
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )

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
                                       max_price, max_depart_hour, browser)
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
