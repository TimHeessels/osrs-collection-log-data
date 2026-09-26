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

"Alt Rarity" is intentionally ignored where the wiki uses it for on-task/off-task slayer splits -
KillCountTracker has no notion of on/off-task state, so the base "Rarity" is the one that
matches what a player would actually see. That's the common case by far: "Alt Rarity" is populated
on hundreds of pages, nearly all of them slayer splits. Entries with a non-numeric Rarity (e.g.
"Always", guaranteed rewards, or unparseable) are skipped and logged to stderr.

The exception is a row the wiki flags with "Alt Rarity Dash": "yes". There the two rarities aren't
alternatives to choose between - they're the ends of one range the wiki renders as "1/1,350-1/540",
because the rate scales with something the drop table doesn't fix. Doom of Mokhaiotl is the clearest
case (rates improve with delve level: Avernic treads is 1/1,350 at delve 4 and 1/540 at delve 9), and
Tombs of Amascut (invocation level), The Nightmare (team size) and Zalcano work the same way. Keeping
only the first end would report the worst rate as if it were the only one, so both ends are emitted -
see RANGE_SOURCE_SUFFIX.

The flag isn't proof of a range on its own: the wiki sets it on skilling pet rows as well, where the
pair is just rounding noise rather than a scale (a pet's rate moves with xp and activity, and the
wiki splits that across one row per level instead). MIN_RANGE_RATIO is what separates the two.

An item that drops from several real sources at different rates (like Dragon chainbody above)
ends up with one entry per source in the output - DropRateResolver.dropProbability(source, item)
picks the right one when the source is known (a correlated kill). When it isn't,
DropRateResolver.dropProbabilityByItemName(item) intentionally returns null for anything with
more than one source entry, rather than guessing which rate applies. Splitting a variant below has
the same effect on purpose: with no correlated kill there are two real rates and no basis to choose
between them, so showing both beats showing whichever one happened to win.

A few wiki pages host several drop tables at once rather than one, distinguished only by the anchor
in each row's "Dropped from" field ("<page_name>#<anchor>"). The Gauntlet's reward chest is the clear
case - it has Regular, Corrupted, Failure and Partial completion sections, and the rates genuinely
differ: Youngllef is 1/2,000 from a regular Gauntlet but 1/800 from a corrupted one. VARIANT_SOURCE_NAMES
lists the pages worth splitting and maps each anchor to the source name KillCountTracker derives from
the in-game chat message ("Corrupted Gauntlet"), so DropRateResolver's exact source lookup resolves
the right table. It's an allowlist rather than a blanket split because nearly every other anchor on
the wiki separates something no chat message reveals - combat level, fishing method, region,
on/off-task state - and emitting those as sources would both add keys nothing can look up and make
dropProbabilityByItemName ambiguous for items that resolve cleanly today.

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

# Pages whose dropsline rows are really several drop tables, keyed by the anchor the wiki puts in
# "Dropped from" ("<page_name>#<anchor>"). Each value is the source string KillCountTracker derives
# from the in-game chat message, so DropRateResolver's exact-key lookup resolves it.
#
# An allowlist rather than a blanket split on every anchor: ~110 distinct anchors exist across the
# wiki's pages and almost none are variants a player's chat message distinguishes - they're combat
# levels ("Rock crab#Level 13"), fishing methods ("#Small net"), regions ("#Varlamore"), on/off-task
# splits, equipment states ("Skeleton#Armed"). Emitting those as sources would add keys nothing can
# look up and push their items from one source entry to several, which makes
# dropProbabilityByItemName ambiguous (see its javadoc) and replaces a correct single rate in the
# overlay with a list. Anchors are an incomplete signal anyway: several genuinely conflicting pages
# (Ancient chest's CoX normal vs Challenge Mode, Nex, Araxxor) carry no anchor at all.
VARIANT_SOURCE_NAMES = {
    "Reward Chest (The Gauntlet)": {
        "Regular": "Gauntlet",
        "Corrupted": "Corrupted Gauntlet",
    },
}

# Suffix for the second end of a ranged rate (see the "Alt Rarity Dash" note above). Both ends have
# to be separate sources because the overlay derives its range from the spread across an item's
# sources - CollectionLogOverlay's DROP_RATE case renders min and max as "1/2500 - 1/540" - and the
# dataset's per-item value has to stay a bare number for Gson (DropRateResolver.DATASET_TYPE is
# Map<String, Map<String, Double>>), so a pair can't live in the value itself.
#
# No chat message ever produces the suffixed name, which is the point: dropProbability(source, item)
# still resolves the unsuffixed source exactly as before, and the extra entry only shows up through
# dropRatesByItemName - the uncorrelated path, where a range is all that can honestly be shown.
RANGE_SOURCE_SUFFIX = " (max)"

# How far apart the two ends have to be before a range is worth showing. The wiki sets the dash flag
# on skilling pet rows too, but there the pair isn't a scale - a pet's rate varies with xp and
# activity, which the wiki models as a separate row per level ("Boat fishing spot (small net, bait)"
# has one row for level 1 and another for level 5). The two numbers inside a single pet row differ by
# a few hundredths of a percent of rounding (Heron 1/870,305 against 1/867,855, 1.003x), so emitting
# them produced honest-looking nonsense like "1/867k - 1/870k". A 1.1x floor keeps every real scale -
# delve level, invocation, team size, Ecumenical key's 1/60 vs 1/40 - and drops the noise.
MIN_RANGE_RATIO = 1.1

# Retried suffix for an item the wiki files its drop under the uncharged name - see
# fetch_dropsline_by_item. Enumerated against all 1,625 collection log items: "Eye of Ayak" is
# currently the only one this recovers, but the retry costs one request per otherwise-empty item and
# keeps working if another charged item is added.
UNCHARGED_SUFFIX = " (uncharged)"


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
    list of (page_name, drop_json) tuples.

    An item whose own page is the charged form files its drop row under the uncharged name, while
    the collection log module (and so the log itself) lists the plain one: the log's Doom of
    Mokhaiotl slot is "Eye of Ayak", but the drop is recorded as "Eye of Ayak (uncharged)". An
    exact-match query on the log's name finds nothing there, so the item would silently carry no
    rate at all. Falling back to the suffixed name recovers it - the reverse of the "Gull (pet)"
    case the plugin's CollectionLogSlotNames handles, where the dataset holds the suffixed name and
    the chat message the plain one.
    """
    rows = fetch_dropsline_rows(item_name)
    if not rows:
        rows = fetch_dropsline_rows(item_name + UNCHARGED_SUFFIX)
    return rows


def fetch_dropsline_rows(item_name):
    """One dropsline bucket query, returning a list of (page_name, drop_json) tuples."""
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


def variant_source_name(page_name, dj):
    """Returns the source key one dropsline row belongs under.

    A page hosting several drop tables identifies each row's table in "Dropped from", as
    "<page_name>#<anchor>" - the Gauntlet's reward chest has Regular, Corrupted, Failure and Partial
    completion sections. For a page in VARIANT_SOURCE_NAMES a known anchor becomes the name the
    plugin's kill-count tracker uses for that variant, so each is its own source with its own rates.
    Anything else - unlisted page, unlisted anchor, or no anchor - keeps the page name, which is what
    this script has always done.
    """
    variants = VARIANT_SOURCE_NAMES.get(page_name)
    if not variants:
        return page_name

    _, separator, anchor = str(dj.get("Dropped from") or "").partition("#")
    if not separator:
        return page_name
    return variants.get(anchor.strip(), page_name)


def alt_rarity_probability(dj):
    """Returns the probability at the other end of a ranged rate, or None if the row isn't one.

    Only rows the wiki flags with "Alt Rarity Dash" count. Without that flag an "Alt Rarity" is an
    on-task/off-task slayer split - a rate that applies instead of the base one, not alongside it -
    and folding those into a range would invent a spread the player never sees.

    Which end is the better rate isn't fixed: it's usually the alt one (Doom of Mokhaiotl's delve 9
    floor), but Zalcano shard is 1/750 solo against 1/1,500, so no ordering is assumed here - the
    overlay takes the min and max of whatever it finds.

    The flag alone isn't enough to call something a range, though - see MIN_RANGE_RATIO, applied by
    the caller once both ends are known.
    """
    if str(dj.get("Alt Rarity Dash") or "").strip().lower() != "yes":
        return None

    parsed = parse_rarity(dj.get("Alt Rarity"))
    if parsed is None:
        return None
    num, den = parsed
    if den <= 0:
        return None

    rolls_raw = dj.get("Rolls")
    try:
        rolls = int(rolls_raw) if rolls_raw not in (None, "") else 1
    except (TypeError, ValueError):
        rolls = 1

    return combined_probability(num, den, rolls)


def parse_item_sources(item_name, rows):
    """Returns {source name: probability} for one item's dropsline rows, skipping
    unparseable/non-numeric rarities.

    The source is normally the row's wiki page_name, but a page listed in VARIANT_SOURCE_NAMES splits
    into one source per drop-table variant (see variant_source_name) - the Gauntlet's reward chest
    drops Youngllef at 1/2,000 regular but 1/800 corrupted, and collapsing both onto one key means
    whichever row the wiki happened to return first silently wins.

    For any other page the first row seen per source still wins. That's deliberate: most pages with
    several rows for one item are split by something no chat message reveals (combat level, fishing
    method, on-task state), so there'd be nothing to choose between them with - see
    VARIANT_SOURCE_NAMES.

    A row whose rate is a range rather than a single number contributes a second entry under
    RANGE_SOURCE_SUFFIX, so both ends reach the overlay.
    """
    sources = {}
    for page_name, dj in rows:
        if not page_name:
            continue

        source = variant_source_name(page_name, dj)
        if source in sources:
            continue

        rarity = dj.get("Rarity")
        if not rarity:
            continue

        parsed = parse_rarity(rarity)
        if parsed is None:
            print(f"  skip [{source}] {item_name!r}: unparseable rarity {rarity!r}", file=sys.stderr)
            continue
        num, den = parsed
        if den <= 0:
            continue

        rolls_raw = dj.get("Rolls")
        try:
            rolls = int(rolls_raw) if rolls_raw not in (None, "") else 1
        except (TypeError, ValueError):
            rolls = 1

        sources[source] = combined_probability(num, den, rolls)

        alt = alt_rarity_probability(dj)
        base = sources[source]
        if alt is not None and max(alt, base) / min(alt, base) >= MIN_RANGE_RATIO:
            sources.setdefault(source + RANGE_SOURCE_SUFFIX, alt)
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
