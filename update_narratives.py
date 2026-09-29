"""
Refresh the automatic part of narratives.txt from what's trending right now.

Sources (free, no keys):
  - DexScreener top boosted tokens   (what projects are paying to push)
  - DexScreener latest token profiles (what new projects are launching around)
  - CoinGecko trending coins + categories (what people are searching for)

Words that show up across several trending Solana tokens become narrative
keywords. Lines you add by hand above the AUTO marker are never touched.

Run:  python update_narratives.py            (writes narratives.txt)
      python update_narratives.py --dry-run  (just prints)
"""

import argparse
import collections
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "narratives.txt")
MARK = "# ---- AUTO (updated daily by update_narratives.py; edits below get overwritten) ----"

SOURCES = {
    "boosts": "https://api.dexscreener.com/token-boosts/top/v1",
    "profiles": "https://api.dexscreener.com/token-profiles/latest/v1",
    "cg_trending": "https://api.coingecko.com/api/v3/search/trending",
}

KEEP_SHORT = {"ai"}  # short words worth keeping
MAX_WORDS = 15
MIN_SOURCES = 2      # a word must appear in at least this many different tokens

STOP = set("""
a an and are as at be been but by can for from has have i if in into is it its
just like more new no not of on or our so than that the their them then there
these they this to up us was we what when where which who will with you your
all any get got one only out over own same some such too very way first best
coin coins token tokens crypto solana sol pump pumpfun fun meme memes memecoin
community project official launch launched chain network web3 dex dexscreener
holders holder buy sell price market cap mc ca contract twitter telegram tg
website site join now today next time world people everyone every most much
make made built build home here back real true life love good big king ever
com www https http app org xyz net day days gets getting got going gonna want need let lets take give keep year years week also still even never always
""".split())


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "pumpbot-narratives/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def words(text):
    return [w for w in re.findall(r"[a-z][a-z0-9]+", (text or "").lower())
            if (len(w) >= 3 or w in KEEP_SHORT) and w not in STOP]


def collect(data):
    """Return one set of words per trending item, so one token can't dominate."""
    items = []
    for it in data.get("boosts") or []:
        if it.get("chainId") == "solana":
            items.append(set(words(it.get("description"))))
    for it in data.get("profiles") or []:
        if it.get("chainId") == "solana":
            items.append(set(words(it.get("description"))))
    cg = data.get("cg_trending") or {}
    for c in cg.get("coins") or []:
        item = c.get("item") or {}
        items.append(set(words(f"{item.get('name')} {item.get('symbol')}")))
    for cat in cg.get("categories") or []:
        items.append(set(words(cat.get("name"))))
    return [s for s in items if s]


def pick(items):
    counts = collections.Counter(w for s in items for w in s)
    return [w for w, n in counts.most_common() if n >= MIN_SOURCES][:MAX_WORDS], counts


def rewrite(new_words, dry):
    manual = []
    if os.path.exists(PATH):
        with open(PATH, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip() == MARK:
                    break
                manual.append(line.rstrip("\n"))
    while manual and not manual[-1].strip():
        manual.pop()
    out = "\n".join(manual + ["", MARK] + new_words) + "\n"
    if dry:
        print(out)
    else:
        with open(PATH, "w", encoding="utf-8") as f:
            f.write(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--from-file", help="use saved JSON instead of the internet (testing)")
    args = ap.parse_args()

    if args.from_file:
        with open(args.from_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}
        for name, url in SOURCES.items():
            try:
                data[name] = fetch(url)
            except Exception as e:
                print(f"warning: {name} failed ({e})", file=sys.stderr)

    items = collect(data)
    if len(items) < 5:
        print("Too little trending data came back; leaving narratives.txt unchanged.")
        return
    chosen, counts = pick(items)
    print(f"{len(items)} trending items -> {len(chosen)} keywords")
    for w in chosen:
        print(f"  {w:<15} in {counts[w]} items")
    rewrite(chosen, args.dry_run)


if __name__ == "__main__":
    main()
