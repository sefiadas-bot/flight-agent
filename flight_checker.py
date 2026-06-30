import os
import requests
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
SERPAPI_KEY        = os.environ["SERPAPI_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

ORIGIN      = "TLV"
DESTINATION = "BUD"
MAX_PRICE   = 400
CURRENCY    = "USD"

# 3 representative ~7-night trips → 3 API calls/day ≈ 90/month (free tier = 100)
DATE_PAIRS = [
    ("2026-09-19", "2026-09-26"),
    ("2026-09-21", "2026-09-28"),
    ("2026-09-23", "2026-09-30"),
]
# ─────────────────────────────────────────────────────────────────────────────


def search_flights(outbound_date: str, return_date: str) -> list[dict]:
    params = {
        "engine":         "google_flights",
        "departure_id":   ORIGIN,
        "arrival_id":     DESTINATION,
        "outbound_date":  outbound_date,
        "return_date":    return_date,
        "currency":       CURRENCY,
        "hl":             "en",
        "type":           "1",      # round trip
        "stops":          "1",      # nonstop only
        "outbound_times": "0,840",  # depart between 00:00 and 14:00 (840 min from midnight)
        "api_key":        SERPAPI_KEY,
    }

    resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    results = []
    for section in ("best_flights", "other_flights"):
        for flight in data.get(section, []):
            price = flight.get("price", 9999)
            if price <= MAX_PRICE:
                results.append({
                    "price":         price,
                    "outbound_date": outbound_date,
                    "return_date":   return_date,
                    "legs":          flight.get("flights", []),
                })
    return results


def fmt_leg(leg: dict, label: str) -> str:
    dep      = leg.get("departure_airport", {})
    arr      = leg.get("arrival_airport", {})
    airline  = leg.get("airline", "")
    flight_no = leg.get("flight_number", "")

    # SerpApi returns times as "2026-09-19 08:30"
    try:
        dep_dt = datetime.strptime(dep.get("time", ""), "%Y-%m-%d %H:%M")
        arr_dt = datetime.strptime(arr.get("time", ""), "%Y-%m-%d %H:%M")
        dep_str = dep_dt.strftime("%a %d %b, %H:%M")
        arr_str = arr_dt.strftime("%H:%M")
    except ValueError:
        dep_str = dep.get("time", "?")
        arr_str = arr.get("time", "?")

    return f"  {label}: {dep_str} \u2192 {arr_str}  ({airline} {flight_no})"


def build_message(all_flights: list[dict]) -> str | None:
    if not all_flights:
        return None

    today = datetime.now().strftime("%d %b %Y")
    lines = [
        f"\u2708\ufe0f *TLV \u2192 BUD Flight Alert* | {today}",
        f"Found *{len(all_flights)}* deal(s) under ${MAX_PRICE}!\n",
    ]

    for i, flight in enumerate(all_flights, 1):
        price = flight["price"]
        legs  = flight["legs"]

        # Nonstop round trip: legs[0] = outbound (TLV→BUD), legs[1] = return (BUD→TLV)
        out_str = fmt_leg(legs[0], "\U0001f6eb Out") if len(legs) > 0 else "  \U0001f6eb Out: N/A"
        ret_str = fmt_leg(legs[1], "\U0001f6ec Ret") if len(legs) > 1 else "  \U0001f6ec Ret: N/A"

        lines.append(
            f"*#{i} \u2014 ${price}*\n"
            f"{out_str}\n"
            f"{ret_str}"
        )

    return "\n\n".join(lines)


def send_telegram(message: str) -> None:
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        json={
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     message,
            "parse_mode":               "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    resp.raise_for_status()


def main() -> None:
    print(f"Searching {ORIGIN} \u2192 {DESTINATION} | max ${MAX_PRICE} | direct | depart \u226414:00")

    all_flights: list[dict] = []
    for outbound_date, return_date in DATE_PAIRS:
        print(f"  Checking {outbound_date} / {return_date} ...")
        try:
            found = search_flights(outbound_date, return_date)
            all_flights.extend(found)
            print(f"    \u2192 {len(found)} deal(s) found")
        except Exception as e:
            print(f"    \u2192 Error: {e}")

    all_flights.sort(key=lambda f: f["price"])

    print(f"\nTotal deals under ${MAX_PRICE}: {len(all_flights)}")

    message = build_message(all_flights)
    if message:
        send_telegram(message)
        print("Telegram notification sent.")
    else:
        print("No deals today \u2014 no notification sent.")


if __name__ == "__main__":
    main()
