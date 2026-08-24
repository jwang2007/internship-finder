#!/bin/bash
# Double-click this file. It sets up Python once, then opens the finder in your browser.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "First launch: setting up (about a minute)…"
  python3 -m venv .venv || { echo "Python 3 is needed: https://www.python.org/downloads/"; read -p "Press Return to close"; exit 1; }
fi
source .venv/bin/activate
pip install -q -r requirements.txt
echo "Leave this window open (minimise it). Close it to stop the finder."
python app.py
