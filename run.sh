#!/usr/bin/env bash
#
# Kalshi Weather Trading Bot - Quick Start
# Usage:
#   ./run.sh              # Dry run (default)
#   ./run.sh --balance 50 # Dry run with custom balance
#   ./run.sh --live       # LIVE MODE (requires .env config)
#

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}=================================================${NC}"
echo -e "${GREEN}  Kalshi Weather Trading Bot                     ${NC}"
echo -e "${GREEN}=================================================${NC}"

if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Python 3 is required. Install it first.${NC}"
    exit 1
fi

if [ ! -d "venv" ]; then
    echo -e "${YELLOW}Creating virtual environment...${NC}"
    python3 -m venv venv
fi

source venv/bin/activate

echo -e "${YELLOW}Installing dependencies...${NC}"
pip install -q httpx python-dotenv cryptography

if [[ "$*" == *"--live"* ]]; then
    echo -e "${RED}WARNING: LIVE MODE - real money at risk!${NC}"
    if [ ! -f ".env" ]; then
        echo -e "${RED}ERROR: .env file required for live mode!${NC}"
        echo -e "Copy .env.example to .env and fill in your Kalshi credentials."
        exit 1
    fi
    if [ ! -f "$(grep KALSHI_PRIVATE_KEY_PATH .env | cut -d= -f2)" ]; then
        echo -e "${RED}ERROR: Private key file not found. Check KALSHI_PRIVATE_KEY_PATH in .env${NC}"
        exit 1
    fi
fi

if [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
fi

mkdir -p logs state

echo -e "${GREEN}Starting bot...${NC}"
python3 bot.py "$@"
