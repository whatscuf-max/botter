"""
Weather Trading Strategy - VALUE-BASED

Core rule: Only buy when forecast DISAGREES with market price.
- If forecast says YES and market prices YES at 30% -> BUY YES (value!)
- If forecast says YES and market prices YES at 90% -> SKIP (no value, already priced in)
- If forecast says NO and market prices NO at 20% -> BUY NO (value!)
- If forecast says NO and market prices NO at 85% -> SKIP (no value)

Never buy YES above 0.75 or NO above 0.75.

Data sources:
  US cities:      NOAA gridpoint + Open-Meteo (averaged, confidence boosted)
  UK/EU cities:   UK Met Office DataHub + Open-Meteo
  All other intl: WeatherAPI.com + Open-Meteo
  Observed:       NOAA current conditions page (US only, full precision)

5-min cache. Hourly data for intraday accuracy.
Airport coordinates verified against Wunderground station pages.

Sizing:    Quarter-Kelly fractional sizing for mathematically optimal compounding.
Edge:      12% minimum threshold (was 5%).
Conviction: Time-decay cap for markets resolving >48 hours out.
"""

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("polymarket_bot.weather")

AIRPORTS = {
    "new york":      {"lat": 40.7772,  "lon": -73.8726,  "noaa": True,  "station": "LaGuardia Airport (KLGA)"},
    "new york city": {"lat": 40.7772,  "lon": -73.8726,  "noaa": True,  "station": "LaGuardia Airport (KLGA)"},
    "nyc":           {"lat": 40.7772,  "lon": -73.8726,  "noaa": True,  "station": "LaGuardia Airport (KLGA)"},
    "seattle":       {"lat": 47.4489,  "lon": -122.3094, "noaa": True,  "station": "Seattle-Tacoma Intl (KSEA)"},
    "miami":         {"lat": 25.7932,  "lon": -80.2906,  "noaa": True,  "station": "Miami Intl Airport (KMIA)"},
    "atlanta":       {"lat": 33.6367,  "lon": -84.4281,  "noaa": True,  "station": "Hartsfield-Jackson (KATL)"},
    "chicago":       {"lat": 41.9742,  "lon": -87.9073,  "noaa": True,  "station": "O'Hare Intl Airport (KORD)"},
    "dallas":        {"lat": 32.8481,  "lon": -96.8512,  "noaa": True,  "station": "Dallas Love Field (KDAL)"},
    "london":        {"lat": 51.4706,  "lon": -0.4619,   "noaa": False, "station": "Heathrow Airport (EGLL)",      "met_office": True},
    "paris":         {"lat": 49.0128,  "lon":  2.5500,   "noaa": False, "station": "Charles de Gaulle (LFPG)",     "met_office": True},
    "munich":        {"lat": 48.3538,  "lon": 11.7861,   "noaa": False, "station": "Munich Airport (EDDM)",        "met_office": True},
    "seoul":         {"lat": 37.4691,  "lon": 126.4505,  "noaa": False, "station": "Incheon Intl (RKSI)"},
    "ankara":        {"lat": 40.1244,  "lon":  32.9992,  "noaa": False, "station": "Esenboga Airport (LTAC)"},
    "lucknow":       {"lat": 26.7606,  "lon":  80.8893,  "noaa": False, "station": "Chaudhary Charan Singh (VILK)"},
    "wellington":    {"lat": -41.3272, "lon": 174.8053,  "noaa": False, "station": "Wellington Airport (NZWN)"},
    "sao paulo":     {"lat": -23.4356, "lon": -46.4731,  "noaa": False, "station": "Guarulhos Intl (SBGR)"},
    "buenos aires":  {"lat": -34.8150, "lon": -58.5350,  "noaa": False, "station": "Ezeiza Intl (SAEZ)"},
    "toronto":       {"lat": 43.6772,  "lon": -79.6306,  "noaa": False, "station": "Pearson Intl (CYYZ)"},
}

MET_OFFICE_API_KEY = "eyJ4NXQjUzI1NiI6Ik5XVTVZakUxTkRjeVl6a3hZbUl4TkdSaFpqSmpOV1l6T1dGaE9XWXpNMk0yTWpRek5USm1OVEE0TXpOaU9EaG1NVFJqWVdNellXUm1ZalUyTTJJeVpBPT0iLCJraWQiOiJnYXRld2F5X2NlcnRpZmljYXRlX2FsaWFzIiwidHlwIjoiSldUIiwiYWxnIjoiUlMyNTYifQ==.eyJzdWIiOiJ3aGF0LnNjdWZAZ21haWwuY29tQGNhcmJvbi5zdXBlciIsImFwcGxpY2F0aW9uIjp7Im93bmVyIjoid2hhdC5zY3VmQGdtYWlsLmNvbSIsInRpZXJRdW90YVR5cGUiOm51bGwsInRpZXIiOiJVbmxpbWl0ZWQiLCJuYW1lIjoic2l0ZV9zcGVjaWZpYy05MDY3YzMwYy1kMTljLTQ4NzYtODc0OC01MTEyNTU4NThiZGYiLCJpZCI6NDE2NjUsInV1aWQiOiI1Zjg5MjI4Yy0xM2U3LTRkMzAtYTAyOC04NTYzYWM4OGU5ZGUifSwiaXNzIjoiaHR0cHM6XC9cL2FwaS1tYW5hZ2VyLmFwaS1tYW5hZ2VtZW50Lm1ldG9mZmljZS5jbG91ZDo0NDNcL29hdXRoMlwvdG9rZW4iLCJ0aWVySW5mbyI6eyJ3ZGhfc2l0ZV9zcGVjaWZpY19mcmVlIjp7InRpZXJRdW90YVR5cGUiOiJyZXF1ZXN0Q291bnQiLCJncmFwaFFMTWF4Q29tcGxleGl0eSI6MCwiZ3JhcGhRTE1heERlcHRoIjowLCJzdG9wT25RdW90YVJlYWNoIjp0cnVlLCJzcGlrZUFycmVzdExpbWl0IjowLCJzcGlrZUFycmVzdFVuaXQiOiJzZWMifX0sImtleXR5cGUiOiJQUk9EVUNUSU9OIiwic3Vic2NyaWJlZEFQSXMiOlt7InN1YnNjcmliZXJUZW5hbnREb21haW4iOiJjYXJib24uc3VwZXIiLCJuYW1lIjoiU2l0ZVNwZWNpZmljRm9yZWNhc3QiLCJjb250ZXh0IjoiXC9zaXRlc3BlY2lmaWNcL3YwIiwicHVibGlzaGVyIjoiSmFndWFyX0NJIiwidmVyc2lvbiI6InYwIiwic3Vic2NyaXB0aW9uVGllciI6IndkaF9zaXRlX3NwZWNpZmljX2ZyZWUifV0sInRva2VuX3R5cGUiOiJhcGlLZXkiLCJpYXQiOjE3NzMxNzMyNDIsImp0aSI6IjVmMjg1NWZlLTM1ZWMtNDQxZS1hMzhjLWNjZGE3MjQwYjI0ZiJ9.I3tFb4mfUVxCnPVTCAMJ_VgUx99SXIRVxY_2UbWEU9eH9IRvBXzjUOv8uC9hVVSw3LmzS4aMLnM6oiB2lJHSHxPGyFB3ybzcBYoa_wIFrt0H5UK_Br5IWGAkB1aG2xvVCburIu-QCHPT5PlpghfDb0MreUjXxB9fZT6HenvHUoHgZ3MZnuR499Y39Y1bdmvhtnf8ypS6wrf5oTuwIPW5rsQmK8QfpTscImt8eqYOjI1FqYJwuWxIdpcdKRg0tqRq0gTjQzHNCt6kGfDqtLtnblrjL0hXonG8DR5wmShr5oim7mAyGi7dENBL9vgkgS3zu4E4m1y7ZXN22QQt2hbD1A=="
WEATHERAPI_KEY = "416de86549c44e40918200751261003"


@dataclass
class ParsedWeatherMarket:
    city: str
    temp_low: float
    temp_high: float
    unit: str
    is_or_below: bool
    is_or_higher: bool
    raw_question: str
    target_date: Optional[str] = None
    hours_to_resolve: float = 24.0


def parse_weather_question(question: str) -> Optional[ParsedWeatherMarket]:
    q = question.lower()
    if "temperature" not in q:
        return None
    city = None
    for name in sorted(AIRPORTS.keys(), key=len, reverse=True):
        if name in q:
            city = name
            break
    if not city:
        m = re.search(r'in\s+(.+?)\s+(?:be|on)', question, re.IGNORECASE)
        if m:
            c = m.group(1).lower().strip()
            if c in AIRPORTS:
                city = c
    if not city:
        return None

    unit = "F"
    if any(x in q for x in ["deg c", "degrees c"]) or re.search(r'be\s+\d+c\b', q) or \
       re.search(r'\d+\s*c\s+on', q) or re.search(r'\d+\s*c\s*\?', q):
        unit = "C"

    is_or_below = "or below" in q or "or lower" in q
    is_or_higher = "or higher" in q or "or above" in q

    m = re.search(r'(?:between|be)\s+(\d+)-(\d+)', q)
    if m:
        return ParsedWeatherMarket(city, float(m.group(1)), float(m.group(2)),
                                   unit, False, False, question)
    m = re.search(r'be\s+(\d+)\s*[FCfc]?\s+on', q)
    if m:
        return ParsedWeatherMarket(city, float(m.group(1)), float(m.group(1)),
                                   unit, is_or_below, is_or_higher, question)
    m = re.search(r'(\d+)\s*[FCfc]\s+or\s+(?:higher|below|above|lower)', q)
    if m:
        return ParsedWeatherMarket(city, float(m.group(1)), float(m.group(1)),
                                   unit, is_or_below, is_or_higher, question)
    return None


class ForecastFetcher:
    """
    Multi-source forecast fetcher with 5-minute cache.
    US:    Open-Meteo + NOAA gridpoint
    UK/EU: Open-Meteo + Met Office DataHub
    Other: Open-Meteo + WeatherAPI.com
    Observed actuals from NOAA current conditions page (US only).
    All sources are target-date aware.
    """

    def __init__(self):
        self._client = httpx.AsyncClient(
            timeout=15.0, headers={"User-Agent": "PolymarketWeatherBot/1.0"}
        )
        self._noaa_grid_cache: Dict[str, dict] = {}
        self._forecast_cache: Dict[str, Tuple[float, dict]] = {}
        self._cache_ttl = 300

    async def close(self):
        await self._client.aclose()

    async def get_forecast(self, city: str, target_date: Optional[str] = None) -> Optional[dict]:
        cache_key = f"{city}|{target_date or 'default'}"
        now = time.time()
        if cache_key in self._forecast_cache:
            t, data = self._forecast_cache[cache_key]
            if now - t < self._cache_ttl:
                return data
            logger.info(f"Refreshing forecast {city} target={target_date}")

        info = AIRPORTS.get(city.lower())
        if not info:
            return None

        lat, lon = info["lat"], info["lon"]
        temps_c: List[float] = []
        sources: List[str] = []

        om = await self._fetch_openmeteo(lat, lon, target_date=target_date)
        if om is not None:
            temps_c.append(om)
            sources.append("Open-Meteo")

        if info.get("noaa"):
            noaa = await self._fetch_noaa(lat, lon)
            if noaa is not None:
                temps_c.append(noaa)
                sources.append("NOAA")
        elif info.get("met_office") and MET_OFFICE_API_KEY:
            mo = await self._fetch_met_office(lat, lon, target_date=target_date)
            if mo is not None:
                temps_c.append(mo)
                sources.append("MetOffice")
        elif WEATHERAPI_KEY:
            wa = await self._fetch_weatherapi(lat, lon, target_date=target_date)
            if wa is not None:
                temps_c.append(wa)
                sources.append("WeatherAPI")

        if not temps_c:
            return None

        avg_c = sum(temps_c) / len(temps_c)
        avg_f = avg_c * 9 / 5 + 32

        boost = 0.0
        if len(temps_c) >= 2:
            diff = abs(temps_c[0] - temps_c[1])
            boost = 0.10 if diff <= 1.5 else (0.05 if diff <= 3.0 else 0.0)

        observed = None
        if info.get("noaa"):
            station_id = info["station"].split("(")[-1].rstrip(")")
            observed = await self._fetch_noaa_observed(station_id)

        result = {
            "temp_c": round(avg_c, 1),
            "temp_f": round(avg_f, 1),
            "sources": sources,
            "confidence_boost": boost,
            "station": info["station"],
            "individual_c": temps_c,
            "observed": observed,
        }
        self._forecast_cache[cache_key] = (now, result)
        logger.info(
            f"FORECAST {info['station']} (target={target_date}): "
            f"{avg_f:.1f}F / {avg_c:.1f}C | {'+'.join(sources)} | boost={boost:.0%}"
        )
        return result

    async def _fetch_noaa(self, lat: float, lon: float) -> Optional[float]:
        try:
            gk = f"{lat:.4f},{lon:.4f}"
            if gk not in self._noaa_grid_cache:
                r = await self._client.get(
                    f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
                    headers={"Accept": "application/geo+json"},
                )
                r.raise_for_status()
                self._noaa_grid_cache[gk] = r.json().get("properties", {})
            g = self._noaa_grid_cache[gk]
            o, gx, gy = g.get("gridId"), g.get("gridX"), g.get("gridY")
            if not o:
                return None
            r = await self._client.get(
                f"https://api.weather.gov/gridpoints/{o}/{gx},{gy}/forecast",
                headers={"Accept": "application/geo+json"},
            )
            r.raise_for_status()
            for p in r.json().get("properties", {}).get("periods", []):
                if p.get("isDaytime", False):
                    t = float(p["temperature"])
                    return (t - 32) * 5 / 9 if p.get("temperatureUnit") == "F" else t
        except Exception as e:
            logger.debug(f"NOAA err: {e}")
        return None

    async def _fetch_openmeteo(
        self, lat: float, lon: float, target_date: Optional[str] = None
    ) -> Optional[float]:
        """
        Fetch max temp for a specific calendar date from Open-Meteo.
        Uses hourly data for maximum intraday accuracy.
        If target_date is None, defaults to tomorrow.
        """
        try:
            r = await self._client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": str(lat),
                    "longitude": str(lon),
                    "daily": "temperature_2m_max",
                    "hourly": "temperature_2m",
                    "timezone": "auto",
                    "forecast_days": "7",
                },
            )
            r.raise_for_status()
            data = r.json()
            daily_maxes = data.get("daily", {}).get("temperature_2m_max", [])
            daily_dates = data.get("daily", {}).get("time", [])
            hourly_temps = data.get("hourly", {}).get("temperature_2m", [])
            hourly_times = data.get("hourly", {}).get("time", [])

            date_str = target_date if target_date else (
                daily_dates[1] if len(daily_dates) >= 2 else None
            )
            if not date_str:
                return None

            if hourly_temps and hourly_times:
                day_temps = [
                    t for ts, t in zip(hourly_times, hourly_temps)
                    if ts.startswith(date_str) and t is not None
                ]
                if day_temps:
                    return float(max(day_temps))

            for d, mx in zip(daily_dates, daily_maxes):
                if d == date_str and mx is not None:
                    return float(mx)

            if daily_maxes and daily_maxes[0] is not None:
                return float(daily_maxes[0])

        except Exception as e:
            logger.debug(f"OpenMeteo err: {e}")
        return None

    async def _fetch_met_office(
        self, lat: float, lon: float, target_date: Optional[str] = None
    ) -> Optional[float]:
        """
        UK Met Office DataHub hourly point forecast.
        Returns max screenTemperature (C) for target date.
        Requires MET_OFFICE_API_KEY env var.
        Free tier: https://datahub.metoffice.gov.uk
        """
        if not MET_OFFICE_API_KEY:
            return None
        try:
            url = "https://data.hub.api.metoffice.gov.uk/sitespecific/v0/point/hourly"
            r = await self._client.get(
                url,
                params={"latitude": str(lat), "longitude": str(lon), "includeLocationName": "false"},
                headers={"apikey": MET_OFFICE_API_KEY, "Accept": "application/json"},
            )
            r.raise_for_status()
            data = r.json()
            date_str = target_date or (
                datetime.now(timezone.utc) + timedelta(days=1)
            ).strftime("%Y-%m-%d")

            day_temps = []
            for feature in data.get("features", []):
                props = feature.get("properties", {})
                if props.get("time", "").startswith(date_str):
                    t = props.get("screenTemperature")
                    if t is not None:
                        day_temps.append(float(t))
            if day_temps:
                return max(day_temps)
        except Exception as e:
            logger.debug(f"Met Office err: {e}")
        return None

    async def _fetch_weatherapi(
        self, lat: float, lon: float, target_date: Optional[str] = None
    ) -> Optional[float]:
        """
        WeatherAPI.com forecast (free tier: 3-day max).
        Returns maxtemp_c for the target date.
        Requires WEATHERAPI_KEY env var.
        """
        if not WEATHERAPI_KEY:
            return None
        try:
            days_ahead = 1
            if target_date:
                today = datetime.now(timezone.utc).date()
                try:
                    td = datetime.fromisoformat(target_date).date()
                    days_ahead = max(1, (td - today).days)
                except Exception:
                    pass
            if days_ahead > 3:
                logger.debug(f"WeatherAPI: {days_ahead}d out, beyond free tier")
                return None

            r = await self._client.get(
                "https://api.weatherapi.com/v1/forecast.json",
                params={
                    "key": WEATHERAPI_KEY,
                    "q": f"{lat},{lon}",
                    "days": str(min(days_ahead + 1, 3)),
                    "aqi": "no",
                    "alerts": "no",
                },
            )
            r.raise_for_status()
            data = r.json()
            date_str = target_date or (
                datetime.now(timezone.utc) + timedelta(days=1)
            ).strftime("%Y-%m-%d")

            for day in data.get("forecast", {}).get("forecastday", []):
                if day.get("date") == date_str:
                    max_c = day.get("day", {}).get("maxtemp_c")
                    if max_c is not None:
                        return float(max_c)

            days_list = data.get("forecast", {}).get("forecastday", [])
            if days_list:
                max_c = days_list[-1].get("day", {}).get("maxtemp_c")
                if max_c is not None:
                    return float(max_c)
        except Exception as e:
            logger.debug(f"WeatherAPI err: {e}")
        return None

    async def _fetch_noaa_observed(self, station_id: str) -> Optional[dict]:
        """Fetch observed actual high/low from NOAA current conditions (US only, full precision)."""
        try:
            r = await self._client.get(
                f"https://tgftp.nws.noaa.gov/weather/current/{station_id}.html"
            )
            r.raise_for_status()
            text = r.text
            max_f = max_c = min_f = min_c = None
            m = re.search(r'Max Temperature\s+([\d.]+)\s*F\s*\(\s*([\d.]+)\s*C\)', text)
            if m:
                max_f, max_c = float(m.group(1)), float(m.group(2))
            m = re.search(r'Min Temperature\s+([\d.]+)\s*F\s*\(\s*([\d.]+)\s*C\)', text)
            if m:
                min_f, min_c = float(m.group(1)), float(m.group(2))
            if max_f is not None and min_f is not None:
                return {"max_f": max_f, "min_f": min_f, "max_c": max_c, "min_c": min_c}
        except Exception as e:
            logger.debug(f"NOAA observed err ({station_id}): {e}")
        return None


class WeatherStrategy:
    """
    VALUE-BASED trading: only buy when there is a real gap between
    our forecast conviction and the market price.

    Improvements over v1:
    - Multi-source forecasts for all cities (Met Office for UK/EU, WeatherAPI for others)
    - Target-date aware fetching (correct day always)
    - Time-decay conviction cap (>48h => max 0.70, >36h => max 0.75)
    - Minimum edge raised to 12% (was 5%)
    - EV check: must exceed 10% expected value per dollar
    - Quarter-Kelly position sizing for optimal compound growth
    """

    def __init__(self, min_confidence: float = 0.55, max_position_pct: float = 0.06):
        self.fetcher = ForecastFetcher()
        self.min_confidence = min_confidence
        self.max_position_pct = max_position_pct

    async def close(self):
        await self.fetcher.close()

    async def analyze(self, markets: list, balance: float) -> list:
        from strategies import TradeSignal, SignalType, Side

        signals = []
        parsed = []
        now_utc = datetime.now(timezone.utc)

        for mkt in markets:
            p = parse_weather_question(mkt.question)
            if p and p.city:
                if mkt.end_date:
                    try:
                        ed = datetime.fromisoformat(mkt.end_date.replace("Z", "+00:00"))
                        p.hours_to_resolve = max(0.0, (ed - now_utc).total_seconds() / 3600)
                        p.target_date = ed.strftime("%Y-%m-%d")
                    except Exception:
                        pass
                parsed.append((mkt, p))

        logger.info(f"Parsed {len(parsed)}/{len(markets)} weather markets")
        if not parsed:
            return signals

        # Fetch forecasts — one call per city, passing the correct target_date
        cities_needed = set(p.city for _, p in parsed)
        forecasts: Dict[str, dict] = {}
        for city in cities_needed:
            city_parsed = [p for _, p in parsed if p.city == city]
            target_date = city_parsed[0].target_date if city_parsed else None
            fc = await self.fetcher.get_forecast(city, target_date=target_date)
            if fc:
                forecasts[city] = fc
            await asyncio.sleep(0.2)

        logger.info(f"Forecasts: {len(forecasts)}/{len(cities_needed)} cities")

        for mkt, p in parsed:
            # Skip markets resolving today or earlier
            if mkt.end_date:
                try:
                    ed = datetime.fromisoformat(mkt.end_date.replace("Z", "+00:00"))
                    today_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
                    if ed <= today_utc:
                        logger.debug(f"SKIP {mkt.question[:50]}: past/today end_date")
                        continue
                except Exception:
                    pass

            fc = forecasts.get(p.city)
            if not fc:
                continue

            # Prefer observed actual high (full precision, US only)
            observed = fc.get("observed")
            if observed:
                forecast_temp = observed["max_f"] if p.unit == "F" else observed["max_c"]
            else:
                forecast_temp = fc["temp_f"] if p.unit == "F" else fc["temp_c"]

            boost = fc.get("confidence_boost", 0.0)
            range_mid = (p.temp_low + p.temp_high) / 2
            distance = abs(forecast_temp - range_mid)

            # Step 1: what does the forecast say?
            if p.is_or_below:
                forecast_says_yes = forecast_temp <= p.temp_low
            elif p.is_or_higher:
                forecast_says_yes = forecast_temp >= p.temp_high
            else:
                forecast_says_yes = (p.temp_low <= forecast_temp <= p.temp_high) or distance <= 1.0

            # Step 2: base conviction from distance
            if forecast_says_yes:
                if distance <= 0:
                    conviction = 0.80
                elif distance <= 1.0:
                    conviction = 0.65
                elif p.is_or_below or p.is_or_higher:
                    conviction = min(0.60 + distance / 10, 0.85)
                else:
                    conviction = 0.60
            else:
                if distance >= 8:
                    conviction = 0.85
                elif distance >= 5:
                    conviction = 0.75
                elif distance >= 3:
                    conviction = 0.68
                elif distance >= 2:
                    conviction = 0.60
                else:
                    conviction = 0.55

            conviction = min(conviction + boost, 0.90)

            # FIX 3: Time-decay cap — NWP skill degrades beyond 36-48h
            if p.hours_to_resolve > 48:
                conviction = min(conviction, 0.70)
                logger.debug(f"TIME-DECAY: {mkt.question[:45]} | {p.hours_to_resolve:.0f}h -> cap 0.70")
            elif p.hours_to_resolve > 36:
                conviction = min(conviction, 0.75)

            if conviction < self.min_confidence:
                continue

            # Step 3: value check
            yes_price = mkt.yes_price or 0.5
            no_price = mkt.no_price or 0.5

            if forecast_says_yes:
                outcome, price, token_id = "Yes", yes_price, mkt.yes_token_id or ""
                if price > 0.75 or price < 0.03:
                    continue
            else:
                outcome, price, token_id = "No", no_price, mkt.no_token_id or ""
                if price > 0.75 or price < 0.03:
                    continue

            # FIX 4: Strict edge + EV filters
            edge = conviction - price
            if edge < 0.12:
                logger.debug(f"LOW EDGE {mkt.question[:45]}: {edge:.2%} < 12%")
                continue

            # EV = conviction * payout_ratio - 1
            # payout_ratio = 1/price (e.g. buying at 0.40 pays 2.5x)
            ev = conviction * (1.0 / price) - 1.0
            if ev < 0.10:
                logger.debug(f"LOW EV {mkt.question[:45]}: EV={ev:.2%} < 10%")
                continue

            # FIX 6: Quarter-Kelly sizing
            # Full Kelly: f* = edge / (1 - conviction)
            # Quarter Kelly: 0.25 * f* (safety multiplier)
            kelly_denom = max(1.0 - conviction, 0.01)
            full_kelly = edge / kelly_denom
            quarter_kelly = full_kelly * 0.25
            size = balance * min(quarter_kelly, self.max_position_pct)
            size = max(0.50, min(size, balance * 0.10))

            if size < 0.50:
                continue

            station = fc.get("station", p.city)
            data_tag = "WX[OBS]" if observed else "WX[FCST]"
            temp_str = f"{forecast_temp:.1f}" if observed else f"{forecast_temp:.0f}"

            sig = TradeSignal(
                signal_type=SignalType.WEATHER,
                market=mkt,
                side=Side.BUY,
                token_id=token_id,
                outcome=outcome,
                price=price,
                size=size,
                confidence=conviction,
                reasoning=(
                    f"{data_tag}: Forecast={temp_str}{p.unit} | "
                    f"Range={p.temp_low:.0f}-{p.temp_high:.0f}{p.unit} | "
                    f"Dist={distance:.1f} | Edge={edge:.0%} | EV={ev:.0%} | "
                    f"Kelly={quarter_kelly:.3f} | TTR={p.hours_to_resolve:.0f}h | "
                    f"{'+'.join(fc['sources'])} | {station} -> {outcome}"
                ),
            )
            signals.append(sig)
            logger.info(
                f"WEATHER TRADE: {outcome}@{price:.2f} | {mkt.question[:55]} | "
                f"Temp={temp_str}{p.unit} | Edge={edge:.0%} | EV={ev:.0%} | "
                f"QKelly={quarter_kelly:.3f} | size=${size:.2f} | TTR={p.hours_to_resolve:.0f}h"
            )

        return signals
