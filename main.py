"""
Dashboard – Weather + Fio Banka SK account balance.
Serves HTTPS, compatible with Opera Mini Native v4.4.
"""

import os
import ssl
from datetime import datetime
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv
from flask import Flask, render_template

load_dotenv()

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config from .env
# ---------------------------------------------------------------------------
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")
WEATHER_CITY    = os.getenv("WEATHER_CITY", "Bratislava")
WEATHER_UNITS   = os.getenv("WEATHER_UNITS", "metric")  # metric | imperial
WEATHER_API_URL = os.getenv(
    "WEATHER_API_URL", "https://api.openweathermap.org/data/2.5/weather"
)

FIO_API_TOKEN   = os.getenv("FIO_API_TOKEN", "")

HOST     = os.getenv("HOST", "0.0.0.0")
PORT     = int(os.getenv("PORT", "4443"))
SSL_CERT = os.getenv("SSL_CERT", "")
SSL_KEY  = os.getenv("SSL_KEY", "")

UNIT_LABEL = {"metric": "C", "imperial": "F"}.get(WEATHER_UNITS, "C")

# ---------------------------------------------------------------------------
# Data fetching helpers
# ---------------------------------------------------------------------------

def fetch_weather():
    """Return a dict with weather data or {'error': str}."""
    if not WEATHER_API_KEY:
        return {"error": "WEATHER_API_KEY not set in .env"}
    try:
        params = {
            "q": WEATHER_CITY,
            "appid": WEATHER_API_KEY,
            "units": WEATHER_UNITS,
        }
        r = requests.get(WEATHER_API_URL, params=params, timeout=10)
        d = r.json()

        # OpenWeather returns details in JSON for non-2xx responses.
        if r.status_code >= 400:
            message = d.get("message") if isinstance(d, dict) else None
            if message:
                return {"error": f"OpenWeather API error: {message}"}
        r.raise_for_status()

        weather_items = d.get("weather") if isinstance(d, dict) else None
        weather_desc = ""
        if isinstance(weather_items, list) and weather_items:
            weather_desc = weather_items[0].get("description", "")

        main = d.get("main", {}) if isinstance(d, dict) else {}
        wind = d.get("wind", {}) if isinstance(d, dict) else {}

        return {
            "city":    d.get("name", WEATHER_CITY),
            "desc":    weather_desc.capitalize(),
            "temp":    round(float(main.get("temp", 0)), 1),
            "feels":   round(float(main.get("feels_like", 0)), 1),
            "humidity":int(main.get("humidity", 0)),
            "wind":    round(float(wind.get("speed", 0)), 1),
        }
    except requests.RequestException as exc:
        return {"error": str(exc)}
    except (KeyError, ValueError) as exc:
        return {"error": f"Unexpected response: {exc}"}


def fetch_bank():
    """Return account balance and today's latest transactions or {'error': str}."""
    if not FIO_API_TOKEN:
        return {"error": "FIO_API_TOKEN not set in .env"}
    try:
        today = datetime.now().date()
        start_of_month = today.replace(day=1)
        url = (
            f"https://fioapi.fio.cz/v1/rest/periods/{FIO_API_TOKEN}"
            f"/{start_of_month:%Y-%m-%d}/{today:%Y-%m-%d}"
            "/transactions.xml"
        )
        r = requests.get(url, timeout=15)
        r.raise_for_status()

        root = ET.fromstring(r.text)
        info = root.find("Info")
        if info is None:
            info = root.find("info")
        if info is None:
            info = root.find(".//Info")
        if info is None:
            return {"error": "XML response missing Info section"}

        def get_text(tag: str, default: str = "") -> str:
            node = info.find(tag)
            if node is None:
                node = info.find(tag.lower())
            if node is None or node.text is None:
                return default
            return node.text.strip()

        transactions = []
        for tx in root.findall(".//Transaction"):
            date_text = (tx.findtext("column_0") or "").strip()
            if not date_text:
                continue

            movement_id_text = (tx.findtext("column_22") or "0").strip()
            try:
                movement_id = int(movement_id_text)
            except ValueError:
                movement_id = 0

            amount_text = (tx.findtext("column_1") or "0").strip().replace(",", ".")
            try:
                amount = round(float(amount_text), 2)
            except ValueError:
                amount = 0.0

            tx_type = (tx.findtext("column_8") or "").strip()
            counterparty = (tx.findtext("column_10") or "").strip()
            note = (tx.findtext("column_16") or tx.findtext("column_25") or "").strip()
            tx_currency = (tx.findtext("column_14") or "").strip()

            transactions.append(
                {
                    "movement_id": movement_id,
                    "date": date_text,
                    "amount": amount,
                    "currency": tx_currency,
                    "type": tx_type,
                    "counterparty": counterparty,
                    "note": note,
                }
            )

        transactions.sort(key=lambda x: x["movement_id"], reverse=True)
        latest_transactions = transactions[:5]

        closing_balance = get_text("closingBalance", "0").replace(",", ".")
        return {
            "account":  get_text("accountId", ""),
            "iban":     get_text("iban", "N/A"),
            "balance":  round(float(closing_balance), 2),
            "currency": get_text("currency", ""),
            "transactions": latest_transactions,
        }
    except requests.RequestException as exc:
        return {"error": str(exc)}
    except ET.ParseError as exc:
        return {"error": f"Invalid XML response: {exc}"}
    except (ValueError, TypeError) as exc:
        return {"error": f"Unexpected response: {exc}"}


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"ok": True}, 200

@app.route("/")
def dashboard():
    weather = fetch_weather()
    bank    = fetch_bank()
    now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return render_template(
        "dashboard.html",
        now=now,

        # weather
        weather_city    = weather.get("city", WEATHER_CITY),
        weather_error   = weather.get("error"),
        weather_desc    = weather.get("desc", ""),
        weather_temp    = weather.get("temp", ""),
        weather_feels   = weather.get("feels", ""),
        weather_humidity= weather.get("humidity", ""),
        weather_wind    = weather.get("wind", ""),
        weather_unit    = UNIT_LABEL,

        # bank
        bank_error    = bank.get("error"),
        bank_account  = bank.get("account", ""),
        bank_iban     = bank.get("iban", ""),
        bank_balance  = bank.get("balance", ""),
        bank_currency = bank.get("currency", ""),
        bank_transactions = bank.get("transactions", []),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Starting dashboard on http://{HOST}:{PORT}/")
    app.run(host=HOST, port=PORT, debug=False)
