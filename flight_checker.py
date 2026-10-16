import os
import json
import time
import smtplib
import requests
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

TRAVELPAYOUTS_TOKEN = os.environ["TRAVELPAYOUTS_TOKEN"]
EMAIL_FROM          = os.environ["EMAIL_FROM"]
EMAIL_TO            = os.environ["EMAIL_TO"]
EMAIL_APP_PASSWORD  = os.environ["EMAIL_APP_PASSWORD"]

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

def search_cheapest(origin, destination, depart_month, return_month):
    for attempt in range(4):
        resp = requests.get(
            "https://api.travelpayouts.com/v1/prices/cheap",
            params={
                "origin":      origin,
                "destination": destination,
                "depart_date": depart_month,
                "return_date": return_month,
                "currency":    "usd",
                "token":       TRAVELPAYOUTS_TOKEN,
            },
            timeout=30
        )
        if resp.status_code == 429:
            wait = 10 * (attempt + 1)
            print(f"    Rate limited — waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise Exception("Too many retries (429)")

def find_deals(origin, destination, outbound_date, return_date, max_price):
    depart_month = outbound_date[:7]  # YYYY-MM
    return_month  = return_date[:7]

    data = search_cheapest(origin, destination, depart_month, return_month)
    if not data.get("success") or not data.get("data"):
        return []

    results = []
    for dest_code, info in data["data"].items():
        price = info.get("price", 9999)
        if price > max_price:
            continue
        dep_at = info.get("departure_at", "")
        ret_at = info.get("return_at", "")
        raw_link = info.get("link", "")
        results.append({
            "price":         price,
            "destination":   dest_code,
            "outbound_date": dep_at[:10] if dep_at else outbound_date,
            "return_date":   ret_at[:10] if ret_at else return_date,
            "airline":       info.get("airline", "?"),
            "transfers":     info.get("transfers", 0),
            "aviasales_url": "https://www.aviasales.com" + raw_link if raw_link else "",
        })
    return results

def build_google_url(origin, destination, outbound_date, return_date):
    return (
        f"https://www.google.com/flights?hl=en"
        f"#flt={origin}.{destination}.{outbound_date}"
        f"*{destination}.{origin}.{return_date};c:USD;e:1;sd:1;t:f"
    )

def send_email(subject, body):
    msg = MIMEMultipart()
    msg['Subject'] = subject
    msg['From']    = EMAIL_FROM
    msg['To']      = EMAIL_TO
    msg.attach(MIMEText(body, 'plain', 'utf-8'))
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
        server.send_message(msg)

def main():
    raw_searches = load_searches()

    # Expand "ALL" destination
    searches = []
    for s in raw_searches:
        if s["destination"].upper() == "ALL":
            for dest in EXPLORE_DESTINATIONS:
                searches.append({**s, "destination": dest})
        else:
            searches.append(s)

    print(f"Loaded {len(raw_searches)} config(s) → {len(searches)} search(es).")

    today     = datetime.now().strftime("%d %b %Y")
    any_deals = False
    summary   = [f"Flight Check — {today}\n"]
    deal_sections = []

    for search in searches:
        origin          = search["origin"]
        destination     = search["destination"]
        max_price       = search["max_price"]
        date_pairs      = search["date_pairs"]

        print(f"\n{origin} -> {destination} | max ${max_price}")
        route_deals = []
        errors      = []

        for outbound_date, return_date in date_pairs:
            outbound_date = outbound_date.strip()
            return_date   = return_date.strip()
            print(f"  Checking {outbound_date} / {return_date} ...")
            try:
                found = find_deals(origin, destination, outbound_date, return_date, max_price)
                route_deals.extend(found)
                print(f"    -> {len(found)} deal(s)")
            except Exception as e:
                errors.append(str(e))
                print(f"    -> Error: {e}")
            time.sleep(1)

        # Deduplicate and sort by price
        seen, unique = set(), []
        for d in sorted(route_deals, key=lambda x: x["price"]):
            key = (d["destination"], d["outbound_date"], d["return_date"])
            if key not in seen:
                seen.add(key)
                unique.append(d)
        route_deals = unique

        date_strs  = ", ".join(f"{o}→{r}" for o, r in date_pairs)
        route_line = f"{origin} → {destination} (max ${max_price}) | {date_strs}"

        if route_deals:
            any_deals = True
            best = route_deals[0]
            summary.append(f"✅ {route_line}\n   Best: ${best['price']} | {best['airline']} | {best['transfers']} stop(s)")
            section = [f"{'='*55}", f"✈  {origin} → {destination}  |  max ${max_price}", f"{'='*55}"]
            for i, d in enumerate(route_deals, 1):
                goog = build_google_url(origin, d["destination"], d["outbound_date"], d["return_date"])
                section.append(
                    f"\n#{i}  ${d['price']} round trip\n"
                    f"    Airline : {d['airline']}  |  {d['transfers']} stop(s)\n"
                    f"    Out     : {d['outbound_date']}\n"
                    f"    Return  : {d['return_date']}\n"
                    f"    Google  : {goog}\n"
                    + (f"    Aviasales: {d['aviasales_url']}\n" if d['aviasales_url'] else "")
                )
            deal_sections.append("\n".join(section))
        elif errors:
            summary.append(f"⚠️  {route_line}\n   Error: {errors[0]}")
        else:
            summary.append(f"❌ {route_line}")

    footer = "\n🎉 Book now — prices change fast!" if any_deals else "\n😔 No deals today. Will check again tomorrow."
    summary.append(footer)

    body    = "\n\n".join(summary)
    if deal_sections:
        body += "\n\n\n" + "\n\n".join(deal_sections)

    subject = f"✈ Flight Deals Found — {today}" if any_deals else f"✈ No Deals Today — {today}"
    send_email(subject, body)
    print(f"\nEmail sent → {EMAIL_TO}")

if __name__ == "__main__":
    main()
