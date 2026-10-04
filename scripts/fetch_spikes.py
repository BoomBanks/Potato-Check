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
            uri = entry.get("download_uri") or entry.get("download_url") or entry.get("uri")
            if uri:
                return uri
            raise RuntimeError(
                f"'default_cards'-Eintrag gefunden, aber keine Download-URL darin. "
                f"Kompletter Eintrag zur Diagnose: {entry}"
            )
    raise RuntimeError(
        f"Konnte 'default_cards' Bulk-Datei nicht finden. "
        f"Verfuegbare Typen: {[e.get('type') for e in entries]}. "
        f"Komplette Antwort zur Diagnose: {body}"
    )


def download_and_extract_prices(url):
    """Streamt die grosse Scryfall-JSON-Datei und extrahiert nonfoil- und foil-Preise getrennt."""
    nonfoil, foil = {}, {}
    with requests.get(url, headers=HEADERS, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "?")
        content_length = resp.headers.get("Content-Length", "?")
        print(f"Download-Antwort: Status={resp.status_code}, Content-Type={content_type}, Content-Length={content_length}")
        if "json" not in content_type.lower():
            preview = resp.raw.read(500)
            raise RuntimeError(
                f"Unerwarteter Content-Type '{content_type}' statt JSON. "
                f"Erste 500 Bytes der Antwort: {preview!r}"
            )
        resp.raw.decode_content = True
        for card in ijson.items(resp.raw, "item"):
            if card.get("lang") != "en":
                continue
            if "paper" not in (card.get("games") or []):
                continue
            if card.get("rarity") not in RARITIES:
                continue

            prices = card.get("prices") or {}
            card_id = card["id"]
            base_info = {
                "name": card.get("name", "?"),
                "set": (card.get("set") or "").upper(),
                "url": card.get("scryfall_uri", ""),
            }

            usd = prices.get("usd")
            if usd:
                try:
                    nonfoil[card_id] = {**base_info, "price": float(usd)}
                except (TypeError, ValueError):
                    pass

            usd_foil = prices.get("usd_foil")
            if usd_foil:
                try:
                    foil[card_id] = {**base_info, "price": float(usd_foil)}
                except (TypeError, ValueError):
                    pass

    if not nonfoil and not foil:
        raise RuntimeError(
            "0 Preise gefunden, obwohl der Download technisch erfolgreich war (Status 200, Content-Type JSON). "
            "Moegliche Ursache: Datenformat der Scryfall-Datei weicht vom erwarteten Format ab, oder alle "
            "Karten wurden durch die Filter (RARITIES, Sprache, 'paper') ausgeschlossen."
        )

    return {"nonfoil": nonfoil, "foil": foil}


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def compute_spikes(previous, current):
    if not previous:
        return []
    spikes = []
    for card_id, cur in current.items():
        prev = previous.get(card_id)
        if not prev:
            continue
        prev_price = prev["price"]
        cur_price = cur["price"]
        if cur_price < MIN_PRICE_AFTER_SPIKE:
            continue
        if prev_price <= 0:
            continue
        pct_change = ((cur_price - prev_price) / prev_price) * 100
        if pct_change < MIN_PCT_CHANGE:
            continue
        spikes.append({
            "name": cur["name"],
            "set": cur["set"],
            "url": cur["url"],
            "price_yesterday": round(prev_price, 2),
            "price_today": round(cur_price, 2),
            "change_pct": round(pct_change, 1),
            "change_abs": round(cur_price - prev_price, 2),
        })
    spikes.sort(key=lambda x: x["change_pct"], reverse=True)
    return spikes[:TOP_N]


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(SITE_DIR, exist_ok=True)

    # 1. Rotation: gestriger "latest" wird zu "previous"
    if os.path.exists(LATEST_PATH):
        shutil.copyfile(LATEST_PATH, PREVIOUS_PATH)

    # 2. Neue Daten holen
    print("Lade Scryfall Bulk-Data-Info...")
    download_url = get_bulk_download_url()
    print(f"Lade & verarbeite Preisdaten von {download_url} ...")
    current = download_and_extract_prices(download_url)
    print(f"{len(current['nonfoil'])} Non-Foil- / {len(current['foil'])} Foil-Preise gefunden.")

    # 3. Neuen Snapshot speichern
    save_json(LATEST_PATH, current)

    # 4. Vergleich
    previous = load_json(PREVIOUS_PATH)
    prev_nonfoil = (previous or {}).get("nonfoil")
    prev_foil = (previous or {}).get("foil")

    spikes_nonfoil = compute_spikes(prev_nonfoil, current["nonfoil"])
    spikes_foil = compute_spikes(prev_foil, current["foil"])

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "compared_to_previous_run": previous is not None,
        "card_count_nonfoil": len(current["nonfoil"]),
        "card_count_foil": len(current["foil"]),
        "spikes_nonfoil": spikes_nonfoil,
        "spikes_foil": spikes_foil,
    }
    save_json(OUTPUT_PATH, result)
    print(f"{len(spikes_nonfoil)} Non-Foil- / {len(spikes_foil)} Foil-Spikes gefunden, geschrieben nach {OUTPUT_PATH}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"FEHLER: {exc}", file=sys.stderr)
        sys.exit(1)
