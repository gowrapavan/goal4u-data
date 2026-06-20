import os
import json
from core.team_data import load_team_data
from core.hd_extractor import extract_m3u8_matches
# Import your scrapers
from scrapers import (
    sportsonline,
    hesgoal,
    freekora,
    yallashots,
    live_soccer,
    koralivetv,
    koraaclub,
    yallasport,
)

# main.py lives at <root>/stream-scraping/main.py, so one level up is <root>.
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSON_FOLDER = os.path.join(ROOT_DIR, "stream-data")
os.makedirs(JSON_FOLDER, exist_ok=True)

# Map output filenames to their respective scrape functions
SOURCES = {
    "sportsonline.json": sportsonline.fetch,
    "hesgoal.json": hesgoal.scrape,
    "freekora.json": freekora.scrape,
    "yallashots.json": yallashots.scrape,
    "live_soccer.json": live_soccer.scrape,
    "koralivetv.json": koralivetv.scrape,
    "koraaclub.json": koraaclub.scrape,  # 👈 source of the m3u8 "gold" links
    "yallasport.json": yallasport.fetch,
}

def run_scrapers():
    print("Pre-loading team data...")
    load_team_data()

    hd_streams = []  # aggregated across every source below

    for filename, fetch_function in SOURCES.items():
        print(f"Starting {filename}...")
        try:
            data = fetch_function()
            if data:
                filepath = os.path.join(JSON_FOLDER, filename)
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                print(f"✅ Saved {filename} with {len(data)} entries")

                source_name = filename.replace(".json", "")
                hd_streams.extend(extract_m3u8_matches(source_name, data))
            else:
                print(f"⚠️ {filename} returned no data, keeping previous file.")
        except Exception as e:
            print(f"❌ Failed to fetch {filename}: {e}")

    # Save every .m3u8 link found across all sources into its own file
    hd_path = os.path.join(JSON_FOLDER, "hd_streams.json")
    with open(hd_path, "w", encoding="utf-8") as f:
        json.dump(hd_streams, f, ensure_ascii=False, indent=2)
    print(f"✅ Saved hd_streams.json with {len(hd_streams)} entries")

if __name__ == "__main__":
    run_scrapers()