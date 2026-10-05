#!/usr/bin/env python3
"""
Laedt die aktuellen MTG-Kartenpreise (USD, i.d.R. TCGplayer-basiert) von Scryfall,
vergleicht sie mit dem gestrigen Snapshot und schreibt die groessten Preis-Spikes
nach site/data.json (fuer die Webseite). Non-Foil und Foil werden getrennt ausgewertet.

Scryfall liefert die Bulk-Daten als gzip-komprimierte JSONL-Datei (eine Karte pro Zeile).

Ablauf:
1. data/latest.json (falls vorhanden) -> data/previous.json (Rotation)
2. Scryfall "default_cards" Bulk-Datei herunterladen & streamen (gzip + JSONL)
3. Neue Preise als data/latest.json speichern (getrennt nach nonfoil/foil)
4. previous vs. latest vergleichen -> Top-Spikes je Finish -> site/data.json
5. Zusaetzlich: taeglicher History-Snapshot (data/history/) fuer den 7-Tage-Trend
"""

import gzip
import io
import json
import os
import shutil
import sys
from datetime import datetime, timezone, timedelta

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
HISTORY_DIR = os.path.join(DATA_DIR, "history")
SITE_DIR = os.path.join(ROOT, "site")
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
PREVIOUS_PATH = os.path.join(DATA_DIR, "previous.json")
OUTPUT_PATH = os.path.join(SITE_DIR, "data.json")

MIN_PRICE_AFTER_SPIKE = 3.0
MIN_PCT_CHANGE = 20.0
MIN_PCT_CHANGE_7D = 35.0  # Schwelle fuer den 7-Tage-Trendvergleich
MIN_7D_BASELINE_AGE_DAYS = 5  # erst ab diesem Alter zaehlt ein Snapshot als "7-Tage"-Basis
HISTORY_RETENTION_DAYS = 8  # aeltere History-Snapshots werden geloescht
TOP_N = 50
RARITIES = {"rare", "mythic"}

BULK_INFO_URL = "https://api.scryfall.com/bulk-data"
HEADERS = {"User-Agent": "mtg-spike-tool/1.0", "Accept": "*/*"}


def get_bulk_download_url():
    resp = requests.get(BULK_INFO_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    body = resp.json()
    entries = body.get("data", [])
    for entry in entries:
        if entry.get("type") == "default_cards":
            uri = entry.get("jsonl_download_uri") or entry.get("download_uri")
            if uri:
                print(f"Gefundene Bulk-Datei-URL: {uri}")
                return uri
            raise RuntimeError(
                f"'default_cards'-Eintrag gefunden, aber keine Download-URL darin. "
                f"Kompletter Eintrag zur Diagnose: {entry}"
            )
    raise RuntimeError(
        f"Konnte 'default_cards' Bulk-Datei nicht finden. "
        f"Verfuegbare Typen: {[e.get('type') for e in entries]}."
    )


def download_and_extract_prices(url):
    """Streamt die gzip-komprimierte JSONL-Datei (eine Karte pro Zeile)."""
    nonfoil, foil = {}, {}
    line_count = 0
    with requests.get(url, headers=HEADERS, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        content_length = resp.headers.get("Content-Length", "?")
        print(f"Download-Antwort: Status={resp.status_code}, Content-Length={content_length}")

        with gzip.GzipFile(fileobj=resp.raw) as gz:
            text_stream = io.TextIOWrapper(gz, encoding="utf-8")
            for line in text_stream:
                line = line.strip()
                if not line:
                    continue
                line_count += 1
                try:
                    card = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if card.get("lang") != "en":
                    continue
                if "paper" not in (card.get("games") or []):
                    continue
                if card.get("rarity") not in RARITIES:
                    continue

                prices = card.get("prices") or {}
                card_id = card["id"]
                purchase_uris = card.get("purchase_uris") or {}
                base_info = {
                    "id": card_id,
                    "name": card.get("name", "?"),
                    "set": (card.get("set") or "").upper(),
                    "url": card.get("scryfall_uri", ""),
                    "cardmarket_url": purchase_uris.get("cardmarket", ""),
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

    print(f"{line_count} Zeilen (Karten) insgesamt verarbeitet.")

    if not nonfoil and not foil:
        raise RuntimeError(
            f"0 Preise gefunden bei {line_count} verarbeiteten Zeilen. "
            "Moegliche Ursache: Filter (RARITIES, Sprache, 'paper') zu streng, "
            "oder Datenstruktur hat sich erneut geaendert."
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


def compute_spikes_vs_baseline(baseline_prices, current, min_price, min_pct):
    """baseline_prices: {card_id: price}. current: {card_id: {name, set, url, cardmarket_url, price}}."""
    if not baseline_prices:
        return []
    spikes = []
    for card_id, cur in current.items():
        baseline_price = baseline_prices.get(card_id)
        if baseline_price is None:
            continue
        cur_price = cur["price"]
        if cur_price < min_price:
            continue
        if baseline_price <= 0:
            continue
        pct_change = ((cur_price - baseline_price) / baseline_price) * 100
        if pct_change < min_pct:
            continue
        spikes.append({
            "id": cur["id"],
            "name": cur["name"],
            "set": cur["set"],
            "url": cur["url"],
            "cardmarket_url": cur.get("cardmarket_url", ""),
            "price_yesterday": round(baseline_price, 2),
            "price_today": round(cur_price, 2),
            "change_pct": round(pct_change, 1),
            "change_abs": round(cur_price - baseline_price, 2),
        })
    spikes.sort(key=lambda x: x["change_pct"], reverse=True)
    return spikes[:TOP_N]


def compact_prices(full_dict):
    """Nur {id: price}, fuer platzsparende History-Snapshots."""
    return {card_id: info["price"] for card_id, info in full_dict.items()}


def save_history_snapshot(current, today_date):
    os.makedirs(HISTORY_DIR, exist_ok=True)
    path = os.path.join(HISTORY_DIR, f"{today_date.isoformat()}.json")
    save_json(path, {
        "nonfoil": compact_prices(current["nonfoil"]),
        "foil": compact_prices(current["foil"]),
    })


def load_7d_baseline(today_date):
    """Sucht den aeltesten verfuegbaren History-Snapshot, der alt genug fuer den 7-Tage-Vergleich ist."""
    if not os.path.isdir(HISTORY_DIR):
        return None, None
    candidates = []
    for filename in os.listdir(HISTORY_DIR):
        if not filename.endswith(".json"):
            continue
        date_str = filename[:-5]
        try:
            file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        age_days = (today_date - file_date).days
        if age_days >= MIN_7D_BASELINE_AGE_DAYS:
            candidates.append((file_date, filename))
    if not candidates:
        return None, None
    candidates.sort()  # aeltestes zuerst
    oldest_date, oldest_filename = candidates[0]
    data = load_json(os.path.join(HISTORY_DIR, oldest_filename))
    return data, oldest_date


def prune_old_history(today_date):
    if not os.path.isdir(HISTORY_DIR):
        return
    for filename in os.listdir(HISTORY_DIR):
        if not filename.endswith(".json"):
            continue
        date_str = filename[:-5]
        try:
            file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if (today_date - file_date).days > HISTORY_RETENTION_DAYS:
            os.remove(os.path.join(HISTORY_DIR, filename))


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(SITE_DIR, exist_ok=True)

    if os.path.exists(LATEST_PATH):
        shutil.copyfile(LATEST_PATH, PREVIOUS_PATH)

    print("Lade Scryfall Bulk-Data-Info...")
    download_url = get_bulk_download_url()
    print(f"Lade & verarbeite Preisdaten von {download_url} ...")
    current = download_and_extract_prices(download_url)
    print(f"{len(current['nonfoil'])} Non-Foil- / {len(current['foil'])} Foil-Preise gefunden.")

    save_json(LATEST_PATH, current)

    previous = load_json(PREVIOUS_PATH)
    prev_nonfoil = (previous or {}).get("nonfoil")
    prev_foil = (previous or {}).get("foil")

    spikes_nonfoil = compute_spikes_vs_baseline(prev_nonfoil, current["nonfoil"], MIN_PRICE_AFTER_SPIKE, MIN_PCT_CHANGE)
    spikes_foil = compute_spikes_vs_baseline(prev_foil, current["foil"], MIN_PRICE_AFTER_SPIKE, MIN_PCT_CHANGE)

    # 7-Tage-Trend
    today_date = datetime.now(timezone.utc).date()
    save_history_snapshot(current, today_date)
    baseline_7d, baseline_7d_date = load_7d_baseline(today_date)
    prune_old_history(today_date)

    if baseline_7d:
        spikes_nonfoil_7d = compute_spikes_vs_baseline(
            baseline_7d.get("nonfoil"), current["nonfoil"], MIN_PRICE_AFTER_SPIKE, MIN_PCT_CHANGE_7D
        )
        spikes_foil_7d = compute_spikes_vs_baseline(
            baseline_7d.get("foil"), current["foil"], MIN_PRICE_AFTER_SPIKE, MIN_PCT_CHANGE_7D
        )
    else:
        spikes_nonfoil_7d, spikes_foil_7d = [], []

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "compared_to_previous_run": previous is not None,
        "card_count_nonfoil": len(current["nonfoil"]),
        "card_count_foil": len(current["foil"]),
        "spikes_nonfoil": spikes_nonfoil,
        "spikes_foil": spikes_foil,
        "has_7d_comparison": baseline_7d is not None,
        "baseline_7d_date": baseline_7d_date.isoformat() if baseline_7d_date else None,
        "spikes_nonfoil_7d": spikes_nonfoil_7d,
        "spikes_foil_7d": spikes_foil_7d,
    }
    save_json(OUTPUT_PATH, result)
    print(f"{len(spikes_nonfoil)} Non-Foil- / {len(spikes_foil)} Foil-Spikes (24h) gefunden.")
    print(f"{len(spikes_nonfoil_7d)} Non-Foil- / {len(spikes_foil_7d)} Foil-Spikes (7 Tage) gefunden, geschrieben nach {OUTPUT_PATH}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        sys.exit(1)
