#!/usr/bin/env bash
# Linux / macOS / Termux / Raspberry Pi — installs what is needed, then keeps bot.py running (restarts if it ever stops)
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt || { echo "pip install failed"; exit 1; }
while true; do
  python3 bot.py
  echo "bot.py stopped — restarting in 5 seconds (Ctrl+C to quit)"
  sleep 5
done
