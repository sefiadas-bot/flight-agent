import os
import json
import requests
from datetime import datetime

SERPAPI_KEY        = os.environ["SERPAPI_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

def load_searches():
    with open("searches.json") as f:
        return json.load(f)

def search_flights(origin, destination, outbound_date, return_date, stops=1):
    params = {
        "engine":        "google_flights",
        "departure_id":  origin,
        "arrival_id":    destination,
        "outbound_date": outbound_date,
        "return_date":   return_date,
        "currency":      "USD",
        "hl":            "en",
        "type":          1,
        "stops":         stops,
        "api_key":       SERPAPI_KEY,
    }
    resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()

def parse_flights(data, max_price, max_depart_hour, outbound_date, return_date):
    results = []
    for section in ("best_flights", "other_flights"):
        for flight in data.get(section, []):
            price = flight.get("price", 9999)
            if price > max_price:
                continue
            legs = flight.get("flights", [])
            if not legs:
                continue
            dep_time_str = legs[0].get("departure_airport", {}).get("time", "")
            try:
                dep_dt = datetime.strptime(dep_time_str, "%Y-%m-%d %H:%M")
                if dep_dt.hour > max_depart_hour or (dep_dt.hour == max_depart_hour and dep_dt.minute > 0):
                    continue
            except ValueError:
                pass
            results.append({
                "price":         price,
                "outbound_date": outbound_date,
                "return_date":   return_date,
                "legs":          legs,
            })
    return results

def fmt_leg(leg, label):
    dep = leg.get("departure_airport", {})
    arr = leg.get("arrival_airport", {})
    airline   = leg.get("airline", "")
    flight_no = leg.get("flight_number", "")
    try:
        dep_dt = datetime.strptime(dep.get("time", ""), "%Y-%m-%d %H:%M")
        arr_dt = datetime.strptime(arr.get("time", ""), "%Y-%m-%d %H:%M")
        dep_str = dep_dt.strftime("%a %d %b, %H:%M")
        arr_str = arr_dt.strftime("%H:%M")
    except ValueError:
        dep_str = dep.get("time", "?")
        arr_str = arr.get("time", "?")
    return f"  {label}: {dep_str} -> {arr_str}  ({airline} {flight_no})"

def build_message(origin, destination, max_price, flights):
    today = datetime.now().strftime("%d %b %Y")
    lines = [
        f"{origin} -> {destination} | {today}",
        f"Found {len(flights)} deal(s) under ${max_price}!\n",
    ]
    for i, flight in enumerate(flights, 1):
        price       = flight["price"]
        return_date = flight["return_date"]
        legs        = flight["legs"]
        leg_strs = [fmt_leg(legs[j], "Out" if j == 0 else f"Leg{j+1}") for j in range(len(legs))]
        leg_strs.append(f"  Ret: {return_date}")
        lines.append(f"#{i} - ${price}\n" + "\n".join(leg_strs))
    return "\n\n".join(lines)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id":  TELEGRAM_CHAT_ID,
        "text":     message,
        "disable_web_page_preview": True,
    }, timeout=15)
    resp.raise_for_status()

def main():
    searches = load_searches()
    print(f"Loaded {len(searches)} search(es).")

    for search in searches:
        origin          = search["origin"]
        destination     = search["destination"]
        max_price       = search["max_price"]
        max_depart_hour = search.get("max_depart_hour", 23)
        date_pairs      = search["date_pairs"]

        print(f"\n{origin} -> {destination} | max ${max_price} | depart <={max_depart_hour}:00")
        all_flights = []

        for outbound_date, return_date in date_pairs:
            outbound_date = outbound_date.replace(' ', '')
            return_date = return_date.replace(' ', '')
            print(f"  Checking {outbound_date} / {return_date} ...")
            try:
                data  = search_flights(origin, destination, outbound_date, return_date)
                found = parse_flights(data, max_price, max_depart_hour, outbound_date, return_date)
                all_flights.extend(found)
                print(f"    -> {len(found)} deal(s) found")
            except Exception as e:
                print(f"    -> Error: {e}")

        all_flights.sort(key=lambda f: f["price"])
        print(f"  Total: {len(all_flights)} deal(s)")

        if all_flights:
            message = build_message(origin, destination, max_price, all_flights)
            send_telegram(message)
            print("  Telegram notification sent.")
        else:
            print("  No deals - no notification sent.")

if __name__ == "__main__":
    main()
