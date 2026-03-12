"""
setup_env.py - Run this once to create your .env file
Just paste each key when prompted, press Enter.
"""

print("\n=== Kalshi Weather Bot - First Time Setup ===\n")
print("Paste each key and press Enter. Leave blank to skip.\n")

kalshi_key_id = input("Kalshi API Key ID: ").strip()
kalshi_private_key = input("Kalshi Private Key (paste the whole thing, one line): ").strip()
weatherapi_key = input("WeatherAPI Key (from weatherapi.com): ").strip()
noaa_token = input("NOAA Token (from ncdc.noaa.gov/cdo-web/token): ").strip()

dry_run = input("\nEnable DRY RUN mode? (yes/no, default yes): ").strip().lower()
dry_run_val = "false" if dry_run == "no" else "true"

lines = []
if kalshi_key_id:
    lines.append(f"KALSHI_API_KEY_ID={kalshi_key_id}")
if kalshi_private_key:
    lines.append(f"KALSHI_PRIVATE_KEY={kalshi_private_key}")
if weatherapi_key:
    lines.append(f"WEATHERAPI_KEY={weatherapi_key}")
if noaa_token:
    lines.append(f"NOAA_TOKEN={noaa_token}")
lines.append(f"DRY_RUN={dry_run_val}")

with open(".env", "w") as f:
    f.write("\n".join(lines) + "\n")

print("\n.env file created successfully!")
print("Now run: python bot.py\n")
