"""
Weather Trading Strategy - VALUE-BASED

Core rule: Only buy when forecast DISAGREES with market price.
- If forecast says YES and market prices YES at 30% -> BUY YES (value!)
- If forecast says YES and market prices YES at 90% -> SKIP (no value, already priced in)
- If forecast says NO and market prices NO at 20% -> BUY NO (value!)
- If forecast says NO and market prices NO at 85% -> SKIP (no value)

Never buy YES above 0.75 or NO above 0.75. That's throwing money at
something the market already knows.

NOAA (US) + Open-Meteo (worldwide). 5-min cache refresh. Hourly data.
Airport coordinates verified against Wunderground station pages.
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("polymarket_bot.weather")

# Verified airport coordinates from Wunderground station pages
# Format: lat, lon, noaa (US only), station name
AIRPORTS = {
    # US - verified from wunderground.com/history/daily/us/...
    "new york":      {"lat": 40.7772, "lon": -73.8726, "noaa": True, "station": "LaGuardia Airport (KLGA)"},
    "new york city": {"lat": 40.7772, "lon": -73.8726, "noaa": True, "station": "LaGuardia Airport (KLGA)"},
    "nyc":           {"lat": 40.7772, "lon": -73.8726, "noaa": True, "station": "LaGuardia Airport (KLGA)"},
    "seattle":       {"lat": 47.4489, "lon": -122.3094, "noaa": True, "station": "Seattle-Tacoma Intl (KSEA)"},
    "miami":         {"lat": 25.7932, "lon": -80.2906, "noaa": True, "station": "Miami Intl Airport (KMIA)"},
    "atlanta":       {"lat": 33.6367, "lon": -84.4281, "noaa": True, "station": "Hartsfield-Jackson (KATL)"},
    "chicago":       {"lat": 41.9742, "lon": -87.9073, "noaa": True, "station": "O'Hare Intl Airport (KORD)"},
    "dallas":        {"lat": 32.8481, "lon": -96.8512, "noaa": True, "station": "Dallas Love Field (KDAL)"},
    # International - verified from wunderground.com/history/daily/...
    "london":        {"lat": 51.4706, "lon": -0.4619, "noaa": False, "station": "Heathrow Airport (EGLL)"},
    "paris":         {"lat": 49.0128, "lon": 2.5500, "noaa": False, "station": "Charles de Gaulle (LFPG)"},
    "seoul":         {"lat": 37.4691, "lon": 126.4505, "noaa": False, "station": "Incheon Intl (RKSI)"},
    "ankara":        {"lat": 40.1244, "lon": 32.9992, "noaa": False, "station": "Esenboga Airport (LTAC)"},
    "lucknow":       {"lat": 26.7606, "lon": 80.8893, "noaa": False, "station": "Chaudhary Charan Singh (VILK)"},
    "wellington":    {"lat": -41.3272, "lon": 174.8053, "noaa": False, "station": "Wellington Airport (NZWN)"},
    "munich":        {"lat": 48.3538, "lon": 11.7861, "noaa": False, "station": "Munich Airport (EDDM)"},
    "sao paulo":     {"lat": -23.4356, "lon": -46.4731, "noaa": False, "station": "Guarulhos Intl (SBGR)"},
    "buenos aires":  {"lat": -34.8150, "lon": -58.5350, "noaa": False, "station": "Ezeiza Intl (SAEZ)"},
    "toronto":       {"lat": 43.6772, "lon": -79.6306, "noaa": False, "station": "Pearson Intl (CYYZ)"},
}


@dataclass
class ParsedWeatherMarket:
    city: str
    temp_low: float
    temp_high: float
    unit: str
    is_or_below: bool
    is_or_higher: bool
    raw_question: str


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
    if any(x in q for x in ["\u00b0c", "\u00bac"]) or re.search(r'be\s+\d+\u00b0?c', q) or \
       re.search(r'\d+\s*c\s+on', q) or re.search(r'\d+\s*c\s*\?', q):
        unit = "C"

    is_or_below = "or below" in q or "or lower" in q
    is_or_higher = "or higher" in q or "or above" in q

    m = re.search(r'(?:between|be)\s+(\d+)-(\d+)', q)
    if m:
        return ParsedWeatherMarket(city, float(m.group(1)), float(m.group(2)),
                                    unit, False, False, question)
    m = re.search(r'be\s+(\d+)\s*\u00b0?[FCfc]?\s+on', q)
    if m:
        return ParsedWeatherMarket(city, float(m.group(1)), float(m.group(1)),
                                    unit, is_or_below, is_or_higher, question)
    m = re.search(r'be\s+(\d+)\s*[\u00b0\u00ba][FCfc]', q)
    if m:
        return ParsedWeatherMarket(city, float(m.group(1)), float(m.group(1)),
                                    unit, is_or_below, is_or_higher, question)
    m = re.search(r'(\d+)\s*\u00b0?[FCfc]\s+or\s+(higher|below|above|lower)', q)
    if m:
        return ParsedWeatherMarket(city, float(m.group(1)), float(m.group(1)),
                                    unit, is_or_below, is_or_higher, question)
    return None


class ForecastFetcher:
    """5-minute cache. Hourly temps from Open-Meteo for intraday accuracy."""

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=15.0,
            headers={"User-Agent": "PolymarketWeatherBot/1.0"})
        self._noaa_grid_cache: Dict[str, dict] = {}
        self._forecast_cache: Dict[str, Tuple[float, dict]] = {}
        self._cache_ttl = 300  # 5 MINUTES

    async def close(self):
        await self._client.aclose()

    async def get_forecast(self, city: str) -> Optional[dict]:
        now = time.time()
        if city in self._forecast_cache:
            t, data = self._forecast_cache[city]
            if now - t < self._cache_ttl:
                return data
            else:
                logger.info(f"Refreshing forecast for {city} (cache expired)")

        info = AIRPORTS.get(city.lower())
        if not info:
            return None

        lat, lon = info["lat"], info["lon"]
        temps_c = []
        sources = []

        om = await self._fetch_openmeteo(lat, lon)
        if om is not None:
            temps_c.append(om)
            sources.append("Open-Meteo")

        if info.get("noaa"):
            noaa = await self._fetch_noaa(lat, lon)
            if noaa is not None:
                temps_c.append(noaa)
                sources.append("NOAA")

        if not temps_c:
            return None

        avg_c = sum(temps_c) / len(temps_c)
        avg_f = avg_c * 9 / 5 + 32
        boost = 0.0
        if len(temps_c) >= 2:
            diff = abs(temps_c[0] - temps_c[1])
            boost = 0.10 if diff <= 1.5 else (0.05 if diff <= 3.0 else 0.0)

        # Fix 2+3: Try to get observed actual high/low with full precision
        observed = None
        if info.get("noaa"):
            station_id = info["station"].split("(")[-1].rstrip(")")  # extract e.g. "KSEA" from "Seattle-Tacoma Intl (KSEA)"
            observed = await self._fetch_noaa_observed(station_id)

        result = {
            "temp_c": round(avg_c, 1), "temp_f": round(avg_f, 1),
            "sources": sources, "confidence_boost": boost,
            "station": info["station"], "individual_c": temps_c,
            "observed": observed,
        }
        self._forecast_cache[city] = (now, result)
        logger.info(
            f"FORECAST {info['station']}: {avg_f:.0f}F / {avg_c:.0f}C | "
            f"{'+'.join(sources)} | boost={boost:.0%}"
        )
        return result

    async def _fetch_noaa(self, lat, lon) -> Optional[float]:
        try:
            gk = f"{lat:.4f},{lon:.4f}"
            if gk not in self._noaa_grid_cache:
                r = await self._client.get(
                    f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
                    headers={"Accept": "application/geo+json"})
                r.raise_for_status()
                self._noaa_grid_cache[gk] = r.json().get("properties", {})
            g = self._noaa_grid_cache[gk]
            o, gx, gy = g.get("gridId"), g.get("gridX"), g.get("gridY")
            if not o:
                return None
            r = await self._client.get(
                f"https://api.weather.gov/gridpoints/{o}/{gx},{gy}/forecast",
                headers={"Accept": "application/geo+json"})
            r.raise_for_status()
            for p in r.json().get("properties", {}).get("periods", []):
                if p.get("isDaytime", False):
                    t = float(p["temperature"])
                    return (t - 32) * 5 / 9 if p.get("temperatureUnit") == "F" else t
        except Exception as e:
            logger.debug(f"NOAA err: {e}")
        return None

    async def _fetch_openmeteo(self, lat, lon) -> Optional[float]:
        try:
            r = await self._client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": str(lat), "longitude": str(lon),
                    "daily": "temperature_2m_max",
                    "hourly": "temperature_2m",
                    "timezone": "auto", "forecast_days": "3",
                })
            r.raise_for_status()
            data = r.json()
            daily_maxes = data.get("daily", {}).get("temperature_2m_max", [])
            daily_dates = data.get("daily", {}).get("time", [])
            hourly_temps = data.get("hourly", {}).get("temperature_2m", [])
            hourly_times = data.get("hourly", {}).get("time", [])

            # Get tomorrow's max from hourly (most accurate, updates intraday)
            tomorrow_str = daily_dates[1] if len(daily_dates) >= 2 else None
            if tomorrow_str and hourly_temps and hourly_times:
                tmrw = [t for ts, t in zip(hourly_times, hourly_temps)
                        if ts.startswith(tomorrow_str) and t is not None]
                if tmrw:
                    return float(max(tmrw))

            # Fallback to daily
            if len(daily_maxes) >= 2 and daily_maxes[1] is not None:
                return float(daily_maxes[1])
            if daily_maxes and daily_maxes[0] is not None:
                return float(daily_maxes[0])
        except Exception as e:
            logger.debug(f"OpenMeteo err: {e}")
        return None


    async def _fetch_noaa_observed(self, station_id: str) -> Optional[dict]:
        """Fetch observed actual high/low temps from NOAA current conditions page."""
        try:
            url = f"https://tgftp.nws.noaa.gov/weather/current/{station_id}.html"
            r = await self._client.get(url)
            r.raise_for_status()
            text = r.text
            max_f = max_c = min_f = min_c = None
            m = re.search(r'Max Temperature\s+([\d.]+)\s*F\s*\(\s*([\d.]+)\s*C\)', text)
            if m:
                max_f = float(m.group(1))
                max_c = float(m.group(2))
            m = re.search(r'Min Temperature\s+([\d.]+)\s*F\s*\(\s*([\d.]+)\s*C\)', text)
            if m:
                min_f = float(m.group(1))
                min_c = float(m.group(2))
            if max_f is not None and min_f is not None:
                return {"max_f": max_f, "min_f": min_f, "max_c": max_c, "min_c": min_c}
        except Exception as e:
            logger.debug(f"NOAA observed err ({station_id}): {e}")
        return None


class WeatherStrategy:
    """
    VALUE-BASED trading:
    Only buy when there's a GAP between forecast conviction and market price.
    If market already prices the correct answer at 75%+, there's no edge. SKIP.
    """

    def __init__(self, min_confidence=0.55, max_position_pct=0.06):
        self.fetcher = ForecastFetcher()
        self.min_confidence = min_confidence
        self.max_position_pct = max_position_pct

    async def close(self):
        await self.fetcher.close()

    async def analyze(self, markets: list, balance: float) -> list:
        from strategies import TradeSignal, SignalType, Side

        signals = []
        parsed = []
        for mkt in markets:
            p = parse_weather_question(mkt.question)
            if p and p.city:
                parsed.append((mkt, p))

        logger.info(f"Parsed {len(parsed)}/{len(markets)} weather markets")
        if not parsed:
            return signals

        cities_needed = set(p.city for _, p in parsed)
        forecasts = {}
        for city in cities_needed:
            fc = await self.fetcher.get_forecast(city)
            if fc:
                forecasts[city] = fc
            await asyncio.sleep(0.2)

        logger.info(f"Forecasts: {len(forecasts)}/{len(cities_needed)} cities")

        for mkt, p in parsed:
            # Fix 1: Skip markets resolving today or earlier (already in progress / resolved)
            if mkt.end_date:
                try:
                    ed = datetime.fromisoformat(mkt.end_date.replace("Z", "+00:00"))
                    today_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                    if ed <= today_utc:
                        logger.debug(f"SKIP {mkt.question[:50]}: end_date {mkt.end_date} is today or past")
                        continue
                except Exception:
                    pass
            fc = forecasts.get(p.city)
            if not fc:
                continue

            # Fix 2+3: Use observed actual high if available (more accurate, full precision)
            observed = fc.get("observed")
            if observed:
                forecast_temp = observed["max_f"] if p.unit == "F" else observed["max_c"]
            else:
                forecast_temp = fc["temp_f"] if p.unit == "F" else fc["temp_c"]
            boost = fc.get("confidence_boost", 0.0)
            range_mid = (p.temp_low + p.temp_high) / 2
            distance = abs(forecast_temp - range_mid)

            # Step 1: Determine what the forecast says (YES or NO)
            if p.is_or_below:
                forecast_says_yes = forecast_temp <= p.temp_low
            elif p.is_or_higher:
                forecast_says_yes = forecast_temp >= p.temp_high
            else:
                # Range market: is forecast temp in the range?
                forecast_says_yes = p.temp_low <= forecast_temp <= p.temp_high or distance <= 1.0

            # Step 2: Calculate our conviction
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

            if conviction < self.min_confidence:
                continue

            # Step 3: VALUE CHECK - only trade if market DISAGREES with us
            yes_price = mkt.yes_price or 0.5
            no_price = mkt.no_price or 0.5

            if forecast_says_yes:
                outcome = "Yes"
                price = yes_price
                token_id = mkt.yes_token_id or ""
                # VALUE: we think YES, so YES price should be HIGH
                # Only buy if market underprices it (YES < 0.75)
                # If YES is already 0.85+, the market already knows. No edge.
                if price > 0.75:
                    logger.debug(
                        f"SKIP {mkt.question[:50]}: YES@{price:.2f} already priced in"
                    )
                    continue
                # Don't buy YES below 0.03 either (probably wrong)
                if price < 0.03:
                    continue
            else:
                outcome = "No"
                price = no_price
                token_id = mkt.no_token_id or ""
                # VALUE: we think NO, so NO price should be HIGH
                # Only buy if market underprices it (NO < 0.75)
                if price > 0.75:
                    logger.debug(
                        f"SKIP {mkt.question[:50]}: NO@{price:.2f} already priced in"
                    )
                    continue
                if price < 0.03:
                    continue

            # Step 4: Size based on edge (gap between our conviction and market price)
            edge = conviction - price  # e.g. we're 80% sure, market says 40% = 0.40 edge
            if edge < 0.05:
                continue  # Not enough edge

            size = min(balance * self.max_position_pct * conviction, balance * 0.05)
            if size < 0.50:
                continue

            station = fc.get("station", p.city)
            sig = TradeSignal(
                signal_type=SignalType.WEATHER, market=mkt, side=Side.BUY,
                token_id=token_id, outcome=outcome, price=price,
                size=size, confidence=conviction,
                reasoning=(
                    (f"WX[OBS]: Observed max={forecast_temp:.1f}{p.unit} | "
                     if observed else
                     f"WX[FCST]: Forecast={forecast_temp:.0f}{p.unit} | ")
                    + f"Range={p.temp_low:.0f}-{p.temp_high:.0f}{p.unit} | "
                    + f"Dist={distance:.0f} | Edge={edge:.0%} | "
                    + f"{'+'.join(fc['sources'])} | {station} -> {outcome}"
                ),
            )
            signals.append(sig)
            logger.info(
                f"WEATHER TRADE: {outcome}@{price:.2f} | {mkt.question} | "
                f"Forecast={forecast_temp:.0f}{p.unit} | Edge={edge:.0%} | "
                f"conf={conviction:.2f} | ${size:.2f}"
            )

        return signals
