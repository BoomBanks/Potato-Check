#!/usr/bin/env python3
"""
Laedt die aktuellen MTG-Kartenpreise (USD, i.d.R. TCGplayer-basiert) von Scryfall,
vergleicht sie mit dem gestrigen Snapshot und schreibt die groessten Preis-Spikes
nach site/data.json (fuer die Webseite). Non-Foil und Foil werden getrennt ausgewertet.

Ablauf:
1. data/latest.json (falls vorhanden) -> data/previous.json (Rotation)
2. Scryfall "default_cards" Bulk-Datei herunterladen & streamen
3. Neue Preise als data/latest.json speichern (getrennt nach nonfoil/foil)
4. previous vs. latest vergleichen -> Top-Spikes je Finish -> site/data.json
"""

import json
import os
import shutil
import sys
from datetime import datetime, timezone

import ijson
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
SITE_DIR = os.path.join(ROOT, "site")
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
PREVIOUS_PATH = os.path.join(DATA_DIR, "previous.json")
OUTPUT_PATH = os.path.join(SITE_DIR, "data.json")

MIN_PRICE_AFTER_SPIKE = 3.0  # Karte muss NACH dem Spike mindestens diesen USD-Preis haben
MIN_PCT_CHANGE = 20.0        # Mindest-Preisanstieg in Prozent, um als "Spike" zu zaehlen
TOP_N = 50
RARITIES = {"rare", "mythic"}  # Commons/Uncommons werden ignoriert (meist nur Preis-Rauschen).
                                 # Zum Einschliessen einfach "uncommon"/"common" ergaenzen.

BULK_INFO_URL = "https://api.scryfall.com/bulk-data"
HEADERS = {"User-Agent": "mtg-spike-tool/1.0", "Accept": "*/*"}


def get_bulk_download_url():
    resp = requests.get(BULK_INFO_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    entries = body.get("data", [])
    for entry in entries:
        if entry.get("type") == "default_cards":
            u
