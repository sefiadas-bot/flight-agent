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

def find_deals(origin, destination, outbound_date, return_date, max_price, max_depart_hour, browser):
    url = build_google_url(origin, destination, outbound_date, return_date)
    deals = []

    page = browser.new_page()
    try:
        page.set_extra_http_headers({"Accept-Language": "en-US,en;q=0.9"})
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(6000)

        # Each flight result is a list item
        cards = page.locator("li").all()
        for card in cards:
            try:
                text = card.inner_text()
                price = parse_price(text)
                if price is None or price > max_price or price < 50:
                    continue

                # Parse times — first two HH:MM AM/PM matches = depart / arrive
                times = re.findall(r'\d{1,2}:\d{2}\s*(?:AM|PM)', text, re.IGNORECASE)
                dep_time = times[0] if times else ""
                arr_time = times[1] if len(times) > 1 else ""

                if max_depart_hour is not None and dep_time:
                    h = parse_hour(dep_time)
                    if h is not None and h > max_depart_hour:
                        continue

                # Stops: look for "Nonstop" or "1 stop"
                stops = "?"
                if re.search(r'nonstop', text, re.IGNORECASE):
                    stops = 0
                else:
                    sm = re.search(r'(\d+)\s+stop', text, re.IGNORECASE)
                    if sm:
                        stops = int(sm.group(1))

                # Duration
                dur_m = re.search(r'(\d+)\s*hr?\s*(?:(\d+)\s*min?)?', text, re.IGNORECASE)
                duration = f"{dur_m.group(1)}h {dur_m.group(2) or '0'}m" if dur_m else "?"

                # Airline — first non-empty line that looks like an airline name
                airline = "?"
                for line in text.splitlines():
                    line = line.strip()
                    if line and not re.search(r'[\$\d:]', line) and len(line) > 2:
                        airline = line
                        break

                deals.append({
                    "price":     price,
                    "airline":   airline,
                    "departure": dep_time,
                    "arrival":   arr_time,
                    "duration":  duration,
                    "stops":     stops,
                })
            except Exception:
                continue
    finally:
        page.close()

    # Deduplicate by price+departure
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
