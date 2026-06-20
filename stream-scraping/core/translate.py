# core/translate.py
"""
Real machine-translation based Arabic -> English translator for scraper
output (team names, league text, status text, commentator) -- not a
hardcoded phrase table. Uses deep-translator's GoogleTranslator (free,
no API key) under the hood.

Two things keep this fast and resilient against an ephemeral CI runner:

1. Disk cache (core/translation_cache.json, tracked in git, NOT
   gitignored). GitHub Actions wipes the filesystem every run, so without
   committing this back, every run would re-translate the same recurring
   team/league strings from scratch. With it, only genuinely new strings
   ever hit the network.

2. Batched calls. All untranslated strings across a source's matches are
   collected and sent in ONE translate_batch() call instead of one
   request per field per match -- fewer round trips, less likely to get
   rate-limited.

If the translator package is missing, the API is unreachable, or a call
fails for any reason, this falls back to the original (untranslated)
text rather than crashing the scrape run.
"""

import os
import re
import json

try:
    from deep_translator import GoogleTranslator
except ImportError:
    GoogleTranslator = None

from core.utils import short_label

CACHE_PATH = os.path.join(os.path.dirname(__file__), "translation_cache.json")
ARABIC_RE = re.compile(r"[\u0600-\u06FF]")
TRANSLATABLE_FIELDS = ("home_team", "away_team", "league", "status_text", "commentator")

_cache = {}
_cache_dirty = False
_translator = None
_translator_broken = False  # stop retrying mid-run after the first failure


def _load_cache():
    global _cache
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                _cache = json.load(f)
        except Exception as e:
            print(f"⚠️ Could not read translation cache, starting fresh: {e}")
            _cache = {}


def _save_cache():
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_cache, f, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception as e:
        print(f"⚠️ Could not write translation cache: {e}")


_load_cache()


def _has_arabic(text):
    return bool(ARABIC_RE.search(text or ""))


def _collect_untranslated(matches):
    """Unique Arabic strings across all matches' translatable fields that
    aren't already in the cache."""
    needed = set()
    for m in matches or []:
        for field in TRANSLATABLE_FIELDS:
            val = (m.get(field) or "").strip()
            if val and _has_arabic(val) and val not in _cache:
                needed.add(val)
    return sorted(needed)


def translate_text(text):
    """Look up a string's translation from the cache. Returns the
    original unchanged if it's not Arabic or wasn't translated this run
    (e.g. the API call failed)."""
    if not text:
        return text
    stripped = text.strip()
    if not stripped or not _has_arabic(stripped):
        return text
    return _cache.get(stripped, text)


def translate_match(match):
    """Shallow copy of `match` with translatable fields swapped for their
    cached English translation, and `label` regenerated from the
    translated names."""
    m = dict(match)
    for field in TRANSLATABLE_FIELDS:
        if field in m:
            m[field] = translate_text(m[field])
    if "label" in m and ("home_team" in m or "away_team" in m):
        m["label"] = short_label(m.get("home_team"), m.get("away_team"))
    return m


def translate_matches(matches):
    """Batch-translate every new Arabic string found across `matches`,
    then return translated copies of every match dict."""
    global _cache_dirty, _translator, _translator_broken

    matches = matches or []
    to_translate = _collect_untranslated(matches)

    if to_translate:
        if GoogleTranslator is None:
            print("⚠️ deep-translator not installed -- skipping translation (pip install deep-translator)")
        elif _translator_broken:
            pass  # already failed once this run, don't keep hammering a dead endpoint
        else:
            try:
                if _translator is None:
                    _translator = GoogleTranslator(source="ar", target="en")
                results = _translator.translate_batch(to_translate)
                for original, translated in zip(to_translate, results):
                    _cache[original] = (translated or "").strip() or original
                    _cache_dirty = True
                print(f"🌐 Translated {len(to_translate)} new strings")
            except Exception as e:
                print(f"⚠️ Batch translation failed ({len(to_translate)} strings): {e}")
                _translator_broken = True

    result = [translate_match(m) for m in matches]

    if _cache_dirty:
        _save_cache()

    return result