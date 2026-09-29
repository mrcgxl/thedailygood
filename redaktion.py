"""Redaktions-Skript für The Daily Good.

Sammelt Meldungen aus RSS-Quellen, lässt Claude die guten Nachrichten aussuchen
und schreibt daraus die Tagesausgabe als JSON im Format der App.

Aufruf:  .venv/bin/python redaktion.py [--hours 36] [--stories 12] [--dry-run] [--no-batch]
Standard ist die Batch-API (halber Preis). Dauert ein Batch zu lange, wird einzeln nachgefragt.
API-Key: aus ANTHROPIC_API_KEY oder dem macOS-Schlüsselbund (Dienst TheDailyGood-Anthropic).
"""

import argparse
import calendar
import datetime as dt
import html
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import warnings
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

warnings.filterwarnings("ignore")

import anthropic  # noqa: E402
import feedparser  # noqa: E402
import trafilatura  # noqa: E402

from prompts import (  # noqa: E402
    IMAGE_SCHEMA, IMAGE_SYSTEM, JOKE_SCHEMA, JOKE_SYSTEM, STORY_SCHEMA, TRIAGE_SCHEMA, TRIAGE_SYSTEM, WRITER_SYSTEM,
)
from sources import FEEDS  # noqa: E402

MODEL = "claude-opus-5-5"
PRICE_INPUT = 4.0 / 1_000_000   # $ pro Token, Claude Opus 5.5
PRICE_OUTPUT = 20.0 / 1_000_000
FIRST_EDITION = dt.date(2026, 9, 29)
ITEMS_PER_FEED = 15
BATCH_WAIT = 45 * 60  # Sekunden, danach wird einzeln nachgefragt
HERE = Path(__file__).resolve().parent
OUT = HERE / "docs"  # wird von GitHub Pages ausgeliefert
PAGES_URL = "https://mrcgxl.github.io/thedailygood"
USER_AGENT = "TheDailyGood/0.1 (https://github.com/mrcgxl/thedailygood)"
FAVORITES = {"design", "architektur"}  # Mircos Herzensthemen
STATE = HERE / "state"


# MARK: API

def api_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "TheDailyGood-Anthropic", "-w"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sys.exit("Kein API-Key gefunden. Setze ANTHROPIC_API_KEY oder lege ihn im Schlüsselbund ab.")


class Usage:
    def __init__(self):
        self.input = 0
        self.output = 0
        self.dollars = 0.0

    def add(self, usage, factor=1.0):
        tokens_in = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) \
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        tokens_out = usage.output_tokens or 0
        self.input += tokens_in
        self.output += tokens_out
        self.dollars += factor * (tokens_in * PRICE_INPUT + tokens_out * PRICE_OUTPUT)


USAGE = Usage()


class Skipped(Exception):
    pass


def params(system, user, schema, max_tokens, effort):
    return {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    }


def parse(message):
    if message.stop_reason == "refusal":
        raise Skipped("abgelehnt")
    if message.stop_reason == "max_tokens":
        raise Skipped("Antwort zu lang")
    text = next((block.text for block in message.content if block.type == "text"), None)
    if text is None:
        raise Skipped("keine Antwort")
    return json.loads(text)


def ask(client, system, user, schema, max_tokens=16000, effort="medium"):
    """Eine einzelne Anfrage mit garantiertem JSON. Lehnt Opus ab, springt serverseitig ein anderes Modell ein."""
    response = client.beta.messages.create(
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        **params(system, user, schema, max_tokens, effort),
    )
    USAGE.add(response.usage)
    return parse(response)


def run_batch(client, jobs):
    """Alle Anfragen als Batch zum halben Preis. Fehlende oder abgelehnte fehlen im Ergebnis."""
    batch = client.messages.batches.create(
        requests=[{"custom_id": key, "params": params(*job)} for key, job in jobs.items()]
    )
    deadline = time.time() + BATCH_WAIT
    while batch.processing_status != "ended":
        if time.time() > deadline:
            client.messages.batches.cancel(batch.id)
            print("   Batch dauert zu lange, frage einzeln nach")
            return {}
        time.sleep(15)
        batch = client.messages.batches.retrieve(batch.id)
    results = {}
    for entry in client.messages.batches.results(batch.id):
        if entry.result.type != "succeeded":
            continue
        USAGE.add(entry.result.message.usage, factor=0.5)
        try:
            results[entry.custom_id] = parse(entry.result.message)
        except (Skipped, json.JSONDecodeError):
            pass  # Ablehnung oder kaputte Antwort: einzeln mit Absicherung nachholen
    return results


def ask_all(client, jobs, use_batch):
    """Mehrere Anfragen auf einmal. jobs: {Schlüssel: (system, user, schema, max_tokens, effort)}.
    Gibt {Schlüssel: Ergebnis} zurück, gescheiterte Anfragen als Fehlerobjekt."""
    results = {}
    if use_batch:
        try:
            results = run_batch(client, jobs)
        except anthropic.APIError as error:
            print(f"   Batch nicht möglich ({error}), frage einzeln nach")
    missing = [key for key in jobs if key not in results]

    def single(key):
        try:
            return ask(client, *jobs[key])
        except (Skipped, anthropic.APIError, json.JSONDecodeError) as error:
            return error

    with ThreadPoolExecutor(4) as pool:
        for key, outcome in zip(missing, pool.map(single, missing)):
            results[key] = outcome
    return results


# MARK: Meldungen sammeln

TAG = re.compile(r"<[^>]+>")
SPACE = re.compile(r"\s+")


def clean(text, limit):
    text = SPACE.sub(" ", html.unescape(TAG.sub(" ", text or ""))).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


def read_feed(feed, since):
    name, url, language = feed
    try:
        parsed = feedparser.parse(url, agent="TheDailyGood/0.1")
    except Exception as error:  # Eine kaputte Quelle darf die Ausgabe nicht stoppen
        print(f"  Quelle übersprungen: {name} ({error})")
        return []
    items = []
    for entry in parsed.entries:
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        if not stamp or calendar.timegm(stamp) < since:
            continue
        title = clean(entry.get("title"), 200)
        link = entry.get("link")
        if not title or not link:
            continue
        items.append({
            "source": name,
            "language": language,
            "title": title,
            "summary": clean(entry.get("summary") or entry.get("description"), 280),
            "link": link,
            "time": calendar.timegm(stamp),
        })
    items.sort(key=lambda item: -item["time"])
    return items[:ITEMS_PER_FEED]


def collect(hours):
    since = time.time() - hours * 3600
    with ThreadPoolExecutor(12) as pool:
        batches = list(pool.map(lambda feed: read_feed(feed, since), FEEDS))
    seen_links, seen_titles, items = set(), set(), []
    for batch in batches:
        for item in batch:
            key = item["title"].lower()[:80]
            if item["link"] in seen_links or key in seen_titles:
                continue
            seen_links.add(item["link"])
            seen_titles.add(key)
            items.append(item)
    for number, item in enumerate(items, 1):
        item["id"] = f"i{number}"
    return items


# MARK: Auswahl

def triage(client, items, use_batch):
    lines = [f"[{i['id']}] {i['source']} ({i['language']}): {i['title']}. {i['summary'][:160]}" for i in items]
    user = "Hier sind die Meldungen der letzten Stunden:\n\n" + "\n".join(lines)
    result = ask_all(client, {"triage": (TRIAGE_SYSTEM, user, TRIAGE_SCHEMA, 16000, "medium")}, use_batch)["triage"]
    if isinstance(result, Exception):
        raise result
    by_id = {item["id"]: item for item in items}
    candidates = []
    for candidate in result["candidates"]:
        members = [by_id[i] for i in candidate["item_ids"] if i in by_id]
        if members:
            candidate["items"] = members
            candidates.append(candidate)
    STATE.mkdir(exist_ok=True)
    (STATE / "last-candidates.json").write_text(json.dumps(candidates, ensure_ascii=False, indent=1))
    return candidates


def select(candidates, target, per_ressort=2):
    pool = sorted((c for c in candidates if c["score"] >= 5), key=lambda c: -c["score"])
    chosen, counts = [], Counter()
    for candidate in pool:  # Zuerst das Beste aus jedem Ressort, Design und Architektur schon ab 5 Punkten
        good_enough = candidate["score"] >= 6 or candidate["ressort"] in FAVORITES
        if good_enough and counts[candidate["ressort"]] == 0 and len(chosen) < target:
            chosen.append(candidate)
            counts[candidate["ressort"]] += 1
    for candidate in pool:  # Dann nach Punkten auffüllen
        if len(chosen) >= target:
            break
        if candidate in chosen or candidate["score"] < 6 or counts[candidate["ressort"]] >= per_ressort:
            continue
        chosen.append(candidate)
        counts[candidate["ressort"]] += 1
    chosen.sort(key=lambda c: -c["score"])
    lead = next((c for c in chosen if not c["heavy"]), chosen[0] if chosen else None)
    rest = [c for c in chosen if c is not lead]
    ordered = [lead] if lead else []
    while rest:  # Gleiche Ressorts nicht direkt hintereinander
        pick = next((c for c in rest if not ordered or c["ressort"] != ordered[-1]["ressort"]), rest[0])
        ordered.append(pick)
        rest.remove(pick)
    return ordered


# MARK: Schreiben

def fetch_article(url):
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ""
        return (trafilatura.extract(downloaded, include_comments=False, include_tables=False) or "")[:12000]
    except Exception:  # Paywalls und Zeitüberschreitungen sind normal
        return ""


def best_text(candidate):
    texts = []
    for item in candidate["items"][:2]:
        text = fetch_article(item["link"])
        if len(text) >= 800:
            return item, text
        texts.append((item, text))
    return max(texts, key=lambda pair: len(pair[1]))


DASHES = re.compile(r"\s*[–—]\s*")
RANGE = re.compile(r"(\d)\s*[–—]\s*(\d)")


def no_dashes(value):
    """Sicherheitsnetz für Mircos Stilregel: keine Gedankenstriche."""
    if isinstance(value, str):
        return DASHES.sub(", ", RANGE.sub(r"\1 bis \2", value))
    if isinstance(value, list):
        return [no_dashes(v) for v in value]
    if isinstance(value, dict):
        return {k: no_dashes(v) for k, v in value.items()}
    return value


def story_prompt(candidate):
    item, text = best_text(candidate)
    user = (
        f"Ressort: {candidate['ressort']}\n"
        f"Quelle: {item['source']} ({item['language']})\n"
        f"Titel: {item['title']}\n"
        f"Anriss: {item['summary']}\n\n"
        f"Artikeltext:\n{text or '(Nicht abrufbar. Nutze nur Titel und Anriss und halte dich kurz.)'}\n\n"
        "Schreib daraus die Nachricht für The Daily Good."
    )
    return user, len(text)


def build_story(candidate, story, story_id):
    story = no_dashes(story)
    kinds = set()
    blocks = []
    for block in story["blocks"]:
        if block["type"] in kinds:
            continue
        kinds.add(block["type"])
        blocks.append(block)
    sources = []
    for member in candidate["items"]:
        if len(sources) < 2 and all(s["name"] != member["source"] for s in sources):
            sources.append({"name": member["source"], "url": member["link"], "language": member["language"]})
    return {
        "id": story_id,
        "ressort": candidate["ressort"],
        "kicker": story["kicker"],
        "headline": story["headline"],
        "teaser": story["teaser"],
        "blocks": blocks[:3],
        "whyGood": story["whyGood"],
        "honestNote": story["honestNote"],
        "sources": sources,
    }


JOKES = STATE / "jokes.json"


def joke_job():
    history = json.loads(JOKES.read_text()) if JOKES.exists() else []
    avoid = "\n".join(f"- {setup}" for setup in history[-40:])
    user = "Wähle den Flachwitz für heute." + (f" Nicht diese, die hatten wir schon:\n{avoid}" if avoid else "")
    return (JOKE_SYSTEM, user, JOKE_SCHEMA, 4000, "medium")


def remember_joke(joke):
    history = json.loads(JOKES.read_text()) if JOKES.exists() else []
    STATE.mkdir(exist_ok=True)
    JOKES.write_text(json.dumps(history + [joke["setup"]], ensure_ascii=False, indent=1))


# MARK: Fotos von Wikimedia Commons

COMMONS = "https://commons.wikimedia.org/w/api.php"
FREE_LICENSES = ("cc0", "public domain", "cc by", "cc-by")
UNWANTED = ("skull", "skelet", "dead", "death", "corpse", "carcass", "blood", "taxiderm", "stuffed", "trophy",
            "crâne", "schädel", "map", "diagram", "logo", "chart", "drawing", "coat of arms", "flag")


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def find_image(query):
    """Freies Querformat-Foto zum Suchbegriff, zuerst unter den geprüften Qualitätsbildern."""
    for quality in (True, False):
        search = f"{query} filetype:bitmap" + (" incategory:Quality_images" if quality else "")
        params = {
            "action": "query", "format": "json", "generator": "search", "gsrsearch": search,
            "gsrnamespace": 6, "gsrlimit": 15, "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1280,
        }
        try:
            data = fetch_json(COMMONS + "?" + urllib.parse.urlencode(params))
        except Exception:  # Commons nicht erreichbar: dann eben ohne Foto
            continue
        pages = sorted(data.get("query", {}).get("pages", {}).values(), key=lambda page: page.get("index", 0))
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata", {})
            license_name = clean(meta.get("LicenseShortName", {}).get("value", ""), 40)
            width, height = info.get("width", 0), info.get("height", 1)
            if not info.get("thumburl") or info.get("mime") not in ("image/jpeg", "image/png"):
                continue
            if width < 1200 or not 1.2 <= width / height <= 2.2:
                continue
            if not license_name.lower().startswith(FREE_LICENSES):
                continue
            if any(word in page.get("title", "").lower() for word in UNWANTED):
                continue
            if license_name.lower().startswith(("public domain", "cc0")):
                credit = "gemeinfrei, Wikimedia Commons"
            else:
                artist = clean(meta.get("Artist", {}).get("value", ""), 40) or "unbekannt"
                credit = f"{artist}, {license_name}, Wikimedia Commons"
            url = urllib.parse.urlsplit(info["thumburl"])
            query = [(k, v) for k, v in urllib.parse.parse_qsl(url.query) if not k.startswith("utm_")]
            return {
                "url": urllib.parse.urlunsplit(url._replace(query=urllib.parse.urlencode(query))),
                "credit": credit,
                "source": info.get("descriptionurl"),
            }
    return None


def attach_images(client, stories):
    lines = [f"[{s['id']}] {s['headline']}. {s['teaser']}" for s in stories]
    result = ask(client, IMAGE_SYSTEM, "Die Nachrichten:\n\n" + "\n".join(lines), IMAGE_SCHEMA, max_tokens=4000, effort="low")
    queries = {q["storyID"]: q["query"] for q in result["queries"]}
    with ThreadPoolExecutor(6) as pool:
        images = list(pool.map(lambda story: find_image(queries[story["id"]]) if story["id"] in queries else None, stories))
    for story, image in zip(stories, images):
        story["image"] = image
    return sum(1 for image in images if image)


# MARK: Comic des Tages (Pepper&Carrot von David Revoy, CC BY 4.0)

PEPPER = "https://www.peppercarrot.com/0_sources"


def fetch_bytes(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def jpeg_size(data):
    """Breite und Höhe aus dem JPEG-Kopf, ohne Zusatzpaket."""
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF or data[i + 1] == 0xFF:
            i += 1
            continue
        if data[i + 1] in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None


def store_page(url, target):
    """Lädt eine Comicseite einmal herunter und legt sie bei GitHub Pages ab. Gibt Breite und Höhe zurück."""
    if not target.exists():
        data = fetch_bytes(url)
        try:  # Etwas stärker komprimieren, falls Pillow da ist. Die Seiten bleiben 1200 Pixel breit.
            from PIL import Image
            buffer = io.BytesIO()
            Image.open(io.BytesIO(data)).convert("RGB").save(buffer, "JPEG", quality=80, optimize=True, progressive=True)
            if buffer.tell() < len(data):
                data = buffer.getvalue()
        except ImportError:
            pass
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return jpeg_size(target.read_bytes())


def comic_of_the_day(today):
    """Jeden Tag eine ganze Folge. Jede Folge wird nur einmal gespeichert und danach wiederverwendet."""
    state_file = STATE / "comic.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    episodes = [e for e in fetch_json(PEPPER + "/episodes.json") if "de" in e["translated_languages"]]
    last = state.get("last") or {}
    if last.get("date") == today.isoformat():
        index = last["episode"]  # Zweiter Lauf am selben Tag: dieselbe Folge
    else:
        index = state.get("next", 0)
        if isinstance(index, list):  # Erste Version merkte sich [Folge, Seite]
            index = index[0]
    if index >= len(episodes):
        index = 0  # Alle Folgen durch: von vorn
    episode = episodes[index]
    number = int(episode["name"][2:4])
    source = f"{PEPPER}/{episode['name']}/low-res/de_Pepper-and-Carrot_by-David-Revoy_E{number:02d}P"
    pages = []
    for page in range(episode["total_pages"]):  # P00 ist das Titelbanner, danach die Seiten
        size = store_page(f"{source}{page:02d}.jpg", OUT / "comics" / episode["name"] / f"p{page:02d}.jpg")
        entry = {"url": f"{PAGES_URL}/comics/{episode['name']}/p{page:02d}.jpg"}
        if size:
            entry["width"], entry["height"] = size
        pages.append(entry)
    for old in (OUT / "comics").glob("*.jpg"):  # Einzelseiten aus der ersten Version
        old.unlink()
    STATE.mkdir(exist_ok=True)
    state_file.write_text(json.dumps({
        "next": index + 1,
        "last": {"date": today.isoformat(), "episode": index},
    }, indent=1))
    return {
        "title": f"Pepper&Carrot · Folge {number}",
        "image": pages[1]["url"],  # Für ältere App-Versionen, die nur eine Seite kennen
        "banner": pages[0],
        "pages": pages[1:],
        "credit": "David Revoy, peppercarrot.com, CC BY 4.0",
        "link": "https://www.peppercarrot.com/de/",
    }


# MARK: Ablauf

def main():
    parser = argparse.ArgumentParser(description="Erzeugt die Tagesausgabe von The Daily Good.")
    parser.add_argument("--hours", type=int, default=36, help="Wie weit zurück Meldungen gesammelt werden")
    parser.add_argument("--stories", type=int, default=12, help="Anzahl der Nachrichten in der Ausgabe")
    parser.add_argument("--dry-run", action="store_true", help="Nur auswählen, nichts schreiben")
    parser.add_argument("--no-batch", action="store_true", help="Einzeln statt per Batch fragen (schneller, doppelt so teuer)")
    args = parser.parse_args()

    client = anthropic.Anthropic(api_key=api_key())
    today = dt.date.today()

    print("1. Meldungen sammeln …")
    items = collect(args.hours)
    print(f"   {len(items)} Meldungen aus {len(FEEDS)} Quellen")

    print("2. Gute Nachrichten aussuchen …")
    try:
        candidates = triage(client, items, not args.no_batch)
    except anthropic.RateLimitError:
        sys.exit("   Zu viele Anfragen gerade. Bitte später noch einmal.")
    except anthropic.APIStatusError as error:
        sys.exit(f"   API-Fehler {error.status_code}: {error.message}")
    except anthropic.APIConnectionError:
        sys.exit("   Keine Verbindung zur API.")
    except (Skipped, json.JSONDecodeError) as reason:
        sys.exit(f"   Auswahl fehlgeschlagen: {reason}")
    chosen = select(candidates, args.stories)
    print(f"   {len(candidates)} Kandidaten, {len(chosen)} ausgewählt:")
    for candidate in chosen:
        print(f"   {candidate['score']:>2}  {candidate['ressort']:<12} {candidate['items'][0]['title'][:80]}")
    if args.dry_run:
        print(f"Kosten bisher: {USAGE.dollars:.2f} $")
        return

    print("3. Artikel lesen und schreiben …")
    with ThreadPoolExecutor(6) as pool:
        prompts = list(pool.map(story_prompt, chosen))
    jobs = {f"story-{n}": (WRITER_SYSTEM, user, STORY_SCHEMA, 8000, "medium") for n, (user, _) in enumerate(prompts, 1)}
    jobs["joke"] = joke_job()
    results = ask_all(client, jobs, not args.no_batch)

    written = []  # (Länge des Artikeltexts, Geschichte)
    for n, (candidate, (_, text_length)) in enumerate(zip(chosen, prompts), 1):
        result = results.get(f"story-{n}")
        try:
            if isinstance(result, Exception) or result is None:
                raise Skipped(str(result))
            written.append((text_length, build_story(candidate, result, f"{today.isoformat()}-{n:02d}")))
        except (Skipped, KeyError) as error:
            print(f"   übersprungen: {candidate['items'][0]['title'][:60]} ({error})")
    # Aufmacher wird die beste Geschichte, deren Artikel vollständig lesbar war (keine Paywall)
    lead = next((pair for pair in written if pair[0] >= 800), written[0] if written else None)
    stories = [story for _, story in ([lead] if lead else []) + [pair for pair in written if pair is not lead]]
    if not stories:
        sys.exit("   Keine Nachricht geschrieben, die Ausgabe bleibt wie sie ist.")

    joke = results.get("joke")
    if isinstance(joke, dict):
        joke = no_dashes(joke)
        remember_joke(joke)
    else:
        print(f"   Flachwitz übersprungen ({joke})")
        joke = None

    try:
        print(f"   Fotos gefunden: {attach_images(client, stories)} von {len(stories)}")
    except (Skipped, anthropic.APIError, json.JSONDecodeError, KeyError) as error:
        print(f"   Fotos übersprungen ({error})")

    comic = None
    try:
        comic = comic_of_the_day(today)
        print(f"   Comic: {comic['title']} mit {len(comic['pages'])} Seiten")
    except Exception as error:  # Ohne Comic erscheint die Ausgabe trotzdem
        print(f"   Comic übersprungen ({error})")

    edition = {
        "date": today.isoformat(),
        "number": (today - FIRST_EDITION).days + 1,
        "isSample": False,
        "leadStoryID": stories[0]["id"],
        "stories": stories,
        "joke": joke,
        "comic": comic,
    }
    (OUT / "editions").mkdir(parents=True, exist_ok=True)
    payload = json.dumps(edition, ensure_ascii=False, indent=2)
    (OUT / "editions" / f"{today.isoformat()}.json").write_text(payload)
    (OUT / "latest.json").write_text(payload)
    print(f"4. Fertig: Ausgabe Nr. {edition['number']} mit {len(stories)} Nachrichten")
    print(f"   Tokens: {USAGE.input:,} rein, {USAGE.output:,} raus. Kosten etwa {USAGE.dollars:.2f} $")


if __name__ == "__main__":
    main()
