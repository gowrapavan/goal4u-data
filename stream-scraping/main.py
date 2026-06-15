import os
import json
from core.team_data import load_team_data
# Import your scrapers
from scrapers import sportsonline, hesgoal, freekora ,yallashots  # 👈 Added freekora import

JSON_FOLDER = os.path.join(os.path.dirname(__file__), "json")
os.makedirs(JSON_FOLDER, exist_ok=True)

# Map output filenames to their respective scrape functions
SOURCES = {
    "sportsonline.json": sportsonline.fetch,
    "hesgoal.json": hesgoal.scrape,
    "freekora.json": freekora.scrape,  # 👈 Added freekora to the list
    "yallashots.json": yallashots.scrape, # 👈 Add here
}

def run_scrapers():
    print("Pre-loading team data...")
    load_team_data()

    for filename, fetch_function in SOURCES.items():
        print(f"Starting {filename}...")
        try:
            data = fetch_function()
            if data:
                filepath = os.path.join(JSON_FOLDER, filename)
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                print(f"✅ Saved {filename} with {len(data)} entries")
            else:
                print(f"⚠️ {filename} returned no data, keeping previous file.")
        except Exception as e:
            print(f"❌ Failed to fetch {filename}: {e}")

if __name__ == "__main__":
    run_scrapers()