#!/usr/bin/env python3
"""
Generates collection-log.json for CollectionLogPopupEnhanced by joining two public OSRS Wiki data
sources on item id:

1. The wiki's own canonical collection log item list:

    https://oldschool.runescape.wiki/w/Module:Collection_log/data.json?action=raw

   A flat array of every real collection log item ({"id", "name", "tabs"}). "tabs" mixes specific
   page names (e.g. "Zulrah") with broader UI categories (e.g. "Slayer", "All Pets") - there's no
   field distinguishing which, but it's enough to know which page(s) an item belongs to, and pets
   are tagged with the "All Pets" category (RarityResolver uses this to detect pets by name,
   instead of a hand-maintained list).

2. The wiki's per-item collection log completion percentages:

    https://oldschool.runescape.wiki/w/Module:Collection_log/completion.json?action=raw

   The same data the wiki's {{Collection log list}} template reads via Module:Collection_log (which
   loads it with mw.loadJsonData("Module:Collection_log/completion.json")) - a flat
   {item id: completion percent} map.

Every id from source 1 gets an entry; "comp" is null for ids with no completion score yet (e.g.
recently-added items the wiki hasn't gathered stats for). An id present in source 2 but missing from
source 1 (shouldn't normally happen) still gets a minimal entry logged to stderr, rather than
silently losing that item's score.

Output shape: { "<item id>": {"name": <str>, "tabs": [<str>, ...], "comp": <float|null>}, ... }
Output: collection-log.json (next to this script). The "update-plugin-data" workflow publishes it to
the orphan "data" branch, which CollectionLogPopupEnhanced's RemoteRarityOverridesUpdater fetches at
runtime.

Re-run this whenever completion percentages should be refreshed:
    python generate-collection-log.py
"""
import json
import sys
import urllib.request
from pathlib import Path

CLOG_DATA_URL = "https://oldschool.runescape.wiki/w/Module:Collection_log/data.json?action=raw"
COMPLETION_URL = "https://oldschool.runescape.wiki/w/Module:Collection_log/completion.json?action=raw"
USER_AGENT = "CollectionLogPopupEnhancedDataGen/1.0 (RuneLite plugin drop-rate dataset generator)"

OUTPUT_PATH = Path(__file__).resolve().parent / "collection-log.json"


def fetch_json(url, timeout=30):
    """Fetches and parses a JSON document from url, with the shared User-Agent header."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    print("Fetching canonical collection log item list...", file=sys.stderr)
    clog_data = fetch_json(CLOG_DATA_URL)
    print(f"Found {len(clog_data)} collection log items.", file=sys.stderr)

    print("Fetching collection log completion percentages...", file=sys.stderr)
    completion_by_id = fetch_json(COMPLETION_URL)

    merged = {}
    for entry in clog_data:
        item_id = entry.get("id")
        if item_id is None:
            continue
        merged[str(item_id)] = {
            "name": entry.get("name"),
            "tabs": entry.get("tabs") or [],
            "comp": None,
        }

    missing_from_clog_data = 0
    for id_str, percent in completion_by_id.items():
        if id_str in merged:
            merged[id_str]["comp"] = percent
        else:
            missing_from_clog_data += 1
            print(f"  warning: completion.json id {id_str!r} not found in data.json - adding a minimal entry", file=sys.stderr)
            merged[id_str] = {"name": None, "tabs": [], "comp": percent}

    with_comp = sum(1 for v in merged.values() if v["comp"] is not None)
    without_comp = len(merged) - with_comp
    print(f"{with_comp} items have a completion percentage, {without_comp} do not yet "
          f"(new items), {missing_from_clog_data} came from completion.json only.", file=sys.stderr)

    sorted_ids = sorted(merged, key=int)

    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump({k: merged[k] for k in sorted_ids}, f, indent="\t")
        f.write("\n")
    print(f"Wrote {len(merged)} entries to {OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
