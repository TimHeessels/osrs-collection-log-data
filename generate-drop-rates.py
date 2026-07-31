#!/usr/bin/env python3
"""
Generates drop-rates.json for CollectionLogPopupEnhanced from two public OSRS Wiki data sources.

1. The wiki's own canonical collection log item list, published as a Lua module data file:

    https://oldschool.runescape.wiki/w/Module:Collection_log/data.json?action=raw

   A flat array of every real collection log item ({"id", "name", "tabs"}). Only "name" is
   used here - it's what drives which items get looked up below. "tabs" (UI category labels
   like "Bosses"/"Miscellaneous"/"Slayer") are NOT used as source names; they're often not a
   single specific NPC (see step 2).

2. The "dropsline" data bucket, queried per item name:

    bucket("dropsline").select("drop_json","page_name").where("item_name","<name>").limit(500).run()

   This is the wiki's own structured/normalized drop-table data (the same store its
   {{DropsLine}} templates populate), so it needs no wikitext/regex parsing and handles
   per-page quirks (custom section headers, on/off-task splits, superior variants, chest
   reward tables) that made raw-wikitext scraping unreliable - see KC-loot-Todo.md's prior
   note on non-boss monsters. Driving this per-item (rather than per a hand-picked source
   list) means every real source - not just tracked-KC bosses - gets discovered automatically:
   an item like "Dragon chainbody" comes back with one row per NPC it can drop from (Kalphite
   Queen, several Slayer monster variants, etc.), each with its own page_name and rate. Since
   every item queried is already a genuine collection log entry (from the module data above),
   there's no need for a "how rare is notable" heuristic - everything parseable is kept, even
   very common rates.

Each row's "Rarity" (num/den, sometimes comma-separated) and "Rolls" are combined into a
single per-kill probability:

    p = 1 - (1 - numerator/denominator) ** rolls

"Alt Rarity" (used by the wiki for on-task/off-task slayer splits) is intentionally ignored -
KillCountTracker has no notion of on/off-task state, so the base "Rarity" is the one that
matches what a player would actually see. Entries with a non-numeric Rarity (e.g. "Always",
guaranteed rewards, or unparseable) are skipped and logged to stderr.

An item that drops from several real sources at different rates (like Dragon chainbody above)
ends up with one entry per source in the output - DropRateResolver.dropProbability(source, item)
picks the right one when the source is known (a correlated kill). When it isn't,
DropRateResolver.dropProbabilityByItemName(item) intentionally returns null for anything with
more than one source entry, rather than guessing which rate applies.

Output: drop-rates.json (next to this script). The "update-plugin-data" workflow publishes it to
the orphan "data" branch, which CollectionLogPopupEnhanced's RemoteDropRateUpdater fetches at
runtime.
Shape: { "Source name": { "Item name": probability (0-1), ... }, ... }

Re-run this whenever drop rates change:
    python generate-drop-rates.py

Queries ~1,700 items against the wiki (one request each, politely rate-limited), so this takes
several minutes to run.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BUCKET_API_URL = "https://oldschool.runescape.wiki/api.php"
CLOG_ITEMS_URL = "https://oldschool.runescape.wiki/w/Module:Collection_log/data.json?action=raw"
USER_AGENT = "CollectionLogPopupEnhancedDataGen/1.0 (RuneLite plugin drop-rate dataset generator)"

OUTPUT_PATH = Path(__file__).resolve().parent / "drop-rates.json"

RARITY_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)?)\s*/\s*(?P<den>\d+(?:\.\d+)?)$")


def combined_probability(num, den, rolls=1):
    """Combines a per-roll rarity (num/den) and roll count into a single per-kill/opening
    probability of getting the item at least once: p = 1 - (1 - num/den) ** rolls."""
    return 1 - (1 - num / den) ** rolls


def parse_rarity(rarity):
    """Returns (numerator, denominator) as floats, or None if unparseable (e.g. "Always",
    or a nested-template rarity the bucket didn't resolve to a plain fraction)."""
    stripped = str(rarity).replace(",", "").strip()
    m = RARITY_RE.match(stripped)
    if not m:
        return None
    return float(m.group("num")), float(m.group("den"))


def fetch_clog_item_names():
    """Returns the sorted, de-duplicated list of every real collection log item's display name,
    from the wiki's own canonical module data."""
    req = urllib.request.Request(CLOG_ITEMS_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        entries = json.loads(resp.read().decode("utf-8"))
    return sorted({entry["name"] for entry in entries if entry.get("name")})


def fetch_dropsline_by_item(item_name):
    """Queries the wiki's dropsline bucket for every source an item can drop from, returning a
    list of (page_name, drop_json) tuples."""
    escaped = item_name.replace('"', '\\"')
    query = f'bucket("dropsline").select("drop_json","page_name").where("item_name","{escaped}").limit(500).run()'
    params = urllib.parse.urlencode({"action": "bucket", "format": "json", "query": query})
    url = f"{BUCKET_API_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    if "error" in data:
        raise RuntimeError(data["error"])

    rows = []
    for row in data.get("bucket", []):
        dj = row.get("drop_json")
        if isinstance(dj, str):
            dj = json.loads(dj)
        if isinstance(dj, dict):
            rows.append((row.get("page_name"), dj))
    return rows


def parse_item_sources(item_name, rows):
    """Returns {source page_name: probability} for one item's dropsline rows, keeping the first
    row seen per source (e.g. a page with both a "Regular" and "Wilderness" variant entry for the
    same source) and skipping unparseable/non-numeric rarities."""
    sources = {}
    for page_name, dj in rows:
        if not page_name or page_name in sources:
            continue

        rarity = dj.get("Rarity")
        if not rarity:
            continue

        parsed = parse_rarity(rarity)
        if parsed is None:
            print(f"  skip [{page_name}] {item_name!r}: unparseable rarity {rarity!r}", file=sys.stderr)
            continue
        num, den = parsed
        if den <= 0:
            continue

        rolls_raw = dj.get("Rolls")
        try:
            rolls = int(rolls_raw) if rolls_raw not in (None, "") else 1
        except (TypeError, ValueError):
            rolls = 1

        sources[page_name] = combined_probability(num, den, rolls)
    return sources


def main():
    print("Fetching canonical collection log item list...", file=sys.stderr)
    item_names = fetch_clog_item_names()
    print(f"Found {len(item_names)} collection log items to query.", file=sys.stderr)

    dataset = {}
    for i, item_name in enumerate(item_names):
        if i % 100 == 0:
            print(f"  [{i}/{len(item_names)}] ...", file=sys.stderr)

        try:
            rows = fetch_dropsline_by_item(item_name)
        except (urllib.error.URLError, RuntimeError) as e:
            print(f"  FAILED to fetch {item_name!r}: {e}", file=sys.stderr)
            continue

        for source, probability in parse_item_sources(item_name, rows).items():
            dataset.setdefault(source, {})[item_name] = probability

        time.sleep(0.3)  # be polite to the wiki

    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(dataset, f, indent="\t", sort_keys=True)
        f.write("\n")

    total_entries = sum(len(v) for v in dataset.values())
    print(f"Wrote {total_entries} drop entries across {len(dataset)} sources to {OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
