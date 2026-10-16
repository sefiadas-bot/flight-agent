import os
import json
import time
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from fast_flights import FlightData, Passengers, create_filter, get_flights

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

def parse_price(price_str):
    try:
        return int(str(price_str).replace("$", "").replace(",", "").strip())
    except Exception:
        return 9999

def parse_hour(time_str):
    for fmt in ("%I:%M %p", "%H:%M"):
        try:
            return datetime.strptime(str(time_str).strip(), fmt).hour
        except ValueError:
            continue
    return 0

def find_deals(origin, destination, outbound_date, return_date, max_price, max_depart_hour):
    f = create_filter(
        flight_data=[
            FlightData(date=outbound_date, from_airport=origin,      to_airport=destination),
            FlightData(date=return_date,   from_airport=destination, to_airport=origin),
        ],
        trip="round-trip",
        seat="economy",
        passengers=Passengers(adults=1),
    )
    result = get_flights(f)

    deals = []
    for flight in result.flights:
        price = parse_price(getattr(flight, "price", 9999))
        if price > max_price:
            continue
        dep = getattr(flight, "departure", "")
        if max_depart_hour is not None and dep:
            if parse_hour(dep) > max_depart_hour:
                continue
        deals.append({
            "price":     price,
            "airline":   getattr(flight, "airline",   "?"),
            "departure": dep,
            "arrival":   getattr(flight, "arrival",   "?"),
            "duration":  getattr(flight, "duration",  "?"),
            "stops":     getattr(flight, "stops",     "?"),
        })
    return sorted(deals, key=lambda x: x["price"])

def build_google_url(origin, destination, outbound_date, return_date):
    return (
        f"https://www.google.com/flights?hl=en"
        f"#flt={origin}.{destination}.{outbound_date}"
        f"*{destination}.{origin}.{return_date};c:USD;e:1;sd:1;t:f"
    )

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

    today     = datetime.now().strftime("%d %b %Y")
    any_deals = False
    summary   = [f"Flight Check — {today}\n"]
    deal_sections = []

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
                found = find_deals(origin, destination, outbound_date, return_date, max_price, max_depart_hour)
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
                f"{best['departure']} → {best['arrival']} | {best['stops']} stop(s)"
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

    summary.append("\n🎉 Book now — prices change fast!" if any_deals else "\n😔 No deals today.")
    body = "\n\n".join(summary)
    if deal_sections:
        body += "\n\n\n" + "\n\n".join(deal_sections)

    subject = f"✈ Flight Deals Found — {today}" if any_deals else f"✈ No Deals Today — {today}"
    send_email(subject, body)
    print(f"\nEmail sent → {EMAIL_TO}")

if __name__ == "__main__":
    main()
