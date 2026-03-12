"""
setup_env.py - Run this once to create your .env file
Just paste each key when prompted, press Enter.
"""

import textwrap
import re

print("\n=== Kalshi Weather Bot - First Time Setup ===\n")
print("Paste each key and press Enter. Leave blank to skip.\n")

kalshi_key_id = input("Kalshi API Key ID: ").strip()
kalshi_private_key = input("Kalshi Private Key (paste the raw string, no headers needed): ").strip()
weatherapi_key = input("WeatherAPI Key (from weatherapi.com): ").strip()
noaa_token = input("NOAA Token (from ncdc.noaa.gov/cdo-web/token): ").strip()
dry_run = input("Enable DRY RUN mode? (yes/no, default yes): ").strip().lower()
dry_run = "false" if dry_run == "no" else "true"

# Strip any existing PEM headers/footers and whitespace from the raw key
raw = kalshi_private_key
raw = re.sub(r'-+BEGIN[^-]+-+', '', raw)
raw = re.sub(r'-+END[^-]+-+', '', raw)
raw = re.sub(r'\s+', '', raw)

# Re-wrap into proper 64-char lines
wrapped = '\n'.join(textwrap.wrap(raw, 64))

# Build clean PEM with exactly 5 dashes
pem = f"-----BEGIN RSA PRIVATE KEY-----\n{wrapped}\n-----END RSA PRIVATE KEY-----"

# Collapse to single line with literal \n for .env storage
pem_oneline = pem.replace('\n', '\\n')

env_content = f"""KALSHI_API_KEY_ID={kalshi_key_id}
KALSHI_PRIVATE_KEY={pem_oneline}
WEATHERAPI_KEY={weatherapi_key}
NOAA_TOKEN={noaa_token}
DRY_RUN={dry_run}
"""

with open('.env', 'w') as f:
    f.write(env_content)

print("\n.env file created successfully!")
print("Now run: python bot.py")
