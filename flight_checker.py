import os
import json
import time
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from amadeus import Client, ResponseError

amadeus = Client(
    client_id=os.environ["AMADEUS_CLIENT_ID"],
    client_secret=os.environ["AMADEUS_CLIENT_SECRET"],
)

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

def find_deals(origin, destination, outbound_date, return_date, max_price, max_depart_hour):
    response = amadeus.shopping.flight_offers_search.get(
        originLocationCode=origin,
        destinationLocationCode=destination,
        departureDate=outbound_date,
        returnDate=return_date,
        adults=1,
        currencyCode="USD",
        max=10,
        nonStop=False,
    )

    deals = []
    for offer in response.data:
        price = float(offer["price"]["grandTotal"])
        if price > max_price:
            continue

        outbound_itin = offer["itineraries"][0]
        first_seg     = outbound_itin["segments"][0]
        dep_at        = first_seg["departure"]["at"]   # "2027-04-01T05:40:00"
        dep_hour      = datetime.fromisoformat(dep_at).hour

        if max_depart_hour is not None and dep_hour > max_depart_hour:
            continue

        airline    = offer.get("validatingAirlineCodes", ["?"])[0]
        out_stops  = len(outbound_itin["segments"]) - 1
        dep_time   = dep_at[11:16]
        arr_time   = outbound_itin["segments"][-1]["arrival"]["at"][11:16]
        duration   = outbound_itin.get("duration", "")

        deals.append({
            "price":    int(price),
            "airline":  airline,
            "departure": dep_time,
            "arrival":   arr_time,
            "duration":  duration.replace("PT", "").replace("H", "h ").replace("M", "m"),
            "stops":     out_stops,
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

    today         = datetime.now().strftime("%d %b %Y")
    any_deals     = False
    summary       = [f"Flight Check — {today}\n"]
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
            except ResponseError as e:
                errors.append(str(e))
                print(f"    -> Error: {e}")
            time.sleep(1)

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

    summary.append("\n🎉 Book now — prices change fast!" if any_deals else "\n😔 No deals today.")
    body = "\n\n".join(summary)
    if deal_sections:
        body += "\n\n\n" + "\n\n".join(deal_sections)

    subject = f"✈ Flight Deals Found — {today}" if any_deals else f"✈ No Deals Today — {today}"
    send_email(subject, body)
    print(f"\nEmail sent → {EMAIL_TO}")

if __name__ == "__main__":
    main()
