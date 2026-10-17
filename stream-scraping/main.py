import argparse
import json
import os
import time

from core.team_data import load_team_data
from scrapers import fabortv, kooraclub, sportsonline, yallashots

# Optional helpers: used only if the files exist in core/.
#   core/translate.py     -> Arabic -> English for team/league/status text
#   core/hd_extractor.py  -> collects direct .m3u8 links into hd_streams.json
try:
    from core.translate import translate_matches
except ModuleNotFoundError:
    translate_matches = None

try:
    from core.hd_extractor import extract_m3u8_matches
except ModuleNotFoundError:
    extract_m3u8_matches = None

# main.py lives at <root>/stream-scraping/main.py, so one level up is <root>.
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_FOLDER = os.path.join(ROOT_DIR, "stream-data")

# Output filename -> scrape function.
# To add a source: import its module above and add a line here.
SOURCES = {
    "sportsonline.json": sportsonline.fetch,
    "fabortv.json": fabortv.scrape,
    "kooraclub.json": kooraclub.scrape,
    "yallashots.json": yallashots.scrape,
}


def has_stream_link(m):
    """True if the entry carries a usable stream link (not just a match/article page)."""
    if m.get("m3u8") or m.get("servers"):
        return True
    url = m.get("url") or ""
    if not url.startswith("http"):
        return False
    article = m.get("article_url")
    return url != article if article else True


def run_scrapers(only=None):
    os.makedirs(JSON_FOLDER, exist_ok=True)

    print("Pre-loading team data...")
    load_team_data()

    selected = {
        name: fn
        for name, fn in SOURCES.items()
        if not only or name.replace(".json", "") in only
    }

    hd_streams = []
    summary = []  # (source, status, matches, with_link, seconds)

    for filename, fetch_function in selected.items():
        source = filename.replace(".json", "")
        print(f"\nStarting {filename}...")
        t0 = time.monotonic()
        try:
            data = fetch_function()
            if data:
                if translate_matches:
                    data = translate_matches(data)

                filepath = os.path.join(JSON_FOLDER, filename)
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                print(f"✅ Saved {filename} with {len(data)} entries")

                if extract_m3u8_matches:
                    hd_streams.extend(extract_m3u8_matches(source, data))

                summary.append((source, "OK", len(data),
                                sum(1 for m in data if has_stream_link(m)),
                                time.monotonic() - t0))
            else:
                print(f"⚠️ {filename} returned no data, keeping previous file.")
                summary.append((source, "EMPTY", 0, 0, time.monotonic() - t0))
        except Exception as e:
            print(f"❌ Failed to fetch {filename}: {e}")
            summary.append((source, "FAILED", 0, 0, time.monotonic() - t0))

    # Only write hd_streams.json when the extractor is available and every
    # source was run (a partial run would overwrite it with partial data).
    if extract_m3u8_matches and not only:
        hd_path = os.path.join(JSON_FOLDER, "hd_streams.json")
        with open(hd_path, "w", encoding="utf-8") as f:
            json.dump(hd_streams, f, ensure_ascii=False, indent=2)
        print(f"\n✅ Saved hd_streams.json with {len(hd_streams)} entries")

    print_summary(summary)


def print_summary(summary):
    print("\n" + "=" * 62)
    print(f"{'SOURCE':<16}{'STATUS':<10}{'MATCHES':>8}{'WITH LINK':>11}{'TIME':>8}")
    print("-" * 62)
    for source, status, total, linked, secs in summary:
        icon = {"OK": "✅", "EMPTY": "⚪", "FAILED": "❌"}[status]
        print(f"{source:<16}{icon} {status:<7}{total:>8}{linked:>11}{secs:>7.1f}s")
    print("=" * 62)
    print("WITH LINK = entries that have a stream/player link, not just a match page.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the stream scrapers.")
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="SOURCE",
        choices=[n.replace(".json", "") for n in SOURCES],
        help="Run only these sources, e.g. --only fabortv",
    )
    args = parser.parse_args()
    run_scrapers(only=args.only)