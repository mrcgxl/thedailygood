"""Redaktions-Skript für The Daily Good.

Sammelt Meldungen aus RSS-Quellen, lässt Claude die guten Nachrichten aussuchen
und schreibt daraus die Tagesausgabe als JSON im Format der App.

Aufruf:  .venv/bin/python redaktion.py [--hours 36] [--stories 12] [--dry-run] [--no-batch]
Standard ist die Batch-API (halber Preis). Dauert ein Batch zu lange, wird einzeln nachgefragt.
API-Key: aus ANTHROPIC_API_KEY oder dem macOS-Schlüsselbund (Dienst TheDailyGood-Anthropic).
Eine Ausgabe wird nur geschrieben, wenn sie den Mindestumfang erreicht. Sonst endet das Skript mit Fehler,
damit ein späterer Termin es noch einmal versucht.
"""

import argparse
import calendar
import datetime as dt
import html
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

warnings.filterwarnings("ignore")
# Ein hängender Feed oder Server darf den Lauf nicht bis zum Job-Timeout blockieren.
# Gilt für alles ohne eigenes Timeout, vor allem feedparser.
socket.setdefaulttimeout(30)

import anthropic  # noqa: E402
import feedparser  # noqa: E402
import trafilatura  # noqa: E402

from prompts import (  # noqa: E402
    BRIEF_SCHEMA, BRIEF_SYSTEM, COMIC_MOODS, COMIC_SCHEMA, COMIC_SYSTEM, GUESS_FIX_SCHEMA, GUESS_FIX_SYSTEM, ICONS,
    IMAGE_PICK_SCHEMA, IMAGE_PICK_SYSTEM, IMAGE_SCHEMA, IMAGE_SYSTEM, JOKE_SCHEMA, JOKE_SYSTEM, QUIZ_SCHEMA,
    QUIZ_SYSTEM, RECIPE_SCHEMA, RECIPE_SYSTEM, RECOMMENDATION_SCHEMA, RECOMMENDATION_SYSTEM, REGION_TRIAGE_SCHEMA,
    REGION_TRIAGE_SYSTEM, REGIONAL_SCHEMA, STORY_SCHEMA, STRIP_CHECK_SCHEMA, STRIP_CHECK_SYSTEM, TRIAGE_SCHEMA,
    TRIAGE_SYSTEM, WRITER_SYSTEM,
)
from sources import (  # noqa: E402
    BUSCH, COMIC_FALLBACK, COMIC_PLAN, FEEDS, RECIPE_SOURCES, RECOMMENDATIONS, REGIONAL_FEEDS, REGIONS, SANDRA_UND_WOO,
    VATER_UND_SOHN,
)

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
ARTICLE_LIMIT = 8000  # so viele Zeichen Artikeltext bekommt eine große Geschichte höchstens
MIN_STORIES = 10  # Mindestumfang einer Ausgabe, wenn die Auswahl so viele hergibt
MIN_BRIEFS = 15
RETRY_LATER = False  # kommt in der Nacht noch ein Termin? Wird in main() aus --retry-later gesetzt
RETRY_PAUSE = 15  # Sekunden vor dem zweiten Versuch einer gescheiterten Anfrage
REPEAT_DAYS = 14  # so lange gilt eine Quelle oder ein Thema als schon gebracht
PHOTO_DAYS = 30  # so lange wird ein Foto nicht noch einmal verwendet
HEADLINE_EDITIONS = 7  # Schlagzeilen so vieler Ausgaben bekommt die Auswahl zum Vergleich
JOKES_FILE = HERE / "jokes.txt"
NOTES = []  # Auffälligkeiten des Laufs für den Kurzbericht


def note(text):
    """Meldet eine Auffälligkeit im Log und merkt sie für den Kurzbericht."""
    print(f"   {text}")
    NOTES.append(text)


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
    """Verbrauch des Laufs, nach Token-Arten getrennt: neue Eingabe, in den Cache geschrieben, aus dem Cache gelesen."""

    def __init__(self):
        self.fresh = 0
        self.cache_write = 0
        self.cache_read = 0
        self.output = 0
        self.calls = 0
        self.dollars = 0.0

    @property
    def input(self):
        return self.fresh + self.cache_write + self.cache_read

    def count(self, fresh, cache_write, cache_read, output):
        self.fresh += fresh or 0
        self.cache_write += cache_write or 0
        self.cache_read += cache_read or 0
        self.output += output or 0
        self.calls += 1

    def add(self, usage, factor=1.0):
        self.count(usage.input_tokens, getattr(usage, "cache_creation_input_tokens", 0),
                   getattr(usage, "cache_read_input_tokens", 0), usage.output_tokens)
        tokens_in = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) \
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        self.dollars += factor * (tokens_in * PRICE_INPUT + (usage.output_tokens or 0) * PRICE_OUTPUT)

    def add_abo(self, result):
        """Verbrauch einer Anfrage über Claude Code. Die Dollar sind nur ein Vergleichswert, das Abo zahlt."""
        usage = result.get("usage") or {}
        self.count(usage.get("input_tokens"), usage.get("cache_creation_input_tokens"),
                   usage.get("cache_read_input_tokens"), usage.get("output_tokens"))
        self.dollars += result.get("total_cost_usd") or 0.0

    def report(self):
        def number(value):
            return f"{value:,}".replace(",", ".")
        return (f"{number(self.input)} Eingabe (davon {number(self.fresh)} neu, {number(self.cache_write)} in den Cache "
                f"geschrieben, {number(self.cache_read)} aus dem Cache), {number(self.output)} Ausgabe "
                f"in {self.calls} Anfragen")


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


RETRYABLE = (Skipped, anthropic.APIError, json.JSONDecodeError, ValueError)


def ask_twice(client, system, user, schema, max_tokens=16000, effort="medium"):
    """Eine Anfrage mit einem zweiten Versuch nach kurzer Pause. Scheitert auch der, kommt dessen Fehler durch."""
    try:
        return ask(client, system, user, schema, max_tokens=max_tokens, effort=effort)
    except RETRYABLE as error:
        print(f"   Anfrage gescheitert ({str(error)[:120]}), zweiter Versuch")
    time.sleep(RETRY_PAUSE)
    return ask(client, system, user, schema, max_tokens=max_tokens, effort=effort)


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
            return ask_twice(client, *jobs[key])
        except RETRYABLE as error:
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


def feed_media(entry):
    """Bildadressen, die ein Feed-Eintrag mitbringt (media:content, media:thumbnail, enclosure), die größten zuerst."""
    def width(media):
        try:
            return int(media.get("width") or 0)
        except (TypeError, ValueError):
            return 0
    urls = []
    for media in sorted(entry.get("media_content") or [], key=lambda media: -width(media)):
        kind = (media.get("type") or "").lower()
        if media.get("url") and (media.get("medium") == "image" or kind.startswith("image/") or (not kind and not media.get("medium"))):
            urls.append(media["url"])
    urls += [thumb["url"] for thumb in sorted(entry.get("media_thumbnail") or [], key=lambda thumb: -width(thumb))
             if thumb.get("url")]
    urls += [enclosure["href"] for enclosure in entry.get("enclosures") or []
             if enclosure.get("href") and (enclosure.get("type") or "").lower().startswith("image/")]
    return list(dict.fromkeys(urls))[:3]


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
            "media": feed_media(entry),
        })
    items.sort(key=lambda item: -item["time"])
    return items[:ITEMS_PER_FEED]


RECIPE_LINK = re.compile(r"recipe|/rezepte/")
RECIPE_TITLE = re.compile(r"\brecipes?\b", re.I)


def is_recipe(item, recipe_links=()):
    """Rezepte haben eine eigene Rubrik und gehören nicht in die Nachrichten."""
    path = urllib.parse.urlsplit(item["link"]).path.lower()
    return bool(RECIPE_LINK.search(path) or RECIPE_TITLE.search(item["title"])) or item["link"] in recipe_links


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


# MARK: Wiederholungen
# Doppelte verhindert nicht mehr nur der Prompt: Quellen und Themen der letzten Ausgaben werden schon im Code aussortiert.

def url_keys(url):
    """Schlüssel, unter denen eine Adresse wiedererkannt wird: ohne www, Abfrage, Anker und Schrägstrich am Ende und
    ohne Versionsnummern wie bei MDR (-100, -102) oder hessenschau (-v1). Dazu Seite plus lange Artikelnummer,
    denn t3n vergibt für denselben Artikel neue Adressen."""
    if not url:
        return set()
    parts = urllib.parse.urlsplit(url.strip().lower())
    host = parts.netloc.rsplit("@", 1)[-1].split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    head, _, last = parts.path.rstrip("/").rpartition("/")
    last = re.sub(r"[-_]v?\d{1,4}(?=,|\.html?$)", "", last)
    keys = {f"{host}{head}/{last}"}
    numbers = [n for n in re.findall(r"\d{6,}", parts.path) if not re.fullmatch(r"20\d\d[01]\d[0-3]\d", n)]
    if numbers:
        keys.add(f"{host}#{max(numbers, key=len)}")
    return keys


STOPWORDS = frozenset("""
aber alle allen alles also andere anderen auch beim bleibt bleiben dabei damit dann dass dem denen dies diese diesem
diesen dieser dieses doch dort durch eine einem einen einer eines einmal erst erste ersten etwa fast gegen geht gibt
haben hier ihre ihren immer jahr jahre jahren jetzt kann kein keine mehr nach neue neuen neuer neues nicht noch nur
oder ohne schon sein seine sich sind soll sollen über unter vom von vor weil weiter welt wenn werden wieder will wird
zwei drei vier fünf zehn hundert tausend millionen milliarden euro prozent deutschland deutsche deutschen deutscher
helfen hilft zeigt zeigen macht machen bekommt bekommen erhält erhalten feiert feiern startet starten bringt bringen
sorgt sorgen steht stehen kommt kommen findet finden lässt junge jungen junger alte alten alter große großen kleine
kleinen gute guten
about after again against also among another around back because been before being best could does down each even
every first from gets have here into just know like made make makes many more most much news next only other over
people says since some still such take than that their them then there these they this those three through time
under until very were what when where which while will with without world would year years your
""".split())


def stem(word):
    for ending in ("ungen", "innen", "heit", "keit", "ern", "en", "er", "es", "e", "n", "s"):
        if len(word) - len(ending) >= 4 and word.endswith(ending):
            return word[:-len(ending)]
    return word


def title_tokens(text):
    """Die tragenden Wörter eines Titels: ohne Füllwörter und Jahreszahlen, grob auf den Wortstamm gekürzt.
    Zahlen bleiben ganz, „10.000“ wird zu „10000“."""
    tokens = set()
    for word in re.findall(r"\d+(?:[.,]\d+)*|[a-zäöüß]+", (text or "").lower()):
        if word[0].isdigit():
            number = re.sub(r"[.,]", "", word)
            if len(number) >= 2 and not (len(number) == 4 and 1900 <= int(number) <= 2100):
                tokens.add(number)
        elif len(word) >= 4 and word not in STOPWORDS:
            tokens.add(stem(word))
    return tokens


def same_topic(a, b):
    """Zwei Titel meinen dasselbe, wenn sie mindestens drei tragende Wörter teilen und die Hälfte des kürzeren.
    An den Ausgaben vom 29.9. bis 10.10. so eingestellt, dass Windrad, Comedy Wildlife Awards, Nobelpreis und
    Tonleitung erkannt werden, ähnliche, aber verschiedene Meldungen dagegen nicht."""
    shared = len(a & b)
    return shared >= 3 and shared >= 0.5 * min(len(a), len(b))


def all_stories(edition):
    stories = edition.get("stories") or []
    regional = edition.get("regional") or []
    return [s for s in stories + regional if isinstance(s, dict)] if isinstance(stories, list) and isinstance(regional, list) else []


class History:
    """Was in den Ausgaben vor heute schon stand: Quellen und Themen der letzten 14 Tage, Schlagzeilen der letzten
    sieben Ausgaben für die Auswahl und Fotos der letzten 30 Tage."""

    def __init__(self, today):
        first_photo = (today - dt.timedelta(days=PHOTO_DAYS)).isoformat()
        first_repeat = (today - dt.timedelta(days=REPEAT_DAYS)).isoformat()
        self.editions = []  # (Datum, Ausgabe), die neueste zuerst
        for path in sorted((OUT / "editions").glob("*.json"), reverse=True):
            if not first_photo <= path.stem < today.isoformat():
                continue
            try:
                edition = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(edition, dict):  # eine kaputte Datei darf den Lauf nicht stoppen
                self.editions.append((path.stem, edition))
        self.links, self.photos = set(), set()
        self.topics = []  # (Wörter, Schlagzeile, Regionalmeldung)
        for date, edition in self.editions:
            for story in all_stories(edition):
                image = story.get("image") if isinstance(story.get("image"), dict) else {}
                self.photos.add(image.get("url"))
                if image.get("kind") != "source":  # bei Symbolfotos die Seite auf Commons
                    self.photos.add(image.get("source"))
                if date < first_repeat:
                    continue
                for source in story.get("sources") or []:
                    if isinstance(source, dict):
                        self.links |= url_keys(source.get("url"))
                headline = story.get("headline") or ""
                self.topics.append((title_tokens(f"{headline} {story.get('kicker') or ''}"), headline,
                                    story.get("ressort") == "region"))
        self.photos.discard(None)

    def headlines(self, editions=HEADLINE_EDITIONS, regional_only=False):
        """Schlagzeilen der letzten Ausgaben, mit den Regionalmeldungen, ohne Dubletten."""
        found = [story.get("headline") for _, edition in self.editions[:editions] for story in all_stories(edition)
                 if not regional_only or story.get("ressort") == "region"]
        return list(dict.fromkeys(headline for headline in found if headline))

    def known_link(self, url):
        return bool(url_keys(url) & self.links)

    def known_topic(self, texts, with_regional=False):
        """Schlagzeile einer früheren Meldung zum selben Thema, sonst None. Der Hauptteil vergleicht nur mit dem
        Hauptteil, denn eine Regionalmeldung haben nur die Leser eines Landes gesehen."""
        tokens = [t for t in map(title_tokens, texts) if t]
        for old, headline, regional in self.topics:
            if (with_regional or not regional) and any(same_topic(new, old) for new in tokens):
                return headline
        return None


def drop_known(items, history, recipe_links=()):
    """Meldungen, deren Adresse in den letzten 14 Tagen schon Quelle war, und Rezepte fallen vor der Auswahl weg."""
    kept, known, recipes = [], 0, 0
    for item in items:
        if history.known_link(item["link"]):
            known += 1
        elif is_recipe(item, recipe_links):
            recipes += 1
        else:
            kept.append(item)
    return kept, known, recipes


def candidate_texts(candidate):
    return [item["title"] for item in candidate["items"]] + [candidate.get("reason") or ""]


def drop_repeats(candidates, history):
    """Kandidaten, deren Thema in den letzten 14 Tagen schon in der Zeitung stand, fallen weg. Meinen zwei
    Kandidaten von heute dasselbe, bleibt der mit mehr Punkten."""
    kept, seen = [], []
    for candidate in sorted(candidates, key=lambda c: -c["score"]):
        texts = candidate_texts(candidate)
        old = history.known_topic(texts)
        tokens = [t for t in map(title_tokens, texts) if t]
        twin = next((other for other, other_tokens in seen
                     if any(same_topic(a, b) for a in tokens for b in other_tokens)), None)
        if old or twin:
            reason = f"stand schon in der Zeitung: {old[:60]}" if old else f"doppelt mit: {twin['items'][0]['title'][:60]}"
            print(f"   aussortiert: {candidate['items'][0]['title'][:60]} ({reason})")
            continue
        seen.append((candidate, tokens))
        kept.append(candidate)
    return kept


# MARK: Auswahl

def triage(client, items, use_batch, headlines=()):
    lines = [f"[{i['id']}] {i['source']} ({i['language']}): {i['title']}. {i['summary'][:160]}" for i in items]
    user = "Hier sind die Meldungen der letzten Stunden:\n\n" + "\n".join(lines)
    if headlines:  # Schon gebracht: nicht noch einmal, auch nicht aus einer anderen Quelle
        user += ("\n\nDiese Nachrichten standen in den letzten Tagen schon in der Zeitung. "
                 "Nimm sie nicht noch einmal auf, auch wenn eine andere Quelle darüber berichtet:\n"
                 + "\n".join(f"- {headline}" for headline in headlines))
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
    # Ausführlich und als Kurzmeldung erst ab 6 Punkten, Design und Architektur schon ab 5.
    # Lieber 20 sehr gute Kurzmeldungen als 30 mit mittelmäßigen aufgefüllte.
    good = [c for c in ranked if c["score"] >= 6 or c["ressort"] in FAVORITES]
    chosen = good[:full]
    short = [c for c in good if c not in chosen][:briefs]

    chosen.sort(key=lambda c: -c["score"])
    lead = next((c for c in chosen if not c["heavy"]), chosen[0] if chosen else None)
    rest = [c for c in chosen if c is not lead]
    ordered = [lead] if lead else []
    while rest:  # Gleiche Ressorts nicht direkt hintereinander
        pick = next((c for c in rest if not ordered or c["ressort"] != ordered[-1]["ressort"]), rest[0])
        ordered.append(pick)
        rest.remove(pick)
    return ordered, short


# MARK: Region

TAGESSCHAU_REGION = "https://www.tagesschau.de/api2u/news/?regions={}"
REGION_ITEMS = 25  # so viele neue Meldungen pro Bundesland bekommt die Auswahl höchstens
BROADCASTERS = {"wdr.de": "WDR", "ndr.de": "NDR", "swr.de": "SWR", "mdr.de": "MDR", "hessenschau.de": "hessenschau",
                "rbb24.de": "rbb24", "sr.de": "SR", "br.de": "BR24", "butenunbinnen.de": "buten un binnen",
                "radiobremen.de": "Radio Bremen"}


def broadcaster(link):
    host = urllib.parse.urlparse(link).hostname or ""
    return next((name for domain, name in BROADCASTERS.items() if host == domain or host.endswith("." + domain)),
                "tagesschau.de")


def teaser_image(news):
    """Größtes 16:9-Vorschaubild einer Meldung aus der tagesschau-Schnittstelle, höchstens 1920 Pixel breit."""
    variants = (news.get("teaserImage") or {}).get("imageVariants") or {}
    sizes = sorted(((int(match.group(1)), url) for key, url in variants.items()
                    for match in [re.fullmatch(r"16x9-(\d+)", key)] if match and url and int(match.group(1)) <= 1920),
                   reverse=True)
    return [url for _, url in sizes[:1]]


def tagesschau_region(code, since):
    """Regionalmeldungen eines Bundeslands von tagesschau.de. Den Volltext gibt es später über details."""
    code_of = {number: key for key, (_, number) in REGIONS.items()}
    try:
        data = fetch_json(TAGESSCHAU_REGION.format(REGIONS[code][1]))
    except Exception as error:  # Eine Region darf die Ausgabe nicht stoppen
        print(f"  Region übersprungen: {code} ({error})")
        return []
    items = []
    for news in data.get("news") or []:
        link = news.get("shareURL") or news.get("detailsweb")
        title = clean(news.get("title"), 200)
        try:
            stamp = dt.datetime.fromisoformat(news["date"]).timestamp()
        except (KeyError, TypeError, ValueError):
            continue
        if stamp < since or not link or not title or news.get("type") != "story":
            continue
        items.append({
            "source": broadcaster(link),
            "language": "Deutsch",
            "title": title,
            "summary": clean(news.get("firstSentence"), 280),
            "link": link,
            "details": news.get("details"),
            "regions": [code_of[n] for n in news.get("regionIds") or [] if n in code_of] or [code],
            "time": stamp,
            "media": teaser_image(news),
        })
    return items


def regional_items(hours, history, main_texts=()):
    """Meldungen aus allen 16 Bundesländern: die Regionalseiten von tagesschau.de, dazu eigene Feeds,
    wo dort zu wenig kommt. Was in den letzten 14 Tagen schon in der Zeitung stand oder heute im Hauptteil steht,
    fällt weg."""
    since = time.time() - hours * 3600
    main_tokens = [t for t in map(title_tokens, main_texts) if t]
    feeds = [(code, feed) for code, region_feeds in REGIONAL_FEEDS.items() for feed in region_feeds]
    with ThreadPoolExecutor(8) as pool:
        batches = list(pool.map(lambda code: tagesschau_region(code, since), REGIONS))
        batches += list(pool.map(lambda pair: [dict(item, regions=[pair[0]]) for item in read_feed(pair[1], since)], feeds))
    by_link, seen_titles, dropped = {}, set(), Counter()
    for item in (item for batch in batches for item in batch):
        if history.known_link(item["link"]):
            dropped["schon Quelle"] += 1
            continue
        if item["link"] in by_link:  # Dieselbe Meldung für mehrere Länder
            known = by_link[item["link"]]
            known["regions"] = sorted(set(known["regions"]) | set(item["regions"]))
            continue
        key = item["title"].lower()[:80]
        if key in seen_titles:
            continue
        seen_titles.add(key)
        tokens = title_tokens(item["title"])
        if history.known_topic([item["title"]], with_regional=True):
            dropped["Thema schon gebracht"] += 1
            continue
        if any(same_topic(tokens, other) for other in main_tokens):
            dropped["steht heute im Hauptteil"] += 1
            continue
        by_link[item["link"]] = item
    if dropped:
        print("   Regional aussortiert:", dict(dropped))
    chosen = {}
    for code in REGIONS:  # Pro Land nur die neuesten, damit die Auswahl überschaubar bleibt
        own = sorted((item for item in by_link.values() if code in item["regions"]), key=lambda item: -item["time"])
        for item in own[:REGION_ITEMS]:
            chosen[item["link"]] = item
    items = list(chosen.values())
    for number, item in enumerate(items, 1):
        item["id"] = f"r{number}"
    return items


def regional_triage(client, items, per_region, headlines=(), main_titles=()):
    """Pro Bundesland bis zu per_region gute Nachrichten, die besten zuerst."""
    lines = []
    for code, (name, _) in REGIONS.items():
        own = [item for item in items if code in item["regions"]]
        if own:
            lines.append(f"\n{name} ({code}):")
            lines += [f"[{item['id']}] {item['title']}. {item['summary'][:150]}" for item in own]
    user = "Hier sind die Meldungen der Landessender aus den letzten Stunden:\n" + "\n".join(lines)
    if main_titles:
        user += "\n\nDiese Themen stehen heute schon im Hauptteil:\n" + "\n".join(f"- {title}" for title in main_titles)
    if headlines:
        user += ("\n\nDiese Meldungen standen in den letzten Tagen schon in der Zeitung:\n"
                 + "\n".join(f"- {headline}" for headline in headlines))
    result = ask_all(client, {"region": (REGION_TRIAGE_SYSTEM, user, REGION_TRIAGE_SCHEMA, 8000, "medium")}, False)["region"]
    if isinstance(result, Exception):
        raise result
    by_id = {item["id"]: item for item in items}
    chosen, count = {}, Counter()
    for pick in sorted(result["picks"], key=lambda pick: -pick["score"]):
        item, code = by_id.get(pick["item_id"]), pick["region"]
        if not item or code not in item["regions"] or pick["score"] < 5 or count[code] >= per_region:
            continue
        entry = chosen.setdefault(item["id"], {"item": item, "regions": [], "score": pick["score"]})
        if code not in entry["regions"]:
            entry["regions"].append(code)
            count[code] += 1
    return list(chosen.values())


def regional_text(item):
    """Volltext: bei tagesschau.de aus der Schnittstelle, sonst von der Seite des Senders."""
    if item.get("details"):
        try:
            content = fetch_json(item["details"]).get("content") or []
            text = " ".join(clean(block.get("value"), 3000) for block in content
                            if block.get("type") in ("text", "headline") and block.get("value"))
            if len(text) >= 300:
                return text
        except Exception:  # Dann eben von der Webseite
            pass
    return fetch_article(item["link"])


def regional_prompt(entry):
    item = entry["item"]
    names = " und ".join(REGIONS[code][0] for code in entry["regions"])
    text = regional_text(item)
    return (
        f"Region laut Feed: {names}\n"
        f"Quelle: {item['source']}\n"
        f"Titel: {item['title']}\n"
        f"Anriss: {item['summary']}\n\n"
        f"Artikeltext:\n{text[:2500] or '(Nicht abrufbar. Nutze nur Titel und Anriss.)'}\n\n"
        "Schreib daraus die Kurzmeldung für die Rubrik „Aus deiner Region“. "
        "land ist das Kürzel des Bundeslands, in dem das Ereignis tatsächlich stattfindet: "
        + ", ".join(f"{code} {name}" for code, (name, _) in REGIONS.items()) + ". "
        "Prüf den Ort im Text und übernimm nicht einfach die Region aus dem Feed. "
        "Spielt die Nachricht außerhalb Deutschlands oder lässt sich der Ort nicht bestimmen, ist land null. "
        "kicker ist der Ort in diesem Bundesland, an dem die Nachricht spielt, zum Beispiel die Stadt oder der Landkreis."
    )


def build_regional(entry, brief, story_id):
    """Regionalmeldung, nur wenn sie wirklich im Land spielt. Der Feed von BR24 Oberbayern bringt zum Beispiel
    auch Meldungen aus Bielefeld oder Frankfurt."""
    brief = no_dashes(brief)
    item = entry["item"]
    regions = entry["regions"]
    if "land" in brief:  # fehlt nur, wenn das Modell das Schema nicht eingehalten hat
        if brief["land"] not in regions:
            raise Skipped(f"spielt in {brief['land'] or 'keinem Bundesland'}, nicht in {', '.join(regions)}")
        regions = [brief["land"]]
    return {
        "id": story_id,
        "ressort": "region",
        "regions": regions,
        "kicker": brief["kicker"],
        "headline": brief["headline"],
        "teaser": brief["teaser"],
        "blocks": [],
        "whyGood": brief["whyGood"],
        "honestNote": honest_note(brief.get("honestNote"), [item["source"]]),
        "sources": [{"name": item["source"], "url": item["link"], "language": "Deutsch"}],
        "brief": True,
        "score": entry["score"],
        "country": "DE",
        "terms": terms_of(brief, 1),
        **gallery_of(brief, "region", [item["title"], brief["headline"]]),
    }


# MARK: Rezept

RECIPES = STATE / "recipes.json"
BROWSER = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 TheDailyGood/0.1"


def find_recipe(node):
    """Sucht in den strukturierten Daten einer Seite (schema.org) das Rezept."""
    if isinstance(node, list):
        return next((found for found in map(find_recipe, node) if found), None)
    if isinstance(node, dict):
        kind = node.get("@type")
        if kind == "Recipe" or (isinstance(kind, list) and "Recipe" in kind):
            return node
        for key in ("@graph", "mainEntity"):
            if key in node:
                found = find_recipe(node[key])
                if found:
                    return found
    return None


def instructions_of(node):
    if isinstance(node, str):
        return [clean(node, 600)]
    if isinstance(node, list):
        return [step for entry in node for step in instructions_of(entry)]
    if isinstance(node, dict):
        if "itemListElement" in node:
            return instructions_of(node["itemListElement"])
        return [clean(node.get("text") or node.get("name") or "", 600)]
    return []


def recipe_material(item):
    """Zutaten und Zubereitung einer Rezeptseite als Text, sonst None."""
    try:
        request = urllib.request.Request(item["link"], headers={"User-Agent": BROWSER})
        with urllib.request.urlopen(request, timeout=15) as response:
            page = response.read().decode("utf-8", "ignore")
    except Exception:
        return None
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        try:
            recipe = find_recipe(json.loads(block.strip()))
        except (json.JSONDecodeError, ValueError):
            continue
        if recipe and len(recipe.get("recipeIngredient") or []) >= 3:
            steps = [step for step in instructions_of(recipe.get("recipeInstructions")) if step]
            if len(steps) < 2:
                continue
            return (f"Name: {clean(recipe.get('name') or item['title'], 200)}\n"
                    f"Portionen: {recipe.get('recipeYield')}\nZeit: {recipe.get('totalTime') or recipe.get('cookTime')}\n"
                    "Zutaten:\n" + "\n".join(f"- {clean(i, 200)}" for i in recipe["recipeIngredient"][:30]) +
                    "\nZubereitung:\n" + "\n".join(f"{n}. {s}" for n, s in enumerate(steps[:12], 1)))[:3500]
    # Ohne strukturierte Daten: Artikeltext, wenn er nach Rezept aussieht
    text = trafilatura.extract(page, include_comments=False, include_tables=True) or ""
    if re.search(r"\bZutaten\b|\bIngredients\b", text) and re.search(r"\d+\s?(g|ml|EL|TL|cups?|tbsp|tsp)\b", text):
        return f"Name: {item['title']}\n{text[:3500]}"
    return None


def recipe_job():
    """Anfrage für das Rezept des Tages. Gibt den Job und die Rezepte zurück, oder None."""
    since = time.time() - 7 * 24 * 3600
    with ThreadPoolExecutor(4) as pool:
        batches = list(pool.map(lambda feed: read_feed(feed, since), RECIPE_SOURCES))
    # Abwechselnd aus den Quellen, damit nicht immer dieselbe Seite gewinnt
    items = [batch[i] for i in range(max(map(len, batches), default=0)) for batch in batches if i < len(batch)]
    history = json.loads(RECIPES.read_text()) if RECIPES.exists() else []
    used = {entry.get("url") for entry in history}
    items = [item for item in items if item["link"] not in used][:14]
    with ThreadPoolExecutor(6) as pool:
        materials = list(pool.map(recipe_material, items))
    found = [(item, material) for item, material in zip(items, materials) if material][:5]
    if not found:
        return None
    user = "\n\n".join(f"Rezept {n} ({item['source']}):\n{material}" for n, (item, material) in enumerate(found))
    if history:
        user += "\n\nDiese Gerichte gab es schon: " + "; ".join(entry["title"] for entry in history[-40:])
    return (RECIPE_SYSTEM, user, RECIPE_SCHEMA, 6000, "medium"), [item for item, _ in found]


def recipe_links(items=()):
    """Adressen der Rezepte, die heute zur Wahl stehen oder schon erschienen sind. Sie kommen nicht als Nachricht."""
    try:
        history = json.loads(RECIPES.read_text()) if RECIPES.exists() else []
    except (OSError, ValueError):
        history = []
    links = {entry.get("url") for entry in history if isinstance(entry, dict)} if isinstance(history, list) else set()
    return (links | {item["link"] for item in items or []}) - {None}


def finish_recipe(result, items):
    if not isinstance(result, dict) or not 0 <= result.get("choice", -1) < len(items):
        return None
    result = no_dashes(result)
    ingredients = [entry for entry in result.get("ingredients") or [] if (entry.get("item") or "").strip()]
    steps = [step.strip() for step in result.get("steps") or [] if step.strip()]
    if len(ingredients) < 3 or len(steps) < 2:
        return None
    item = items[result["choice"]]
    recipe = {
        "title": result["title"], "intro": result["intro"], "servings": max(1, int(result.get("servings") or 2)),
        "minutes": max(5, int(result.get("minutes") or 30)), "difficulty": result.get("difficulty") or "einfach",
        "ingredients": ingredients, "steps": steps[:8], "tip": result.get("tip"),
        "icon": result.get("icon") if result.get("icon") in ICONS else "fork.knife",
        "source": {"name": item["source"], "url": item["link"]},
    }
    history = json.loads(RECIPES.read_text()) if RECIPES.exists() else []
    history.append({"title": recipe["title"], "url": item["link"], "date": dt.date.today().isoformat()})
    STATE.mkdir(exist_ok=True)
    RECIPES.write_text(json.dumps(history[-200:], ensure_ascii=False, indent=1))
    return recipe


# MARK: Schreiben

PAGE_IMAGES = {}  # Artikeladresse: Vorschaubilder, die der Verlag im HTML angibt (og:image, twitter:image)


def fetch_article(url):
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ""
        text = (trafilatura.extract(downloaded, include_comments=False, include_tables=False) or "")[:ARTICLE_LIMIT]
    except Exception:  # Paywalls und Zeitüberschreitungen sind normal
        return ""
    try:
        PAGE_IMAGES[url] = page_images(downloaded, url)
    except Exception:  # Ein seltsamer Seitenkopf kostet nur das Foto, nie den Text
        pass
    return text


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


def country_of(result):
    """ISO-Code des Landes, nur wenn er wie einer aussieht."""
    code = (result.get("country") or "").strip().upper()
    return code if re.fullmatch(r"[A-Z]{2}", code) else None


def terms_of(result, limit):
    """Erklärte Begriffe: kurz, ohne Dubletten und nur mit beiden Teilen."""
    terms, seen = [], set()
    for entry in result.get("terms") or []:
        term, text = (entry.get("term") or "").strip(), (entry.get("text") or "").strip()
        if not term or not text or len(term) > 40 or len(text) > 320 or term.lower() in seen:
            continue
        seen.add(term.lower())
        terms.append({"term": term, "text": text})
    return terms[:limit]


def guess_of(story):
    """Tippfrage vor dem Lesen, nur wenn sie vollständig ist."""
    guess = story.get("guess") or {}
    options = [str(option).strip() for option in guess.get("options") or [] if str(option).strip()]
    answer = guess.get("answer")
    if len(options) != 3 or not isinstance(answer, int) or not 0 <= answer <= 2 or not guess.get("question"):
        return None
    return {
        "question": guess["question"].strip(),
        "options": options,
        "answer": answer,
        "reveal": (guess.get("reveal") or "").strip(),
        "icon": guess.get("icon") if guess.get("icon") in ICONS else None,
    }


COMMON_WORDS = {"einen", "einem", "einer", "eines", "nicht", "durch", "haben", "wurde", "wurden", "werden", "seine",
                "seinen", "seiner", "ihren", "ihrer", "immer", "schon", "unter", "nach", "jeder", "jedes", "allen", "etwas"}


def gives_away(guess, story):
    """Verrät die Titelseite die Antwort schon? Gibt das verräterische Wort zurück, sonst None.
    Geprüft werden Zahlen und längere Wörter, die nur in der richtigen Antwort stehen."""
    def words(text):
        return {w for w in re.findall(r"\d+(?:[.,]\d+)*|[a-zäöüß]{5,}", text.lower()) if w not in COMMON_WORDS}
    seen = f"{story.get('headline', '')} {story.get('teaser', '')}".lower()
    others = words(guess["question"])
    for index, option in enumerate(guess["options"]):
        if index != guess["answer"]:
            others |= words(option)
    for word in words(guess["options"][guess["answer"]]) - others:
        if word[0].isdigit():
            if re.search(rf"(?<![\d.,]){re.escape(word)}(?![\d]|[.,]\d)", seen):
                return word
        elif word in seen:  # auch in Zusammensetzungen wie „Fischpassage“
            return word
    return None


def fix_guesses(client, stories):
    """Schreibt Tippfragen neu, deren Antwort schon auf der Titelseite steht. Was nicht klappt, fällt weg."""
    spoiled = [s for s in stories if s.get("guess") and gives_away(s["guess"], s)]
    if not spoiled:
        return 0, 0
    lines = [f"[{s['id']}] Überschrift: {s['headline']}\nVorspann: {s['teaser']}\n"
             f"Bisherige Frage: {s['guess']['question']}\nText: {story_text(s)[:1500]}" for s in spoiled]
    try:
        result = ask_twice(client, GUESS_FIX_SYSTEM.format(icons=", ".join(ICONS)), "\n\n".join(lines),
                           GUESS_FIX_SCHEMA, max_tokens=4000, effort="low")
        fresh = {item["storyID"]: guess_of({"guess": item}) for item in no_dashes(result["guesses"])}
    except (Skipped, anthropic.APIError, json.JSONDecodeError, KeyError) as error:
        print(f"   Tippfragen nicht überarbeitet ({error})")
        fresh = {}
    fixed = 0
    for story in spoiled:
        guess = fresh.get(story["id"])
        if guess and not gives_away(guess, story):
            story["guess"] = guess
            fixed += 1
        else:
            story["guess"] = None
    return fixed, len(spoiled)


# Wörter, mit denen über das Material gesprochen wird. „Artikel 12“ eines Vertrags ist dagegen Sache.
SOURCE_TALK = re.compile(r"\b(Artikel|Bericht|Reportage|Rezension|Kritik|Kritiker|Kritikerin|Autor|Autorin|Redaktion|Quelle)"
                         r"(s|n|en|in|innen)?\b(?!\s*\d)")


def honest_note(text, source_names=()):
    """Die ehrliche Einordnung nur, wenn sie über die Sache spricht. Sätze über die Quelle, den Artikel oder
    Kritiker fallen weg, denn die Leser wollen wissen, wie gut die Nachricht ist, nicht wie gut das Material war."""
    text = text.strip() if isinstance(text, str) else ""
    if not text or SOURCE_TALK.search(text):
        return None
    if any(re.search(rf"\b{re.escape(name)}\b", text) for name in source_names if len(name) >= 3):
        return None
    return text


GALLERY_RESSORTS = {"fotografie", "architektur", "design", "dekoration", "kultur"}
GALLERY_WORDS = re.compile(r"foto|photo|bilder|pictures|galerie|gallery|ausstellung|exhibition|wettbewerb|contest|"
                           r"award|preis|kunst|museum", re.I)


def gallery_of(result, ressort, texts):
    """{"gallery": True}, wenn die Bilder selbst die Nachricht sind, sonst nichts. Das Modell entscheidet,
    fehlt seine Angabe, entscheiden Ressort und Stichwörter."""
    value = result.get("gallery")
    if not isinstance(value, bool):
        value = ressort == "fotografie" or (ressort in GALLERY_RESSORTS and bool(GALLERY_WORDS.search(" ".join(texts))))
    return {"gallery": True} if value else {}


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
        "honestNote": honest_note(brief.get("honestNote"), [item["source"] for item in candidate["items"]]),
        "sources": sources_of(candidate),
        "brief": True,
        "score": candidate["score"],
        "country": country_of(brief),
        "terms": terms_of(brief, 1),
        **gallery_of(brief, candidate["ressort"], candidate_texts(candidate) + [brief["headline"]]),
    }


def clean_block(block):
    """Symbole nur aus der Liste, Zahlen in sinnvollen Grenzen. Was nicht passt, fällt weg."""
    for key in ("icon",):
        if key in block and block[key] not in ICONS:
            block[key] = None
    if "icons" in block:
        block["icons"] = [icon if icon in ICONS else None for icon in (block.get("icons") or [])]
    if block["type"] == "count" and not (2 <= int(block.get("value") or 0) <= 1000):
        return None
    if block["type"] == "share" and not (1 <= float(block.get("value") or 0) <= 99):
        return None
    return block


def build_story(candidate, story, story_id):
    story = no_dashes(story)
    kinds = set()
    blocks = []
    for block in story["blocks"]:
        if block["type"] in kinds:
            continue
        block = clean_block(block)
        if block is None:
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
        "honestNote": honest_note(story.get("honestNote"), [item["source"] for item in candidate["items"]]),
        "sources": sources_of(candidate),
        "score": candidate["score"],
        "country": country_of(story),
        "terms": terms_of(story, 2),
        "guess": guess_of(story),
        **gallery_of(story, candidate["ressort"], candidate_texts(candidate) + [story["headline"]]),
    }


JOKES = STATE / "jokes.json"  # Fragen der schon erschienenen Witze, der neueste zuletzt


def joke_history():
    try:
        history = json.loads(JOKES.read_text()) if JOKES.exists() else []
    except (OSError, ValueError):
        return []
    return [entry for entry in history if isinstance(entry, str)] if isinstance(history, list) else []


def joke_key(text):
    return re.sub(r"[^a-zäöüß0-9]+", " ", (text or "").lower()).strip()


def curated_jokes():
    """Die Witze aus jokes.txt in der Reihenfolge der Datei. Zeilen ohne genau einen senkrechten Strich fallen weg."""
    try:
        lines = JOKES_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    jokes = []
    for line in lines:
        if line.lstrip().startswith("#") or line.count("|") != 1:
            continue
        setup, punchline = (part.strip() for part in line.split("|"))
        if setup and punchline:
            jokes.append(no_dashes({"setup": setup, "punchline": punchline}))
    return jokes


def joke_of_the_day(today, history_editions=()):
    """Flachwitz aus der kuratierten Liste: zuerst der erste, der noch nie dran war, danach der, der am längsten
    zurückliegt. Läuft die Redaktion am selben Tag noch einmal, bleibt es beim Witz der Ausgabe. None, wenn die
    Liste fehlt."""
    try:
        joke = json.loads((OUT / "editions" / f"{today.isoformat()}.json").read_text()).get("joke")
        if isinstance(joke, dict) and joke.get("setup") and joke.get("punchline"):
            return {"setup": joke["setup"], "punchline": joke["punchline"]}
    except (OSError, ValueError, AttributeError):
        pass
    jokes = curated_jokes()
    if not jokes:
        return None
    last_used = {joke_key(setup): position for position, setup in enumerate(joke_history())}
    # Witze, die das Modell früher anders formuliert hat, erkennt man an der Pointe
    old_punchlines = {joke_key(edition.get("joke", {}).get("punchline")) for _, edition in history_editions
                      if isinstance(edition.get("joke"), dict)}

    def recency(joke):
        if joke_key(joke["setup"]) in last_used:
            return last_used[joke_key(joke["setup"])]
        return -0.5 if joke_key(joke["punchline"]) in old_punchlines else -1

    return min(jokes, key=recency)


def joke_job():
    """Nur noch Rückfall, wenn jokes.txt fehlt: das Modell wählt einen bekannten Witz."""
    avoid = "\n".join(f"- {setup}" for setup in joke_history()[-40:])
    user = "Wähle den Flachwitz für heute." + (f" Nicht diese, die hatten wir schon:\n{avoid}" if avoid else "")
    return (JOKE_SYSTEM, user, JOKE_SCHEMA, 4000, "medium")


def remember_joke(joke):
    history = joke_history()
    if history and joke_key(history[-1]) == joke_key(joke["setup"]):
        return  # zweiter Lauf am selben Tag
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
    "doku": " Eine Dokumentation, die staunen lässt oder Mut macht, zum Beispiel über Natur, Wissenschaft oder besondere Menschen. Nenne keinen Sender und keinen Streamingdienst, wenn er nicht in der Meldung steht.",
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
        if block.get("type") == "share":
            parts.append(f"{block.get('value')} Prozent: {block.get('label') or ''}")
        if block.get("type") == "count":
            parts.append(str(block.get("value")))
        for entry in block.get("items") or []:
            parts.append(entry if isinstance(entry, str) else entry.get("text", ""))
    return " ".join(part for part in parts if part)


def make_quiz(client, stories):
    """Drei Fragen mit je vier Antworten zu den ausführlichen Geschichten."""
    full = [s for s in stories if not s.get("brief")]
    if len(full) < 3:
        return None
    lines = [f"[{s['id']}] {story_text(s)[:900]}" + (f"\nTippfrage: {s['guess']['question']}" if s.get("guess") else "")
             for s in full]
    result = ask_twice(client, QUIZ_SYSTEM, "Die Geschichten von heute:\n\n" + "\n\n".join(lines), QUIZ_SCHEMA,
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


def image_options(query, limit=4, avoid=()):
    """Bis zu vier freie Querformat-Fotos zum Suchbegriff, zuerst aus den geprüften Qualitätsbildern.
    Fotos aus den Ausgaben der letzten 30 Tage (avoid) kommen nicht noch einmal."""
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
            url = urllib.parse.urlunsplit(url._replace(query=urllib.parse.urlencode(params_clean)))
            if url in avoid or info.get("descriptionurl") in avoid:
                continue
            seen.add(title)
            found.append({
                "url": url,
                "credit": credit,
                "source": info.get("descriptionurl"),
                "title": title.removeprefix("File:").rsplit(".", 1)[0].replace("_", " "),
                "description": clean(meta.get("ImageDescription", {}).get("value", ""), 160),
            })
            if len(found) >= limit:
                return found
    return found


def attach_images(client, stories, avoid=()):
    """Symbolfotos für Geschichten ohne Vorschaubild des Verlags: Claude nennt Suchwörter, Commons liefert Fotos,
    dann wählt Claude pro Nachricht das passende oder keins. Gibt die Zahl der Symbolfotos zurück."""
    stories = [s for s in stories if not s.get("image")]
    for story in stories:
        story["image"] = None
    if not stories:
        return 0
    lines = [f"[{s['id']}] {s['headline']}. {s['teaser']}" for s in stories]
    result = ask_twice(client, IMAGE_SYSTEM, "Die Nachrichten:\n\n" + "\n".join(lines), IMAGE_SCHEMA,
                       max_tokens=6000, effort="low")
    queries = {q["storyID"]: q["query"] for q in result["queries"]}
    with ThreadPoolExecutor(6) as pool:
        options = list(pool.map(lambda story: image_options(queries[story["id"]], avoid=avoid)
                                if story["id"] in queries else [], stories))
    blocks = []
    for story, found in zip(stories, options):
        if found:
            choices = "\n".join(f"  {k}: {o['title']}. {o['description']}" for k, o in enumerate(found))
            blocks.append(f"[{story['id']}] {story['headline']}. {story['teaser']}\n{choices}")
    picks = {}
    if blocks:
        result = ask_twice(client, IMAGE_PICK_SYSTEM, "\n\n".join(blocks), IMAGE_PICK_SCHEMA, max_tokens=6000,
                           effort="low")
        picks = {p["storyID"]: p["choice"] for p in result["picks"]}
    count, used = 0, set()
    for story, found in zip(stories, options):
        choice = picks.get(story["id"], -1)
        if 0 <= choice < len(found) and found[choice]["url"] not in used:  # dasselbe Foto nicht zweimal am Tag
            story["image"] = {**{key: found[choice][key] for key in ("url", "credit", "source")}, "kind": "symbol"}
            used.add(found[choice]["url"])
            count += 1
    return count


# Vorschaubilder der Verlage: das Bild, das eine Seite für Linkvorschauen angibt (og:image, twitter:image),
# sonst das Bild aus dem Feed. Gespeichert wird nur die Adresse, die App lädt das Bild beim Verlag.

META_TAG = re.compile(r"<meta\b[^>]*>", re.I)
ATTRIBUTE = re.compile(r"""([a-zA-Z_:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
PAGE_IMAGE_KEYS = ("og:image:secure_url", "og:image", "og:image:url", "twitter:image", "twitter:image:src")
GENERIC_FOLDER = {"logo", "logos", "placeholder", "placeholders", "platzhalter", "favicon", "favicons", "icon", "icons",
                  "avatar", "avatars", "sprite", "sprites"}
GENERIC_IMAGE = GENERIC_FOLDER | {"default", "fallback", "blank", "spacer", "dummy", "noimage", "nophoto", "watermark"}
IMAGE_FORMATS = {"JPEG", "MPO", "PNG", "WEBP", "AVIF"}
IMAGE_BYTES = 3 * 1024 * 1024  # so viel wird zum Prüfen eines Bildes höchstens geladen
PROBED = {}  # Bildadresse: (Breite, Höhe), None bei allem, was kein brauchbares Rasterbild ist


def image_url(url, base):
    """Absolute https-Adresse eines Bildes, sonst None."""
    url = (url or "").strip()
    if not url or url.startswith("data:"):
        return None
    url = urllib.parse.urljoin(base, url)
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url if url.startswith("https://") else None


def page_images(page, base):
    """Vorschaubilder aus dem Kopf einer Seite, og:image vor twitter:image."""
    head = re.split(r"</head>", page[:400000], maxsplit=1, flags=re.I)[0]
    found = {}
    for tag in META_TAG.findall(head):
        attributes = {match.group(1).lower(): html.unescape(match.group(2) or match.group(3) or match.group(4) or "")
                      for match in ATTRIBUTE.finditer(tag)}
        key = (attributes.get("property") or attributes.get("name") or "").strip().lower()
        if key in PAGE_IMAGE_KEYS and attributes.get("content", "").strip():
            found.setdefault(key, attributes["content"].strip())
    urls = [image_url(found[key], base) for key in PAGE_IMAGE_KEYS if key in found]
    return list(dict.fromkeys(url for url in urls if url))


def looks_generic(url):
    """Logos, Platzhalter, Symbole und Vektorgrafiken erkennt man meist schon an der Adresse. „default“ zählt nur im
    Dateinamen, denn manche Bilddienste haben es in jedem Pfad (New Atlas, NPR: /dims4/default/)."""
    folder, _, name = urllib.parse.urlsplit(url).path.lower().rpartition("/")
    return (name.endswith((".svg", ".gif", ".ico")) or bool(set(re.split(r"[^a-z0-9]+", name)) & GENERIC_IMAGE)
            or bool(set(re.split(r"[^a-z0-9]+", folder)) & GENERIC_FOLDER))


def probe_image(url):
    """Breite und Höhe eines Rasterbilds, gelesen aus dem Anfang der Datei. None, wenn es keins ist."""
    if url in PROBED:
        return PROBED[url]
    from PIL import Image
    size = None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": BROWSER})
        with urllib.request.urlopen(request, timeout=15) as response:
            kind = (response.headers.get_content_type() or "").lower()
            if kind.startswith("image/") or kind in ("application/octet-stream", "binary/octet-stream"):
                data = b""
                while len(data) < IMAGE_BYTES:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    data += chunk
                    try:
                        with Image.open(io.BytesIO(data)) as image:
                            size = image.size if image.format in IMAGE_FORMATS else None
                        break
                    except Exception:  # Kopf noch nicht vollständig gelesen
                        continue
    except Exception:  # Server antwortet nicht oder verweigert Bots: dann eben kein Foto
        size = None
    PROBED[url] = size
    return size


def usable_image(url, avoid=()):
    """Echtes Foto: mindestens 600 Pixel breit, Seitenverhältnis von 1:2 bis 5:2, nicht schon verwendet."""
    if not url or url in avoid or looks_generic(url):
        return False
    size = probe_image(url)
    return bool(size) and size[0] >= 600 and 0.5 <= size[0] / max(size[1], 1) <= 2.5


def source_image(items, avoid=()):
    """Vorschaubild des Verlags zu einer Meldung: aus dem Artikel, sonst aus dem Feed. None, wenn keins taugt."""
    for item in items[:2]:
        urls = PAGE_IMAGES.get(item["link"], []) + [image_url(url, item["link"]) for url in item.get("media") or []]
        for url in list(dict.fromkeys(url for url in urls if url))[:3]:
            if usable_image(url, avoid):
                return {"url": url, "credit": item["source"], "source": item["link"], "kind": "source"}
    return None


def attach_source_images(entries, avoid=()):
    """entries: Paare aus Geschichte und ihren Meldungen. Jede Geschichte bekommt das Vorschaubild ihres Verlags,
    wenn es eins gibt. Steht dasselbe Bild bei mehreren Geschichten, ist es meist ein Platzhalter und fällt weg.
    Gibt die Zahl der Fotos zurück."""
    with ThreadPoolExecutor(8) as pool:
        found = list(pool.map(lambda entry: source_image(entry[1], avoid), entries))
    uses = Counter(image["url"] for image in found if image)
    count = 0
    for (story, _), image in zip(entries, found):
        if image and uses[image["url"]] == 1:
            story["image"] = image
            count += 1
    return count


# MARK: Comic des Tages
# Jeden Wochentag eine andere Reihe (COMIC_PLAN in sources.py): „Vater und Sohn“, „Sandra und Woo“,
# Wilhelm Busch und „Sonne & Wolke“, unser eigener Comic. Fällt eine Reihe aus, springt die nächste ein.

def fetch_bytes(url, agent=USER_AGENT):
    request = urllib.request.Request(url, headers={"User-Agent": agent})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def wiki_api(host, **params):
    return fetch_json(f"https://{host}/w/api.php?" + urllib.parse.urlencode({**params, "format": "json"}))


def wiki_images(host, titles, width=1200):
    """Adresse, Breite und Höhe von Bilddateien eines Wikis, als {Titel: Seite}. Die App lädt sie direkt dort."""
    found = {}
    for start in range(0, len(titles), 40):
        data = wiki_api(host, action="query", titles="|".join(titles[start:start + 40]),
                        prop="imageinfo", iiprop="url|size", iiurlwidth=width)["query"]
        renamed = {entry["to"]: entry["from"] for entry in data.get("normalized", [])}
        for page in data["pages"].values():
            if not page.get("imageinfo"):
                continue
            info = page["imageinfo"][0]
            found[renamed.get(page["title"], page["title"])] = {
                "url": (info.get("thumburl") or info["url"]).split("?")[0],
                "width": info.get("thumbwidth") or info["width"],
                "height": info.get("thumbheight") or info["height"],
            }
    return found


def vater_und_sohn(state, client, stories):
    """Eine Bildgeschichte von e.o.plauen, ganz ohne Worte. Nach der letzten geht es von vorn los."""
    index = state.get("vaterundsohn", 0) % len(VATER_UND_SOHN)
    name, title, year = VATER_UND_SOHN[index]
    file = f"Datei:Vater und Sohn - {name}"
    page = wiki_images("de.wikipedia.org", [file]).get(file)
    if not page:
        raise Skipped(f"Bild {name} fehlt")
    state["vaterundsohn"] = index + 1
    return {
        "series": "vaterundsohn",
        "title": "Vater und Sohn",
        "episode": title,
        "image": page["url"],
        "pages": [page],
        "credit": f"e.o.plauen (Erich Ohser), {year}, gemeinfrei",
        "link": "https://de.wikipedia.org/wiki/Vater_und_Sohn",
        "linkTitle": "Mehr über Vater und Sohn",
    }


TEXT_TEMPLATES = {"SperrSchrift", "Sperrschrift", "Kapitälchen", "g", "Antiqua", "Kursiv", "fett"}


def wiki_verses(raw):
    """Verse aus einem Stück Wikitext, ohne Vorlagen, Tabellen und Seitenzahlen."""
    def unwrap(match):
        name, *args = match.group(1).split("|")
        return args[-1] if name.strip() in TEXT_TEMPLATES and args else ""
    previous = None
    while previous != raw:  # Vorlagen von innen nach außen auflösen
        previous, raw = raw, re.sub(r"\{\{([^{}]*)\}\}", unwrap, raw)
    raw = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", raw)
    raw = re.sub(r"<!--.*?-->|</?(?:poem|center|br|div|span)[^>]*>|'''?", "\n", raw, flags=re.S)
    lines = []
    for line in raw.split("\n"):
        line = line.strip()
        if line.startswith(("{|", "|}", "|-", "!")):
            continue
        if line.startswith("|"):  # Tabellenzelle: Text steht hinter dem letzten senkrechten Strich
            line = line.rsplit("|", 1)[1].strip()
        line = re.sub(r"\s+", " ", line).strip(" –-—")
        line = re.sub(r"\s+[–—]\s+", ", ", line)
        if line:
            lines.append(line)
    return lines


def wikisource_pages(page):
    """Eine Bildergeschichte von Wikisource als Seiten: jedes Bild mit den Versen davor."""
    text = wiki_api("de.wikisource.org", action="parse", page=page, prop="wikitext")["parse"]["wikitext"]["*"]
    text = re.split(r"\n==[^=]", text)[0]  # Inhaltsverzeichnis und Kategorien weglassen
    parts = re.split(r"\[\[(?:Bild|Datei|File|Image):([^|\]]+)[^\]]*\]\]", text)
    names = [parts[i].strip() for i in range(1, len(parts), 2)]
    images = wiki_images("de.wikisource.org", [f"Datei:{name}" for name in names], width=900)
    pages = []
    for i, name in enumerate(names):
        verses = wiki_verses(parts[2 * i])
        if len(verses) > 8:  # Lange Strophen bekommen eine eigene Seite
            pages.append({"caption": "\n".join(verses[:-8])})
            verses = verses[-8:]
        image = images.get(f"Datei:{name}")
        if image:
            pages.append({**image, "caption": "\n".join(verses) or None})
        elif verses:
            pages.append({"caption": "\n".join(verses)})
    rest = wiki_verses(parts[-1])
    if rest and pages and len(rest) <= 6 and pages[-1].get("url") and len((pages[-1].get("caption") or "").split("\n")) <= 2:
        pages[-1]["caption"] = "\n".join(filter(None, [pages[-1].get("caption"), *rest]))
    elif rest:  # Schlussverse gleichmäßig auf Textseiten mit höchstens zehn Zeilen verteilen
        size = -(-len(rest) // -(-len(rest) // 10))
        for start in range(0, len(rest), size):
            pages.append({"caption": "\n".join(rest[start:start + size])})
    return pages


def busch(state, client, stories):
    """Sonntags ein Streich von Max und Moritz, Bild für Bild mit den Versen von Wilhelm Busch."""
    index = state.get("busch", 0) % len(BUSCH)
    title, episode, sources = BUSCH[index]
    pages = [page for source in sources for page in wikisource_pages(source)]
    if sum(1 for page in pages if page.get("url")) < 3:
        raise Skipped("zu wenige Bilder bei Wikisource")
    state["busch"] = index + 1
    return {
        "series": "busch",
        "title": title,
        "episode": episode,
        "image": next(page["url"] for page in pages if page.get("url")),
        "pages": pages,
        "credit": "Wilhelm Busch, 1865, gemeinfrei. Text und Bilder nach Wikisource",
        "link": "https://de.wikisource.org/wiki/" + sources[0],
        "linkTitle": "Bei Wikisource lesen",
    }


def detect_panels(data):
    """Findet die einzelnen Bilder eines Strips an den weißen Zwischenräumen.
    Gibt Rechtecke [x, y, Breite, Höhe] von 0 bis 1 zurück, None bei nur einem Bild."""
    from PIL import Image
    image = Image.open(io.BytesIO(data)).convert("L")
    width, height = image.size
    ink = image.point(lambda v: 255 if v < 160 else 0)

    def runs(profile, minimum):
        """Abschnitte mit Tinte, getrennt durch leere Streifen von mindestens 4 Pixeln."""
        found, start, gap = [], None, 0
        for i, value in enumerate(profile + [0] * 4):
            if value > 1:
                if start is None:
                    start = i
                gap = 0
            elif start is not None:
                gap += 1
                if gap >= 4:
                    if i - gap + 1 - start >= minimum:
                        found.append((start, i - gap + 1))
                    start, gap = None, 0
        return found

    profile = list(ink.resize((1, height), Image.BOX).getdata())
    rows = runs(profile, height * 0.12)
    if len(rows) == 1 and height / width > 0.45:
        # Zwei Reihen, aber eine Figur oder Sprechblase ragt in den Zwischenraum:
        # an der hellsten Zeile in der Mitte trennen, wenn dort wenig Tinte ist
        top, bottom = rows[0]
        middle = range(top + (bottom - top) // 3, top + 2 * (bottom - top) // 3)
        cut = min(middle, key=lambda y: profile[y])
        if profile[cut] < 255 * 0.25:
            rows = [(top, cut), (cut, bottom)]
    panels = []
    for top, bottom in rows:
        band = ink.crop((0, top, width, bottom))
        for left, right in runs(list(band.resize((width, 1), Image.BOX).getdata()), width * 0.12):
            panels.append([round(left / width, 4), round(top / height, 4),
                           round((right - left) / width, 4), round((bottom - top) / height, 4)])
    return panels if len(panels) > 1 else None


def strip_transcript(content):
    text = html.unescape(re.sub(r"<[^>]+>", " ", content))
    return re.sub(r"\s+", " ", text).strip()[:600]


def sandra_und_woo(state, client, stories):
    """Drei Strips am Stück, der Reihe nach. Vorher prüft Claude die Dialoge auf Familientauglichkeit."""
    from PIL import Image
    offset = state.get("sandraundwoo", 0)  # Zahl der schon gelesenen Beiträge im Blog
    posts = json.loads(fetch_bytes(f"{SANDRA_UND_WOO}/wp-json/wp/v2/posts?per_page=12&offset={offset}"
                                   "&order=asc&orderby=date&_fields=title,link,content", BROWSER))
    candidates = []
    for position, post in enumerate(posts):
        match = re.match(r"\[(\d{4})\]\s*(.+)", html.unescape(post["title"]["rendered"]))
        if match:  # Andere Beiträge sind Ankündigungen oder Zeichnungen ohne Strip
            candidates.append({"position": offset + position, "number": int(match.group(1)),
                               "title": match.group(2).strip(), "link": post["link"],
                               "text": strip_transcript(post["content"]["rendered"])})
    if not candidates:
        raise Skipped("keine neuen Strips")
    lines = [f"[{c['number']}] {c['title']}: {c['text']}" for c in candidates]
    result = ask_twice(client, STRIP_CHECK_SYSTEM, "\n\n".join(lines), STRIP_CHECK_SCHEMA, max_tokens=1000, effort="low")
    suitable = {entry["number"] for entry in result["strips"] if entry["ok"]}
    chosen = [c for c in candidates if c["number"] in suitable][:3]
    if not chosen:
        state["sandraundwoo"] = offset + len(posts)  # Beim nächsten Mal weiter hinten suchen
        raise Skipped("keine passenden Strips")
    pages = []
    for strip in chosen:
        found = re.search(r'<img src="(/woode/comics/[^"]+)"', fetch_bytes(strip["link"], BROWSER).decode("utf-8", "replace"))
        if not found:
            continue
        source = "https://www.sandraandwoo.com" + found.group(1)
        target = OUT / "comics" / "sandraundwoo" / f"{strip['number']:04d}.{source.rsplit('.', 1)[-1].lower()}"
        if not target.exists():  # Unverändert ablegen, so verlangt es die Lizenz
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(fetch_bytes(source, BROWSER))
        data = target.read_bytes()
        width, height = Image.open(io.BytesIO(data)).size
        pages.append({"url": f"{PAGES_URL}/comics/sandraundwoo/{target.name}", "width": width, "height": height,
                      "title": strip["title"], "panels": detect_panels(data)})
    if not pages:
        raise Skipped("keine Bilder gefunden")
    state["sandraundwoo"] = chosen[-1]["position"] + 1
    return {
        "series": "sandraundwoo",
        "title": "Sandra und Woo",
        "episode": chosen[0]["title"] if len(pages) == 1 else f"{chosen[0]['title']} und mehr",
        "image": pages[0]["url"],
        "pages": pages,
        "credit": "Sandra und Woo von Oliver Knörzer (Autor) und Powree (Künstlerin), CC BY-NC-ND 3.0",
        "link": SANDRA_UND_WOO + "/",
        "linkTitle": "Mehr von Sandra und Woo",
    }


def sonne_und_wolke(state, client, stories):
    """Unser eigener Comic: Sonne und Wolke unterhalten sich in drei Bildern über eine Nachricht von heute."""
    lines = [f"[{s['id']}] {s['headline']}. {s['teaser']}" for s in stories]
    result = no_dashes(ask_twice(client, COMIC_SYSTEM.format(icons=", ".join(ICONS)),
                                 "Die Nachrichten von heute:\n\n" + "\n".join(lines), COMIC_SCHEMA,
                                 max_tokens=3000, effort="medium"))
    story = next((s for s in stories if s["id"] == result.get("storyID")), None)
    panels = []
    for panel in result["panels"][:3]:
        entry = {"prop": panel.get("prop") if panel.get("prop") in ICONS else None,
                 "caption": (panel.get("caption") or "").strip() or None}
        for who in ("sun", "cloud"):
            face = panel.get(who) or {}
            text = (face.get("text") or "").strip() or None
            if text and len(text) > 100:
                raise Skipped("Sprechblase zu lang")
            entry[who] = {"mood": face.get("mood") if face.get("mood") in COMIC_MOODS else "froh", "text": text}
        panels.append(entry)
    if len(panels) != 3 or sum(1 for p in panels if p["sun"]["text"] or p["cloud"]["text"]) < 2:
        raise Skipped("Comic unvollständig")
    return {
        "series": "sonnewolke",
        "title": "Sonne & Wolke",
        "episode": result["title"].strip(),
        "strip": {"storyID": story["id"] if story else None, "panels": panels},
        "credit": "Ein Comic von The Daily Good",
    }


COMIC_SERIES = {
    "vaterundsohn": vater_und_sohn,
    "busch": busch,
    "sandraundwoo": sandra_und_woo,
    "sonnewolke": sonne_und_wolke,
}


def comic_of_the_day(today, client, stories):
    """Comic für heute nach Wochentag. Die Reihen merken sich in state/comic.json, wo sie stehen."""
    state_file = STATE / "comic.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    last = state.get("last") or {}
    if last.get("date") == today.isoformat() and last.get("comic"):
        return last["comic"]  # Zweiter Lauf am selben Tag: derselbe Comic
    planned = COMIC_PLAN[today.weekday()]
    for series in [planned] + [s for s in COMIC_FALLBACK if s != planned]:
        try:
            comic = COMIC_SERIES[series](state, client, stories)
        except Exception as error:  # Eine Reihe fällt aus, die nächste springt ein
            note(f"Comic {series} übersprungen ({error})")
            continue
        state = {key: value for key, value in state.items() if key not in ("next", "last")}
        state["last"] = {"date": today.isoformat(), "comic": comic}
        STATE.mkdir(exist_ok=True)
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        return comic
    return None


# MARK: Kurzbericht

def write_summary(title, lines):
    """Kurzbericht für die Übersicht des GitHub-Laufs (Job Summary). Ohne GitHub passiert nichts."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as summary:
            summary.write(f"### {title}\n\n" + "".join(f"- {line}\n" for line in lines))
            if NOTES:
                summary.write("\n**Hinweise**\n\n" + "".join(f"- {text}\n" for text in NOTES[:40]))
            summary.write("\n")
    except OSError:  # Der Bericht darf den Lauf nie stören
        pass


def fail(today, message):
    """Bricht ohne Ausgabe ab. Kommt in der Nacht noch ein Termin (--retry-later), endet der Lauf ohne Fehler und
    nur mit Warnung, denn der nächste Termin versucht es noch einmal und Mirco bekommt keine Fehler-Mail.
    Der letzte Termin und ein Lauf von Hand enden mit Fehler."""
    later = " Ein späterer Termin versucht es noch einmal." if RETRY_LATER else ""
    print(f"   {message}{later}")
    write_summary(f"Ausgabe {today.isoformat()} nicht geschrieben", [message + later, f"Tokens: {USAGE.report()}"])
    if not RETRY_LATER:
        sys.exit(1)
    print(f"::warning::Ausgabe {today.isoformat()} nicht geschrieben.{later}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:  # damit der Schritt „Veröffentlichen“ nichts Halbes hochlädt
        with open(output, "a", encoding="utf-8") as file:
            file.write("abgebrochen=ja\n")
    sys.exit(0)


# MARK: Ablauf

def edition_ready(path):
    """Gibt es die Ausgabe schon? Halbe Ausgaben werden nicht mehr geschrieben, deshalb gilt jede lesbare Datei
    mit mindestens einer großen Geschichte als fertig."""
    try:
        edition = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    stories = edition.get("stories") if isinstance(edition, dict) else None
    return isinstance(stories, list) and any(isinstance(story, dict) and not story.get("brief") for story in stories)


def required(planned, minimum):
    """Wie viele von planned mindestens geschrieben sein müssen: minimum, aber nie mehr als vier Fünftel
    des Geplanten, damit ein Tag mit wenig guten Nachrichten nicht scheitert."""
    return min(minimum, planned - planned // 5)


def main():
    parser = argparse.ArgumentParser(description="Erzeugt die Tagesausgabe von The Daily Good.")
    parser.add_argument("--hours", type=int, default=36, help="Wie weit zurück Meldungen gesammelt werden")
    parser.add_argument("--stories", type=int, default=12, help="Anzahl der ausführlichen Geschichten")
    parser.add_argument("--briefs", type=int, default=30, help="Höchstzahl der Kurzmeldungen (nur ab 6 Punkten)")
    parser.add_argument("--per-ressort", type=int, default=5, help="Höchstens so viele Beiträge pro Ressort")
    parser.add_argument("--per-region", type=int, default=2, help="Höchstens so viele Meldungen pro Bundesland")
    parser.add_argument("--no-region", action="store_true", help="Ohne die Rubrik „Aus deiner Region“")
    parser.add_argument("--dry-run", action="store_true", help="Nur auswählen, nichts schreiben")
    parser.add_argument("--no-batch", action="store_true", help="Einzeln statt per Batch fragen (schneller, doppelt so teuer)")
    parser.add_argument("--skip-if-exists", action="store_true", help="Nichts tun, wenn es die heutige Ausgabe schon gibt")
    parser.add_argument("--min-stories", type=int, default=MIN_STORIES,
                        help="Mindestzahl großer Geschichten, sonst wird nichts geschrieben")
    parser.add_argument("--min-briefs", type=int, default=MIN_BRIEFS,
                        help="Mindestzahl der Kurzmeldungen, sonst wird nichts geschrieben")
    parser.add_argument("--retry-later", action="store_true",
                        help="In der Nacht kommt noch ein Termin: Mindestumfang verlangen, sonst ohne Fehler abbrechen")
    args = parser.parse_args()
    global RETRY_LATER
    RETRY_LATER = args.retry_later
    if not RETRY_LATER:  # letzter Termin oder Lauf von Hand: lieber eine kleinere Ausgabe als gar keine
        args.min_stories, args.min_briefs = 1, 0

    today = dt.date.today()
    target = OUT / "editions" / f"{today.isoformat()}.json"
    if args.skip_if_exists and target.exists():
        if edition_ready(target):
            print(f"Die Ausgabe vom {today.isoformat()} gibt es schon, nichts zu tun.")
            write_summary(f"Ausgabe {today.isoformat()}", ["gab es schon, nichts zu tun"])
            return
        print(f"Die Ausgabe vom {today.isoformat()} ist unlesbar oder leer und wird neu geschrieben.")
    client = anthropic.Anthropic(api_key=api_key()) if BACKEND == "api" else None
    print(f"Über {'das Claude-Abo (' + ABO_MODEL + ')' if BACKEND == 'abo' else 'die API (' + MODEL + ')'}")
    history = History(today)

    print("1. Meldungen sammeln …")
    items = collect(args.hours)
    print(f"   {len(items)} Meldungen aus {len(FEEDS)} Quellen")
    recipe_request, recipe_items = None, None
    try:  # Die Rezepte stehen vor der Auswahl fest, damit keins davon auch als Nachricht erscheint
        recipe_found = recipe_job()
        if recipe_found:
            recipe_request, recipe_items = recipe_found
    except Exception as error:  # Ohne Rezept erscheint die Ausgabe trotzdem
        note(f"Rezept übersprungen ({error})")
    items, known, recipes = drop_known(items, history, recipe_links(recipe_items))
    print(f"   {known} schon gebrachte Meldungen und {recipes} Rezepte aussortiert, {len(items)} bleiben")

    print("2. Gute Nachrichten aussuchen …")
    try:
        candidates = triage(client, items, not args.no_batch, history.headlines())
    except anthropic.RateLimitError:
        fail(today, "Zu viele Anfragen gerade. Bitte später noch einmal.")
    except anthropic.APIStatusError as error:
        fail(today, f"API-Fehler {error.status_code}: {error.message}")
    except anthropic.APIConnectionError:
        fail(today, "Keine Verbindung zur API.")
    except (Skipped, json.JSONDecodeError, ValueError, KeyError, TypeError) as reason:
        fail(today, f"Auswahl fehlgeschlagen: {reason}")
    candidates = drop_repeats(candidates, history)
    chosen, short = select(candidates, args.stories, args.briefs, args.per_ressort)
    print(f"   {len(candidates)} Kandidaten, {len(chosen)} ausführlich, {len(short)} kurz:")
    for candidate in chosen + short:
        kind = "kurz" if candidate in short else "lang"
        print(f"   {candidate['score']:>2}  {kind}  {candidate['ressort']:<12} {candidate['items'][0]['title'][:74]}")
    print("   pro Ressort:", dict(Counter(c["ressort"] for c in chosen + short).most_common()))

    regional_picks = []
    if not args.no_region:
        print("   Aus den Regionen …")
        try:
            main_texts = [text for candidate in chosen + short for text in candidate_texts(candidate)]
            regional = regional_items(args.hours, history, main_texts)
            print(f"   {len(regional)} Regionalmeldungen")
            regional_picks = regional_triage(client, regional, args.per_region, history.headlines(regional_only=True),
                                             [candidate["items"][0]["title"] for candidate in chosen + short])
            per_state = Counter(code for entry in regional_picks for code in entry["regions"])
            print(f"   {len(regional_picks)} ausgewählt:", dict(sorted(per_state.items())))
        except Exception as error:  # Ohne Regionalteil erscheint die Ausgabe trotzdem
            note(f"Regionalteil übersprungen ({error})")
            regional_picks = []
    if args.dry_run:
        print(f"Kosten bisher: {USAGE.dollars:.2f} $")
        return

    print("3. Artikel lesen und schreiben …")
    with ThreadPoolExecutor(6) as pool:
        prompts = list(pool.map(story_prompt, chosen))
        brief_prompts = list(pool.map(brief_prompt, short))
        regional_prompts = list(pool.map(regional_prompt, regional_picks))
    jobs = {f"story-{n}": (WRITER_SYSTEM, user, STORY_SCHEMA, 8000, "medium") for n, (user, _) in enumerate(prompts, 1)}
    jobs.update({f"brief-{n}": (BRIEF_SYSTEM, user, BRIEF_SCHEMA, 3000, "low") for n, user in enumerate(brief_prompts, 1)})
    jobs.update({f"region-{n}": (BRIEF_SYSTEM, user, REGIONAL_SCHEMA, 3000, "low")
                 for n, user in enumerate(regional_prompts, 1)})
    joke = joke_of_the_day(today, history.editions)
    if joke is None:
        note("Keine Witzliste gefunden, das Modell wählt den Flachwitz")
        jobs["joke"] = joke_job()
    if recipe_request:
        jobs["rezept"] = recipe_request
    tip = None
    try:
        tip_job, tip_kind, tip_label, tip_items = recommendation_job(today)
        jobs["tipp"] = tip_job
        tip = (tip_kind, tip_label, tip_items)
    except Exception as error:  # Ohne Empfehlung erscheint die Ausgabe trotzdem
        note(f"Empfehlung übersprungen ({error})")
    results = ask_all(client, jobs, not args.no_batch)

    sourced = []  # (Geschichte, Meldungen) für die Vorschaubilder
    written = []  # (Länge des Artikeltexts, Geschichte)
    for n, (candidate, (_, text_length)) in enumerate(zip(chosen, prompts), 1):
        result = results.get(f"story-{n}")
        try:
            if isinstance(result, Exception) or result is None:
                raise Skipped(str(result))
            story = build_story(candidate, result, f"{today.isoformat()}-{n:02d}")
        except (Skipped, KeyError, TypeError, ValueError) as error:
            print(f"   übersprungen: {candidate['items'][0]['title'][:60]} ({error})")
            continue
        written.append((text_length, story))
        sourced.append((story, candidate["items"]))
    # Aufmacher wird die beste Geschichte, deren Artikel vollständig lesbar war (keine Paywall)
    lead = next((pair for pair in written if pair[0] >= 800), written[0] if written else None)
    stories = [story for _, story in ([lead] if lead else []) + [pair for pair in written if pair is not lead]]
    for n, candidate in enumerate(short, 1):
        result = results.get(f"brief-{n}")
        try:
            if isinstance(result, Exception) or result is None:
                raise Skipped(str(result))
            story = build_brief(candidate, result, f"{today.isoformat()}-k{n:02d}")
        except (Skipped, KeyError, TypeError, ValueError) as error:
            print(f"   Kurzmeldung übersprungen: {candidate['items'][0]['title'][:60]} ({error})")
            continue
        stories.append(story)
        sourced.append((story, candidate["items"]))
    regional_stories, elsewhere = [], 0
    for n, entry in enumerate(regional_picks, 1):
        result = results.get(f"region-{n}")
        try:
            if isinstance(result, Exception) or result is None:
                raise Skipped(str(result))
            story = build_regional(entry, result, f"{today.isoformat()}-r{n:02d}")
        except (Skipped, KeyError, TypeError, ValueError) as error:
            elsewhere += "spielt in" in str(error)
            print(f"   Regionalmeldung übersprungen: {entry['item']['title'][:60]} ({error})")
            continue
        regional_stories.append(story)
        sourced.append((story, [entry["item"]]))
    if elsewhere:
        note(f"{elsewhere} Regionalmeldungen spielten nicht im Land ihres Feeds und fielen weg")

    # Mindestumfang: Endet das Abo-Kontingent mitten im Lauf, erscheint keine halbe Ausgabe
    full_count = sum(1 for story in stories if not story.get("brief"))
    brief_count = len(stories) - full_count
    need_full, need_briefs = required(len(chosen), args.min_stories), required(len(short), args.min_briefs)
    if not stories or full_count < need_full or brief_count < need_briefs:
        failed = sum(1 for result in results.values() if isinstance(result, Exception))
        fail(today, f"Zu wenig geschrieben: {full_count} von {len(chosen)} Geschichten (nötig {need_full}) und "
                    f"{brief_count} von {len(short)} Kurzmeldungen (nötig {need_briefs}), {failed} Anfragen gescheitert. "
                    "Die Ausgabe bleibt, wie sie ist.")

    if joke is None:
        joke = results.get("joke")
        if isinstance(joke, dict) and joke.get("setup") and joke.get("punchline"):
            joke = no_dashes({"setup": joke["setup"], "punchline": joke["punchline"]})
        else:
            note(f"Flachwitz übersprungen ({joke})")
            joke = None

    photos = Counter()
    try:
        attach_source_images(sourced, history.photos)
        for story in stories + regional_stories:
            if story.get("image"):
                photos["region" if story["ressort"] == "region" else "kurz" if story.get("brief") else "lang"] += 1
    except Exception as error:  # Ohne Vorschaubilder erscheint die Ausgabe trotzdem
        note(f"Vorschaubilder übersprungen ({error})")
    try:
        full = [s for s in stories if not s.get("brief")]  # Kurzmeldungen bekommen keine Symbolfotos
        photos["symbol"] = attach_images(client, full, history.photos)
    except Exception as error:
        note(f"Symbolfotos übersprungen ({error})")
    print(f"   Fotos: {photos['lang']} vom Verlag und {photos['symbol']} Symbolfotos bei {full_count} Geschichten, "
          f"{photos['kurz']} von {brief_count} Kurzmeldungen, {photos['region']} von {len(regional_stories)} aus den Regionen")

    recommendation = None
    if tip:
        try:
            recommendation = finish_recommendation(results.get("tipp"), *tip, today)
            print(f"   Empfehlung: {recommendation['label']}, {recommendation['title']}" if recommendation else "   Heute keine belegte Empfehlung")
        except Exception as error:
            note(f"Empfehlung übersprungen ({error})")
    recipe = None
    if recipe_items:
        try:
            recipe = finish_recipe(results.get("rezept"), recipe_items)
            print(f"   Rezept: {recipe['title']}" if recipe else "   Heute kein passendes Rezept")
        except Exception as error:
            note(f"Rezept übersprungen ({error})")
    if recipe:  # Das Rezept des Tages steht nicht noch einmal als Nachricht in der Ausgabe
        link = recipe["source"]["url"]
        twins = [s for s in stories[1:] if any(source["url"] == link for source in s.get("sources") or [])]
        if twins:
            note(f"{len(twins)} Meldung mit dem Rezept des Tages entfernt")
            stories = [s for s in stories if s not in twins]

    try:
        fixed, spoiled = fix_guesses(client, [s for s in stories if not s.get("brief")])
        if spoiled:
            print(f"   Tippfragen: {spoiled} verrieten die Antwort, {fixed} neu geschrieben")
    except Exception as error:
        note(f"Tippfragen nicht geprüft ({error})")

    quiz = None
    try:
        quiz = make_quiz(client, stories)
        print(f"   Quiz mit {len(quiz['questions'])} Fragen" if quiz else "   Kein Quiz heute")
    except Exception as error:
        note(f"Quiz übersprungen ({error})")

    weekly = None
    try:
        weekly = weekly_review(today, stories)
        if weekly:
            print(f"   Wochenrückblick mit {len(weekly['stories'])} Geschichten")
    except Exception as error:
        note(f"Wochenrückblick übersprungen ({error})")

    comic = None
    try:
        comic = comic_of_the_day(today, client, stories)
        print(f"   Comic: {comic['title']}, {comic.get('episode')}" if comic else "   Kein Comic heute")
    except Exception as error:  # Ohne Comic erscheint die Ausgabe trotzdem
        note(f"Comic übersprungen ({error})")

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
        "regional": regional_stories,
        "recipe": recipe,
    }
    (OUT / "editions").mkdir(parents=True, exist_ok=True)
    payload = json.dumps(edition, ensure_ascii=False, indent=2)
    target.write_text(payload)
    (OUT / "latest.json").write_text(payload)
    if joke:
        remember_joke(joke)
    briefs_written = sum(1 for s in stories if s.get("brief"))
    print(f"4. Fertig: Ausgabe Nr. {edition['number']} mit {len(stories) - briefs_written} Geschichten, "
          f"{briefs_written} Kurzmeldungen und {len(regional_stories)} Meldungen aus den Regionen")
    if BACKEND == "abo":
        print(f"   Tokens: {USAGE.report()}. Über das Abo, keine Extrakosten "
              f"(über die API wären es etwa {USAGE.dollars:.2f} $)")
    else:
        print(f"   Tokens: {USAGE.report()}. Kosten etwa {USAGE.dollars:.2f} $")

    planned = COMIC_PLAN[today.weekday()]
    comic_line = "keiner"
    if comic:
        comic_line = f"{comic['title']}, {comic.get('episode') or ''}".rstrip(", ")
        if comic.get("series") != planned:
            comic_line += f" (Ersatz, geplant war {planned})"
    write_summary(f"Ausgabe {today.isoformat()}", [
        f"{len(stories) - briefs_written} Geschichten, {briefs_written} Kurzmeldungen, "
        f"{len(regional_stories)} Meldungen aus den Regionen",
        f"Fotos: {photos['lang']} vom Verlag und {photos['symbol']} Symbolfotos bei den großen Geschichten, "
        f"{photos['kurz']} bei den Kurzmeldungen, {photos['region']} bei den Regionalmeldungen",
        f"Comic: {comic_line}",
        f"Empfehlung: {recommendation['label']}, {recommendation['title']}" if recommendation else "Empfehlung: keine",
        f"Rezept: {recipe['title']}" if recipe else "Rezept: keins",
        f"Flachwitz: {joke['setup']} {joke['punchline']}" if joke else "Flachwitz: keiner",
        f"Quiz: {len(quiz['questions'])} Fragen" if quiz else "Quiz: keins",
        f"Tokens: {USAGE.report()}",
    ])


if __name__ == "__main__":
    main()
