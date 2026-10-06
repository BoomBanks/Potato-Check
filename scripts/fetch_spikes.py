#!/usr/bin/env python3
"""Holt Scryfall-Bulkdaten, berechnet Preis-Spikes und schreibt site/data.json."""

import gzip
import io
import json
import os
import shutil
import sys
from datetime import datetime, timezone

import requests

MIN_PRICE_AFTER_SPIKE = 3.0
MIN_PCT_CHANGE = 20.0
MIN_PCT_CHANGE_7D = 35.0
MIN_7D_BASELINE_AGE_DAYS = 5
HISTORY_RETENTION_DAYS = 8
TOP_N = 50
RARITIES = {"rare", "mythic"}

BULK_INFO_URL = "https://api.scryfall.com/bulk-data"
LATEST_PATH = "data/latest.json"
PREVIOUS_PATH = "data/previous.json"
HISTORY_DIR = "data/history"
OUTPUT_PATH = "site/data.json"

HEADERS = {
    "User-Agent": "MTGSpikeTool/1.0 (private use)",
    "Accept": "application/json;q=0.9,*/*;q=0.8",
}


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def get_bulk_download_url():
    resp = requests.get(BULK_INFO_URL, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    for entry in resp.json().get("data", []):
        if entry.get("type") == "default_cards":
            url = entry.get("jsonl_download_uri") or entry.get("download_uri")
            if url:
                return url
    raise RuntimeError("default_cards Bulk-Eintrag nicht gefunden")


def _to_float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def download_and_extract_prices(url):
    nonfoil, foil = {}, {}
    with requests.get(url, headers=HEADERS, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        resp.raw.decode_content = False
        gz = gzip.GzipFile(fileobj=resp.raw)
        text = io.TextIOWrapper(gz, encoding="utf-8")
        for line in text:
            line = line.strip()
            if not line:
                continue
            try:
                card = json.loads(line)
            except ValueError:
                continue
            if card.get("lang") != "en":
                continue
            if "paper" not in (card.get("games") or []):
                continue
            if card.get("rarity") not in RARITIES:
                continue
            prices = card.get("prices") or {}
            base = {
                "id": card.get("id"),
                "name": card.get("name"),
                "set": (card.get("set") or "").upper(),
                "url": card.get("scryfall_uri"),
                "cardmarket_url": (card.get("purchase_uris") or {}).get("cardmarket"),
            }
            usd = _to_float(prices.get("usd"))
            if usd is not None:
                entry = dict(base)
                entry["price"] = usd
                entry["eur_price"] = _to_float(prices.get("eur"))
                nonfoil[card["id"]] = entry
            usd_f = _to_float(prices.get("usd_foil"))
            if usd_f is not None:
                entry = dict(base)
                entry["price"] = usd_f
                entry["eur_price"] = _to_float(prices.get("eur_foil"))
                foil[card["id"]] = entry
    if not nonfoil and not foil:
        raise RuntimeError("0 Preise gefunden - Bulk-Format unerwartet")
    return nonfoil, foil


def _extract_price(entry):
    if entry is None:
        return None
    if isinstance(entry, dict):
        entry = entry.get("price")
    return _to_float(entry)


def _extract_eur_price(entry):
    if isinstance(entry, dict):
        return _to_float(entry.get("eur_price"))
    return None


def compute_spikes_vs_baseline(baseline_prices, current, min_price, min_pct):
    spikes = []
    baseline_prices = baseline_prices or {}
    for cid, card in current.items():
        old = _extract_price(baseline_prices.get(cid))
        new = card.get("price")
        if not old or old <= 0 or new is None:
            continue
        if new < min_price:
            continue
        pct = (new - old) / old * 100.0
        if pct < min_pct:
            continue
        eur_old = _extract_eur_price(baseline_prices.get(cid))
        eur_new = card.get("eur_price")
        eur_pct = None
        if eur_old and eur_new is not None and eur_old > 0:
            eur_pct = round((eur_new - eur_old) / eur_old * 100.0, 1)
        spikes.append({
            "id": cid,
            "name": card.get("name"),
            "set": card.get("set"),
            "url": card.get("url"),
            "cardmarket_url": card.get("cardmarket_url"),
            "price_yesterday": round(old, 2),
            "price_today": round(new, 2),
            "change_pct": round(pct, 1),
            "change_abs": round(new - old, 2),
            "eur_price_yesterday": eur_old,
            "eur_price_today": eur_new,
            "eur_change_pct": eur_pct,
        })
    spikes.sort(key=lambda s: s["change_pct"], reverse=True)
    return spikes[:TOP_N]


def compact_prices(prices):
    return {
        cid: {"price": c["price"], "eur_price": c.get("eur_price")}
        for cid, c in prices.items()
    }


def save_history_snapshot(today_date, nonfoil, foil):
    path = os.path.join(HISTORY_DIR, today_date.isoformat() + ".json")
    save_json(path, {"nonfoil": compact_prices(nonfoil), "foil": compact_prices(foil)})


def _list_history():
    if not os.path.isdir(HISTORY_DIR):
        return []
    out = []
    for filename in os.listdir(HISTORY_DIR):
        if not filename.endswith(".json"):
            continue
        try:
            d = datetime.strptime(filename[:-5], "%Y-%m-%d").date()
        except ValueError:
            continue
        out.append((d, filename))
    out.sort()
    return out


def load_latest_snapshot_before(today_date):
    """Neuester Tages-Snapshot mit Datum vor heute (ueberbrueckt Tage ohne Lauf)."""
    candidates = [x for x in _list_history() if x[0] < today_date]
    if not candidates:
        return None, None
    newest_date, newest_filename = candidates[-1]
    return load_json(os.path.join(HISTORY_DIR, newest_filename)), newest_date


def load_7d_baseline(today_date):
    candidates = [
        x for x in _list_history()
        if (today_date - x[0]).days >= MIN_7D_BASELINE_AGE_DAYS
    ]
    if not candidates:
        return None, None
    d, filename = candidates[0]
    return load_json(os.path.join(HISTORY_DIR, filename)), d


def prune_old_history(today_date):
    for d, filename in _list_history():
        if (today_date - d).days > HISTORY_RETENTION_DAYS:
            os.remove(os.path.join(HISTORY_DIR, filename))


def main():
    now = datetime.now(timezone.utc)
    today_date = now.date()

    if os.path.exists(LATEST_PATH):
        shutil.copyfile(LATEST_PATH, PREVIOUS_PATH)

    url = get_bulk_download_url()
    print(f"Lade {url}")
    nonfoil, foil = download_and_extract_prices(url)
    print(f"{len(nonfoil)} Non-Foil, {len(foil)} Foil Preise")

    save_json(LATEST_PATH, {"nonfoil": nonfoil, "foil": foil})

    baseline_24h, baseline_24h_date = load_latest_snapshot_before(today_date)
    if baseline_24h is None and os.path.exists(PREVIOUS_PATH):
        baseline_24h = load_json(PREVIOUS_PATH)
    has_24h = baseline_24h is not None
    if not has_24h:
        baseline_24h = {}

    spikes_nonfoil = compute_spikes_vs_baseline(
        baseline_24h.get("nonfoil"), nonfoil, MIN_PRICE_AFTER_SPIKE, MIN_PCT_CHANGE)
    spikes_foil = compute_spikes_vs_baseline(
        baseline_24h.get("foil"), foil, MIN_PRICE_AFTER_SPIKE, MIN_PCT_CHANGE)

    save_history_snapshot(today_date, nonfoil, foil)

    baseline_7d, baseline_7d_date = load_7d_baseline(today_date)
    has_7d = baseline_7d is not None
    if has_7d:
        spikes_nonfoil_7d = compute_spikes_vs_baseline(
            baseline_7d.get("nonfoil"), nonfoil, MIN_PRICE_AFTER_SPIKE, MIN_PCT_CHANGE_7D)
        spikes_foil_7d = compute_spikes_vs_baseline(
            baseline_7d.get("foil"), foil, MIN_PRICE_AFTER_SPIKE, MIN_PCT_CHANGE_7D)
    else:
        spikes_nonfoil_7d, spikes_foil_7d = [], []

    prune_old_history(today_date)

    save_json(OUTPUT_PATH, {
        "generated_at": now.isoformat(),
        "compared_to_previous_run": has_24h,
        "baseline_24h_date": baseline_24h_date.isoformat() if baseline_24h_date else None,
        "card_count_nonfoil": len(nonfoil),
        "card_count_foil": len(foil),
        "spikes_nonfoil": spikes_nonfoil,
        "spikes_foil": spikes_foil,
        "has_7d_comparison": has_7d,
        "baseline_7d_date": baseline_7d_date.isoformat() if baseline_7d_date else None,
        "spikes_nonfoil_7d": spikes_nonfoil_7d,
        "spikes_foil_7d": spikes_foil_7d,
    })
    print(f"Fertig: {len(spikes_nonfoil)} Non-Foil, {len(spikes_foil)} Foil Spikes (24h)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        sys.exit(1)
