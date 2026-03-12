"""
setup_env.py - Run this once to create your .env file
Just paste each key when prompted, press Enter.
"""

import textwrap

print("\n=== Kalshi Weather Bot - First Time Setup ===\n")
print("Paste each key and press Enter. Leave blank to skip.\n")

kalshi_key_id = input("Kalshi API Key ID: ").strip()

print("\nKalshi Private Key:")
print("(Paste just the raw key string - no headers needed, we add them automatically)")
raw_key = input("> ").strip()

# Strip any existing headers/whitespace just in case
raw_key = raw_key.replace("-----BEGIN RSA PRIVATE KEY-----", "")
raw_key = raw_key.replace("-----END RSA PRIVATE KEY-----", "")
raw_key = raw_key.replace("-----BEGIN PRIVATE KEY-----", "")
raw_key = raw_key.replace("-----END PRIVATE KEY-----", "")
raw_key = raw_key.replace(" ", "").replace("\n", "").replace("\r", "").strip()

# Wrap to 64-char lines (standard PEM format)
wrapped = "\n".join(textwrap.wrap(raw_key, 64))
kalshi_private_key = f"-----BEGIN RSA PRIVATE KEY-----\n{wrapped}\n-----END RSA PRIVATE KEY-----"

weatherapi_key = input("\nWeatherAPI Key (from weatherapi.com): ").strip()
noaa_token = input("NOAA Token (from ncdc.noaa.gov/cdo-web/token): ").strip()

dry = input("\nEnable DRY RUN mode? (yes/no, default yes): ").strip().lower()
dry_run = "false" if dry == "no" else "true"

# Write .env - private key stored as single line with \n escapes
key_oneline = kalshi_private_key.replace("\n", "\\n")

env_content = f"""KALSHI_API_KEY_ID={kalshi_key_id}
KALSHI_PRIVATE_KEY={key_oneline}
WEATHERAPI_KEY={weatherapi_key}
NOAA_TOKEN={noaa_token}
DRY_RUN={dry_run}
"""

with open(".env", "w") as f:
    f.write(env_content)

print("\n.env file created successfully!")
print("Now run: python bot.py")
