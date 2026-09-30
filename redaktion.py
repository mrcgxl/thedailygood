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
import tempfile
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
    BRIEF_SCHEMA, BRIEF_SYSTEM, IMAGE_PICK_SCHEMA, IMAGE_PICK_SYSTEM, IMAGE_SCHEMA, IMAGE_SYSTEM, JOKE_SCHEMA,
    JOKE_SYSTEM, QUIZ_SCHEMA, QUIZ_SYSTEM, RECOMMENDATION_SCHEMA, RECOMMENDATION_SYSTEM, STORY_SCHEMA, TRIAGE_SCHEMA,
    TRIAGE_SYSTEM, WRITER_SYSTEM,
)
from sources import FEEDS, RECOMMENDATIONS  # noqa: E402

MODEL = "claude-opus-5-5"
# Mit einem Token von `claude setup-token` läuft alles über Mircos Claude-Abo statt über API-Guthaben
BACKEND = "abo" if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") else "api"
ABO_MODEL = os.environ.get("DG_ABO_MODEL", "sonnet")
ABO_TIMEOUT = 20 * 60  # Sekunden pro Anfrage
PRICE_INPUT = 4.0 / 1_000_000   # $ pro Token, Claude Opus 5.5
PRICE_OUTPUT = 20.0 / 1_000_000
FIRST_EDITION = dt.date(2026, 9, 29)
ITEMS_PER_FEED = 15
BATCH_WAIT = 90 * 60  # Sekunden, danach wird einzeln nachgefragt (am 30.09. brauchten Batches über 70 Minuten)
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

    def add_abo(self, result):
        """Verbrauch einer Anfrage über Claude Code. Die Dollar sind nur ein Vergleichswert, das Abo zahlt."""
        usage = result.get("usage") or {}
        self.input += sum(usage.get(key) or 0 for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        self.output += usage.get("output_tokens") or 0
        self.dollars += result.get("total_cost_usd") or 0.0


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


def ask_abo(system, user, schema, effort):
    """Eine Anfrage über Claude Code mit dem Abo: kein Werkzeug, kein Verlauf, festes JSON-Format."""
    env = {key: value for key, value in os.environ.items() if key != "ANTHROPIC_API_KEY"}  # sonst zahlt doch die API
    env["CLAUDE_CODE_OAUTH_TOKEN"] = "".join(env.get("CLAUDE_CODE_OAUTH_TOKEN", "").split())  # Umbrüche vom Kopieren
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    command = [
        "claude", "-p", "--output-format", "json", "--model", ABO_MODEL, "--tools", "",
        "--no-session-persistence", "--effort", effort, "--system-prompt", system,
        "--json-schema", json.dumps(schema, ensure_ascii=False),
    ]
    try:
        run = subprocess.run(command, input=user, capture_output=True, text=True, timeout=ABO_TIMEOUT,
                             env=env, cwd=tempfile.gettempdir())
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Skipped(f"Claude Code: {error}")
    try:
        result = json.loads(run.stdout)
    except json.JSONDecodeError:
        raise Skipped(f"Claude Code ohne Antwort: {(run.stderr or run.stdout).strip()[:200]}")
    USAGE.add_abo(result)
    if result.get("is_error"):
        raise Skipped(str(result.get("result"))[:200])
    if isinstance(result.get("structured_output"), dict):
        return result["structured_output"]
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (result.get("result") or "").strip())
    return json.loads(text)


def ask(client, system, user, schema, max_tokens=16000, effort="medium"):
    """Eine einzelne Anfrage mit garantiertem JSON. Lehnt Opus ab, springt serverseitig ein anderes Modell ein.
    Gestreamt, weil das SDK große Antworten ohne Streaming ablehnt (mögliche Dauer über 10 Minuten)."""
    if BACKEND == "abo":
        return ask_abo(system, user, schema, effort)
    with client.beta.messages.stream(
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        **params(system, user, schema, max_tokens, effort),
    ) as stream:
        response = stream.get_final_message()
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
    if use_batch and BACKEND == "api":
        try:
            results = run_batch(client, jobs)
        except anthropic.APIError as error:
            print(f"   Batch nicht möglich ({error}), frage einzeln nach")
    missing = [key for key in jobs if key not in results]

    def single(key):
        try:
            return ask(client, *jobs[key])
        except (Skipped, anthropic.APIError, json.JSONDecodeError, ValueError) as error:
            return error

    with ThreadPoolExecutor(3 if BACKEND == "abo" else 4) as pool:
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

def recent_headlines(days=3):
    """Schlagzeilen der letzten Ausgaben vor heute, damit nichts doppelt erscheint."""
    today = dt.date.today().isoformat()
    headlines = []
    for path in sorted(p for p in (OUT / "editions").glob("*.json") if p.stem < today)[-days:]:
        try:
            headlines += [story["headline"] for story in json.loads(path.read_text()).get("stories", [])]
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return headlines


def triage(client, items, use_batch):
    lines = [f"[{i['id']}] {i['source']} ({i['language']}): {i['title']}. {i['summary'][:160]}" for i in items]
    user = "Hier sind die Meldungen der letzten Stunden:\n\n" + "\n".join(lines)
    recent = recent_headlines()
    if recent:  # Gestern schon gebracht: nicht noch einmal, auch nicht aus einer anderen Quelle
        user += ("\n\nDiese Nachrichten standen in den letzten Tagen schon in der Zeitung. "
                 "Nimm sie nicht noch einmal auf, auch wenn eine andere Quelle darüber berichtet:\n"
                 + "\n".join(f"- {headline}" for headline in recent))
    result = ask_all(client, {"triage": (TRIAGE_SYSTEM, user, TRIAGE_SCHEMA, 32000, "medium")}, use_batch)["triage"]
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


def select(candidates, full, briefs, per_ressort=5):
    """Reihum durch die Ressorts: erst das Beste jedes Ressorts, dann das Zweitbeste und so weiter.
    Die besten werden ausführliche Geschichten, die nächsten Kurzmeldungen."""
    pool = sorted((c for c in candidates if c["score"] >= 5), key=lambda c: -c["score"])
    groups = {}
    for candidate in pool:
        groups.setdefault(candidate["ressort"], []).append(candidate)
    ranked = []
    for rank in range(per_ressort):
        ranked += sorted((group[rank] for group in groups.values() if rank < len(group)), key=lambda c: -c["score"])
    # Ausführlich erst ab 6 Punkten, Design und Architektur schon ab 5
    chosen = [c for c in ranked if c["score"] >= 6 or c["ressort"] in FAVORITES][:full]
    short = [c for c in ranked if c not in chosen][:briefs]

    chosen.sort(key=lambda c: -c["score"])
    lead = next((c for c in chosen if not c["heavy"]), chosen[0] if chosen else None)
    rest = [c for c in chosen if c is not lead]
    ordered = [lead] if lead else []
    while rest:  # Gleiche Ressorts nicht direkt hintereinander
        pick = next((c for c in rest if not ordered or c["ressort"] != ordered[-1]["ressort"]), rest[0])
        ordered.append(pick)
        rest.remove(pick)
    return ordered, short


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


def brief_prompt(candidate):
    item, text = best_text(candidate)
    user = (
        f"Ressort: {candidate['ressort']}\n"
        f"Quelle: {item['source']} ({item['language']})\n"
        f"Titel: {item['title']}\n"
        f"Anriss: {item['summary']}\n\n"
        f"Artikeltext:\n{text[:2500] or '(Nicht abrufbar. Nutze nur Titel und Anriss.)'}\n\n"
        "Schreib daraus die Kurzmeldung für The Daily Good."
    )
    return user


def sources_of(candidate):
    sources = []
    for member in candidate["items"]:
        if len(sources) < 2 and all(s["name"] != member["source"] for s in sources):
            sources.append({"name": member["source"], "url": member["link"], "language": member["language"]})
    return sources


def build_brief(candidate, brief, story_id):
    brief = no_dashes(brief)
    return {
        "id": story_id,
        "ressort": candidate["ressort"],
        "kicker": brief["kicker"],
        "headline": brief["headline"],
        "teaser": brief["teaser"],
        "blocks": [],
        "whyGood": brief["whyGood"],
        "honestNote": brief["honestNote"],
        "sources": sources_of(candidate),
        "brief": True,
        "score": candidate["score"],
    }


def build_story(candidate, story, story_id):
    story = no_dashes(story)
    kinds = set()
    blocks = []
    for block in story["blocks"]:
        if block["type"] in kinds:
            continue
        kinds.add(block["type"])
        blocks.append(block)
    return {
        "id": story_id,
        "ressort": candidate["ressort"],
        "kicker": story["kicker"],
        "headline": story["headline"],
        "teaser": story["teaser"],
        "blocks": blocks[:3],
        "whyGood": story["whyGood"],
        "honestNote": story["honestNote"],
        "sources": sources_of(candidate),
        "score": candidate["score"],
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


# MARK: Empfehlung des Tages

TIPS = STATE / "recommendations.json"
WIKIPEDIA = [("de", "https://de.wikipedia.org/w/api.php"), ("en", "https://en.wikipedia.org/w/api.php")]
TIP_HINTS = {
    "buch": " Am liebsten ein Buch, das es auf Deutsch gibt.",
    "film": " Nenne keinen Streamingdienst und kein Kino, wenn es nicht in der Meldung steht.",
    "serie": " Nenne keinen Streamingdienst, wenn er nicht in der Meldung steht.",
    "podcast": " Am liebsten ein deutschsprachiger Podcast mit eigenem Wikipedia-Artikel.",
    "spiel": " Am liebsten ein Spiel für mehrere Plattformen und für viele Altersgruppen.",
    "rezept": " Beim Rezept gilt: nur Rezepte aus den Meldungen, itemID ist Pflicht. Beschreib das Gericht, verrate aber nicht das ganze Rezept.",
    "album": " Am liebsten ein Album aus den letzten Monaten, das in den Kritiken gefeiert wird.",
}


def simplify(text):
    return re.sub(r"[^a-z0-9äöüß]+", " ", (text or "").lower()).strip()


def recommendation_job(today):
    """Anfrage für die Empfehlung des Tages. Gibt den Job, die Art und die Meldungen nach ID zurück."""
    kind, label, feeds = RECOMMENDATIONS[today.weekday()]
    since = time.time() - 14 * 24 * 3600
    with ThreadPoolExecutor(6) as pool:
        batches = list(pool.map(lambda feed: read_feed(feed, since), feeds))
    items = [item for batch in batches for item in batch]
    for number, item in enumerate(items, 1):
        item["id"] = f"t{number}"
    history = json.loads(TIPS.read_text()) if TIPS.exists() else []
    avoid = "; ".join(entry["title"] for entry in history[-80:])
    lines = [f"[{i['id']}] {i['source']}: {i['title']}. {i['summary'][:220]}" for i in items]
    user = ("Meldungen und Kritiken:\n\n" + "\n".join(lines)) if lines else "Heute gibt es kein Material."
    if avoid:
        user += f"\n\nDiese Titel hatten wir schon, bitte nicht wiederholen: {avoid}"
    system = RECOMMENDATION_SYSTEM.format(kind=label, extra=TIP_HINTS.get(kind, ""))
    return (system, user, RECOMMENDATION_SCHEMA, 6000, "medium"), kind, label, {i["id"]: i for i in items}


def wikipedia_page(title, creator, year):
    """Adresse des Wikipedia-Artikels, wenn Titel und Macher oder Jahr zum Werk passen, sonst None."""
    def core(text):  # ohne Untertitel und Klammerzusatz, „Gemischtes Hack (Podcast)“ wird zu „gemischtes hack“
        return simplify(re.split(r"[:(]", text or "")[0])

    wanted = core(title)
    first = re.split(r",| und | and | & |\(", creator or "")[0]  # „Martin Bourboulon (Regie)“ wird zu Bourboulon
    surname = simplify(first).split(" ")[-1] if simplify(first) else ""
    for lang, api in WIKIPEDIA:
        search = {"action": "query", "format": "json", "list": "search", "srsearch": f"{title} {creator}", "srlimit": 5}
        try:
            results = fetch_json(api + "?" + urllib.parse.urlencode(search))["query"]["search"]
        except Exception:
            continue
        for result in results:
            if not wanted or core(result["title"]) != wanted:  # „Hades II“ ist nicht „Hades“
                continue
            query = {"action": "query", "format": "json", "prop": "extracts", "exintro": 1, "explaintext": 1,
                     "titles": result["title"]}
            try:
                pages = fetch_json(api + "?" + urllib.parse.urlencode(query))["query"]["pages"]
                intro = simplify(" ".join(p.get("extract", "") for p in pages.values()))
            except Exception:
                continue
            if (surname and surname in intro) or (year and year in intro):
                return f"https://{lang}.wikipedia.org/wiki/" + urllib.parse.quote(result["title"].replace(" ", "_"))
    return None


def finish_recommendation(result, kind, label, items, today):
    """Nimmt den ersten Vorschlag, den Wikipedia oder eine echte Meldung belegt. Sonst keine Empfehlung."""
    if not isinstance(result, dict):
        return None
    for pick in no_dashes(result["picks"])[:3]:
        item = items.get(pick.get("itemID") or "")
        if kind == "rezept":
            link, proven = None, item is not None
        else:
            link = wikipedia_page(pick["title"], pick["creator"], pick.get("year"))
            mentioned = item is not None and simplify(pick["title"]) in simplify(item["title"] + " " + item["summary"])
            proven = link is not None or mentioned
        if not proven:
            print(f"   Empfehlung nicht belegt, verworfen: {pick['title']}")
            continue
        history = json.loads(TIPS.read_text()) if TIPS.exists() else []
        STATE.mkdir(exist_ok=True)
        TIPS.write_text(json.dumps(history + [{"date": today.isoformat(), "kind": kind, "title": pick["title"]}],
                                   ensure_ascii=False, indent=1))
        return {
            "kind": kind,
            "label": label,
            "title": pick["title"],
            "creator": pick["creator"],
            "year": pick.get("year"),
            "text": pick["text"],
            "forWhom": pick["forWhom"],
            "link": link,
            "source": {"name": item["source"], "url": item["link"]} if item else None,
        }
    return None


# MARK: Nachrichten-Quiz

def story_text(story):
    """Alles Lesbare einer Geschichte als ein Text, für das Quiz."""
    parts = [story["headline"], story["teaser"]]
    for block in story.get("blocks", []):
        for key in ("caption", "text", "title", "place"):
            if isinstance(block.get(key), str):
                parts.append(block[key])
        if block.get("type") == "bigNumber":
            parts.append(f"{block.get('value')} {block.get('unit') or ''}")
        for entry in block.get("items") or []:
            parts.append(entry if isinstance(entry, str) else entry.get("text", ""))
    return " ".join(part for part in parts if part)


def make_quiz(client, stories):
    """Drei Fragen mit je vier Antworten zu den ausführlichen Geschichten."""
    full = [s for s in stories if not s.get("brief")]
    if len(full) < 3:
        return None
    lines = [f"[{s['id']}] {story_text(s)[:900]}" for s in full]
    result = ask(client, QUIZ_SYSTEM, "Die Geschichten von heute:\n\n" + "\n\n".join(lines), QUIZ_SCHEMA,
                 max_tokens=4000, effort="low")
    ids = {s["id"] for s in full}
    questions = []
    for q in no_dashes(result["questions"]):
        options = [o.strip() for o in q["options"] if o.strip()]
        if q["storyID"] in ids and len(options) == 4 and 0 <= q["answer"] < 4 and len(set(options)) == 4:
            questions.append({"storyID": q["storyID"], "question": q["question"], "options": options, "answer": q["answer"]})
    return {"questions": questions[:3]} if len(questions) >= 2 else None


# MARK: Wochenrückblick

def weekly_review(today, stories):
    """Sonntags die fünf schönsten Geschichten der Woche, jede aus einem anderen Ressort."""
    if today.weekday() != 6:
        return None
    pool = []
    for back in range(7):
        day = today - dt.timedelta(days=back)
        if back == 0:
            day_stories, lead = stories, stories[0]["id"] if stories else None
        else:
            path = OUT / "editions" / f"{day.isoformat()}.json"
            if not path.exists():
                continue
            data = json.loads(path.read_text())
            day_stories, lead = data.get("stories", []), data.get("leadStoryID")
        for story in day_stories:
            if not story.get("brief"):  # Aufmacher zählen etwas mehr
                pool.append((story.get("score", 5) + (2 if story["id"] == lead else 0), story))
    pool.sort(key=lambda pair: -pair[0])
    picked, ressorts = [], set()
    for _, story in pool:
        if story["ressort"] not in ressorts:
            picked.append(story)
            ressorts.add(story["ressort"])
        if len(picked) == 5:
            break
    return {"title": "Die schönsten Nachrichten der Woche", "stories": picked} if len(picked) >= 3 else None


# MARK: Fotos von Wikimedia Commons

COMMONS = "https://commons.wikimedia.org/w/api.php"
FREE_LICENSES = ("cc0", "public domain", "cc by", "cc-by")
UNWANTED = ("skull", "skelet", "dead", "death", "corpse", "carcass", "blood", "taxiderm", "stuffed", "trophy",
            "crâne", "schädel", "map", "diagram", "logo", "chart", "drawing", "coat of arms", "flag")


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def image_options(query, limit=4):
    """Bis zu vier freie Querformat-Fotos zum Suchbegriff, zuerst aus den geprüften Qualitätsbildern."""
    found, seen = [], set()
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
            title = page.get("title", "")
            if title in seen or not info.get("thumburl") or info.get("mime") not in ("image/jpeg", "image/png"):
                continue
            if width < 1200 or not 1.2 <= width / height <= 2.2:
                continue
            if not license_name.lower().startswith(FREE_LICENSES):
                continue
            if any(word in title.lower() for word in UNWANTED):
                continue
            if license_name.lower().startswith(("public domain", "cc0")):
                credit = "gemeinfrei, Wikimedia Commons"
            else:
                artist = clean(meta.get("Artist", {}).get("value", ""), 40) or "unbekannt"
                credit = f"{artist}, {license_name}, Wikimedia Commons"
            url = urllib.parse.urlsplit(info["thumburl"])
            params_clean = [(k, v) for k, v in urllib.parse.parse_qsl(url.query) if not k.startswith("utm_")]
            seen.add(title)
            found.append({
                "url": urllib.parse.urlunsplit(url._replace(query=urllib.parse.urlencode(params_clean))),
                "credit": credit,
                "source": info.get("descriptionurl"),
                "title": title.removeprefix("File:").rsplit(".", 1)[0].replace("_", " "),
                "description": clean(meta.get("ImageDescription", {}).get("value", ""), 160),
            })
            if len(found) >= limit:
                return found
    return found


def attach_images(client, stories):
    """Claude nennt Suchwörter, Commons liefert Fotos, dann wählt Claude pro Nachricht das passende oder keins."""
    lines = [f"[{s['id']}] {s['headline']}. {s['teaser']}" for s in stories]
    result = ask(client, IMAGE_SYSTEM, "Die Nachrichten:\n\n" + "\n".join(lines), IMAGE_SCHEMA, max_tokens=6000, effort="low")
    queries = {q["storyID"]: q["query"] for q in result["queries"]}
    with ThreadPoolExecutor(6) as pool:
        options = list(pool.map(lambda story: image_options(queries[story["id"]]) if story["id"] in queries else [], stories))
    blocks = []
    for story, found in zip(stories, options):
        if found:
            choices = "\n".join(f"  {k}: {o['title']}. {o['description']}" for k, o in enumerate(found))
            blocks.append(f"[{story['id']}] {story['headline']}. {story['teaser']}\n{choices}")
    picks = {}
    if blocks:
        result = ask(client, IMAGE_PICK_SYSTEM, "\n\n".join(blocks), IMAGE_PICK_SCHEMA, max_tokens=6000, effort="low")
        picks = {p["storyID"]: p["choice"] for p in result["picks"]}
    count = 0
    for story, found in zip(stories, options):
        choice = picks.get(story["id"], -1)
        story["image"] = None
        if 0 <= choice < len(found):
            story["image"] = {key: found[choice][key] for key in ("url", "credit", "source")}
            count += 1
    return count


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
    parser.add_argument("--stories", type=int, default=12, help="Anzahl der ausführlichen Geschichten")
    parser.add_argument("--briefs", type=int, default=30, help="Anzahl der Kurzmeldungen")
    parser.add_argument("--per-ressort", type=int, default=5, help="Höchstens so viele Beiträge pro Ressort")
    parser.add_argument("--dry-run", action="store_true", help="Nur auswählen, nichts schreiben")
    parser.add_argument("--no-batch", action="store_true", help="Einzeln statt per Batch fragen (schneller, doppelt so teuer)")
    parser.add_argument("--skip-if-exists", action="store_true", help="Nichts tun, wenn es die heutige Ausgabe schon gibt")
    args = parser.parse_args()

    today = dt.date.today()
    if args.skip_if_exists and (OUT / "editions" / f"{today.isoformat()}.json").exists():
        print(f"Die Ausgabe vom {today.isoformat()} gibt es schon, nichts zu tun.")
        return
    client = anthropic.Anthropic(api_key=api_key()) if BACKEND == "api" else None
    print(f"Über {'das Claude-Abo (' + ABO_MODEL + ')' if BACKEND == 'abo' else 'die API (' + MODEL + ')'}")

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
    chosen, short = select(candidates, args.stories, args.briefs, args.per_ressort)
    print(f"   {len(candidates)} Kandidaten, {len(chosen)} ausführlich, {len(short)} kurz:")
    for candidate in chosen + short:
        kind = "kurz" if candidate in short else "lang"
        print(f"   {candidate['score']:>2}  {kind}  {candidate['ressort']:<12} {candidate['items'][0]['title'][:74]}")
    print("   pro Ressort:", dict(Counter(c["ressort"] for c in chosen + short).most_common()))
    if args.dry_run:
        print(f"Kosten bisher: {USAGE.dollars:.2f} $")
        return

    print("3. Artikel lesen und schreiben …")
    with ThreadPoolExecutor(6) as pool:
        prompts = list(pool.map(story_prompt, chosen))
        brief_prompts = list(pool.map(brief_prompt, short))
    jobs = {f"story-{n}": (WRITER_SYSTEM, user, STORY_SCHEMA, 8000, "medium") for n, (user, _) in enumerate(prompts, 1)}
    jobs.update({f"brief-{n}": (BRIEF_SYSTEM, user, BRIEF_SCHEMA, 3000, "low") for n, user in enumerate(brief_prompts, 1)})
    jobs["joke"] = joke_job()
    tip = None
    try:
        tip_job, tip_kind, tip_label, tip_items = recommendation_job(today)
        jobs["tipp"] = tip_job
        tip = (tip_kind, tip_label, tip_items)
    except Exception as error:  # Ohne Empfehlung erscheint die Ausgabe trotzdem
        print(f"   Empfehlung übersprungen ({error})")
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
    for n, candidate in enumerate(short, 1):
        result = results.get(f"brief-{n}")
        try:
            if isinstance(result, Exception) or result is None:
                raise Skipped(str(result))
            stories.append(build_brief(candidate, result, f"{today.isoformat()}-k{n:02d}"))
        except (Skipped, KeyError) as error:
            print(f"   Kurzmeldung übersprungen: {candidate['items'][0]['title'][:60]} ({error})")

    joke = results.get("joke")
    if isinstance(joke, dict):
        joke = no_dashes(joke)
        remember_joke(joke)
    else:
        print(f"   Flachwitz übersprungen ({joke})")
        joke = None

    try:
        full = [s for s in stories if not s.get("brief")]  # Kurzmeldungen zeigen die Illustration ihres Ressorts
        print(f"   Fotos gefunden: {attach_images(client, full)} von {len(full)}")
    except (Skipped, anthropic.APIError, json.JSONDecodeError, KeyError) as error:
        print(f"   Fotos übersprungen ({error})")

    recommendation = None
    if tip:
        try:
            recommendation = finish_recommendation(results.get("tipp"), *tip, today)
            print(f"   Empfehlung: {recommendation['label']}, {recommendation['title']}" if recommendation else "   Heute keine belegte Empfehlung")
        except Exception as error:
            print(f"   Empfehlung übersprungen ({error})")
    quiz = None
    try:
        quiz = make_quiz(client, stories)
        print(f"   Quiz mit {len(quiz['questions'])} Fragen" if quiz else "   Kein Quiz heute")
    except (Skipped, anthropic.APIError, json.JSONDecodeError, KeyError) as error:
        print(f"   Quiz übersprungen ({error})")

    weekly = weekly_review(today, stories)
    if weekly:
        print(f"   Wochenrückblick mit {len(weekly['stories'])} Geschichten")

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
        "recommendation": recommendation,
        "weekly": weekly,
        "quiz": quiz,
    }
    (OUT / "editions").mkdir(parents=True, exist_ok=True)
    payload = json.dumps(edition, ensure_ascii=False, indent=2)
    (OUT / "editions" / f"{today.isoformat()}.json").write_text(payload)
    (OUT / "latest.json").write_text(payload)
    briefs_written = sum(1 for s in stories if s.get("brief"))
    print(f"4. Fertig: Ausgabe Nr. {edition['number']} mit {len(stories) - briefs_written} Geschichten und {briefs_written} Kurzmeldungen")
    if BACKEND == "abo":
        print(f"   Tokens: {USAGE.input:,} rein, {USAGE.output:,} raus. Über das Abo, keine Extrakosten "
              f"(über die API wären es etwa {USAGE.dollars:.2f} $)")
    else:
        print(f"   Tokens: {USAGE.input:,} rein, {USAGE.output:,} raus. Kosten etwa {USAGE.dollars:.2f} $")


if __name__ == "__main__":
    main()
