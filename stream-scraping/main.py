import os
import json
from core.team_data import load_team_data

# Import your individual scrapers
from scrapers import sportsonline

# Ensure output directory exists
JSON_FOLDER = os.path.join(os.path.dirname(__file__), "json")
os.makedirs(JSON_FOLDER, exist_ok=True)

# Map the output filename to the module's fetch function
SOURCES = {
    "sportsonline.json": sportsonline.fetch,
    # We will add hesgoal, livekora, etc. here as we build them
}

def run_scrapers():
    print("Pre-loading team data...")
    load_team_data()

    for filename, fetch_function in SOURCES.items():
        print(f"Starting {filename}...")
        try:
            data = fetch_function()
            
            filepath = os.path.join(JSON_FOLDER, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                
            print(f"✅ Saved {filename} with {len(data)} entries")
            
        except Exception as e:
            print(f"❌ Failed to fetch {filename}: {e}")
            filepath = os.path.join(JSON_FOLDER, filename)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump([], f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    run_scrapers()