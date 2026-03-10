#!/usr/bin/env bash
#
# Polymarket Autonomous Trading Bot - Quick Start
# Usage:
#   ./run.sh              # Dry run with $50 balance
#   ./run.sh --balance 30 # Dry run with $30
#   ./run.sh --live       # LIVE MODE (requires .env config)
#

set -e

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Polymarket Autonomous Trading Bot                ${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"

# Check Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Python 3 is required. Install it first.${NC}"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo -e "Python: ${PYTHON_VERSION}"

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}Creating virtual environment...${NC}"
    python3 -m venv venv
fi

# Activate venv
source venv/bin/activate

# Install dependencies
echo -e "${YELLOW}Installing dependencies...${NC}"
pip install -q httpx python-dotenv 2>/dev/null

# Check if live mode requested and py-clob-client needed
if [[ "$*" == *"--live"* ]]; then
    echo -e "${RED}⚠️  LIVE MODE - Installing Polymarket client...${NC}"
    pip install -q py-clob-client 2>/dev/null

    # Check for .env file
    if [ ! -f ".env" ]; then
        echo -e "${RED}ERROR: .env file required for live mode!${NC}"
        echo -e "Copy .env.example to .env and configure your wallet."
        exit 1
    fi
fi

# Load .env if it exists
if [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
fi

# Create required directories
mkdir -p logs state

# Start the dashboard in background (optional)
echo -e "${GREEN}Starting dashboard on http://localhost:8080${NC}"

# Run the bot
echo -e "${GREEN}Starting bot...${NC}"
echo ""
python3 bot.py "$@"
