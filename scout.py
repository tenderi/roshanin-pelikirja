#!/usr/bin/env python3
"""
Dota 2 -vastustajien pelikirja / scouting-raportti
==================================================

Lukee joukkueet ja pelaajat tiedostosta `joukkueet.txt` ja hakee jokaisesta
pelaajasta OpenDota API:sta (https://www.opendota.com/, ei vaadi API-avainta):
  - profiilin (persona-nimi + rank medal) -> Steam ID:n oikeellisuuden tarkistus
  - top-heropoolin kaikilta ajoilta (pelit + win rate)
  - viimeaikaisen heropoolin ja muodon (viimeisimmät N ottelua)
  - pelipaikkajakauman (safe / mid / off / jungle)

...ja koostaa niistä joukkuekohtaisen Markdown-pelikirjan.

Jokaiselle joukkueelle lasketaan bannilista, joka painottaa joukkueen
kovimman MMR:n pelaajia (ks. team_bans).

KÄYTTÖ
------
    pip install requests
    python3 scout.py              # raportit, raakadata ja verkkosivusto
    python3 scout.py --pdf        # sama + PDF per joukkue (valinnainen)
    python3 scout.py --oma "Joukkueeni"   # merkitse oma joukkue

Pelipaikat ilmoitetaan `joukkueet.txt`:ssä neljäntenä kenttänä:

    Nick | MMR | STEAM_0:Y:Z | hard support

Kelpaavat mm. "1".."5", "safelane", "mid", "offlane", "soft support",
"hard support", "kantaja", "keskilinja" (ks. ROLE_ALIASES). Pelipaikka
näytetään pelaajan kortissa.

Oman joukkueen voi valita kolmella tavalla, tässä järjestyksessä:
    1. lipulla  --oma "Joukkueen nimi"  (osittainen nimi riittää)
    2. ympäristömuuttujalla  OMA_JOUKKUE
    3. merkitsemällä joukkueet.txt:ssä otsikko:  ## Joukkueeni (oma)
Oman joukkueen sivulla bannilista kertoo, mitä meiltä todennäköisesti
bannataan.

Syntyy kaksi hakemistoa: `scouting-results/` (lähdeaineisto ja raportit)
sekä `docs/` (julkaistava sivusto).

    scouting-results/
        README.md                    <- yleiskatsaus + linkit
        lph-voide/
            lph-voide.md             <- joukkueen pelikirja
            lph-voide.pdf            <- vain jos --pdf annettu
            raw/                     <- OpenDotan raakavastaukset
                seinis-104984836.json
                ...
    docs/
        .nojekyll
        index.html                   <- etusivu
        lph-voide/index.html         <- joukkueen sivu
        ...

JULKAISU GITHUB PAGESIIN
------------------------
Sivusto on valmista staattista HTML:ää hakemistossa `docs/`. Kytke se
päälle kerran repon asetuksista:

    Settings -> Pages -> Source: "Deploy from a branch"
                      -> Branch: main,  kansio: /docs  -> Save

Tämän jälkeen jokainen `docs/`-muutoksen push päivittää sivuston
osoitteessa https://<käyttäjä>.github.io/<repo>/

HUOM
----
- OpenDota API on ilmainen mutta rajoittaa pyyntömäärää (n. 60 pyyntöä/min
  ilman avainta). Jos sinulla on OpenDota API-avain, aseta se
  ympäristömuuttujaan:
      export OPENDOTA_API_KEY="oma-avaimesi"
- Pelaajan Dota-tilaston täytyy olla julkinen (Dota 2 -asetus
  "Expose Public Match Data"), tai OpenDota ei näe hänestä mitään. Tällöin
  raporttiin merkitään "Ei julkista dataa OpenDotassa".
- Raakadata (`raw/*.json`) sisältää OpenDotan vastaukset sellaisenaan:
  profiili, voitot/tappiot, heropooli, viimeisimmät ottelut ja pelipaikat.
- Syötteen Steam ID:t ovat STEAM_0:Y:Z -muodossa (Steam2). Ne muunnetaan
  OpenDotan 32-bit account_id -muotoon kaavalla: account_id = Z*2 + Y
- PDF:n luontiin käytetään ensimmäistä koneelta löytyvää työkalua:
  weasyprint, wkhtmltopdf, chromium/chrome, pandoc tai libreoffice. Jos
  yhtäkään ei löydy, viereen kirjoitetaan tulostusvalmis `pelikirja.html`
  jonka voi tulostaa selaimesta PDF:ksi. Markdownin luonti ei koskaan kaadu
  PDF-vaiheeseen.
- Vastaukset välimuistitetaan hakemistoon `.cache/`, joten ajon voi keskeyttää
  ja jatkaa myöhemmin ilman että kaikki haetaan uudelleen. Tyhjennä hakemisto
  kun haluat tuoreet luvut.
"""

import os
import re
import sys
import time
import json
import argparse
import datetime
import difflib
import html
import shutil
import subprocess
import tempfile
from collections import defaultdict, Counter

try:
    import requests
except ImportError:
    sys.exit("Tarvitset requests-kirjaston: pip install requests")

HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_FILE = os.path.join(HERE, "joukkueet.txt")
OUTPUT_DIR = os.path.join(HERE, "scouting-results")
SITE_DIR = os.path.join(HERE, "docs")   # GitHub Pages: main-haara, /docs
CACHE_DIR = os.path.join(HERE, ".cache")

OPENDOTA_BASE = "https://api.opendota.com/api"
API_KEY = os.environ.get("OPENDOTA_API_KEY", "").strip()
REQUEST_DELAY = 1.1 if not API_KEY else 0.15  # sekuntia pyyntöjen välillä
MAX_RETRIES = 4                  # 429/5xx-uudelleenyritysten maksimimäärä
MATCH_FETCH_LIMIT = 100          # kuinka monta viimeisintä ottelua haetaan
MIN_RANKED_FOR_FILTER = 10       # jos ei-turbo-otteluita väh. näin monta, turbot jätetään pois
TURBO_GAME_MODE = 23             # OpenDota game_mode: Turbo
TOP_HERO_COUNT = 8               # montako heropia per pelaaja, kaikki ajat
RECENT_HERO_COUNT = 8            # montako viimeaikaista heropia per pelaaja
MIN_GAMES_FOR_HERO = 3           # jätä pois heropit joita pelattu alle N kertaa

# Bannit (ks. team_bans)
MIN_RECENT_FOR_BAN = 2           # väh. näin monta tuoretta peliä...
MIN_ALLTIME_FOR_BAN = 15         # ...tai näin monta kaikkiaan, jotta hero huomioidaan
BAN_COUNT = 6                    # joukkueen bannilistan pituus
PLAYER_BAN_COUNT = 3             # bannikohteet per pelaaja
SUMMARY_BAN_COUNT = 3            # montako bannia etusivun kortteihin
MMR_WEIGHT_EXP = 3               # pelaajan paino = (MMR / joukkueen kovin MMR)^tämä
SUB_WEIGHT = 0.5                 # varapelaajan painokerroin
BAN_WHO_SHARE = 0.15             # "kenen takia" -listaan pelaajat joiden osuus ≥ tämä

# Pelipaikat. Käyttäjä ilmoittaa ne `joukkueet.txt`:ssä neljäntenä kenttänä;
# tunnistetaan numerolla, suomeksi ja englanniksi.
ROLE_ALIASES = {
    "1": "pos1", "pos1": "pos1", "p1": "pos1", "safe": "pos1",
    "safelane": "pos1", "safe lane": "pos1", "carry": "pos1",
    "hard carry": "pos1", "kantaja": "pos1", "ykkonen": "pos1",
    "2": "pos2", "pos2": "pos2", "p2": "pos2", "mid": "pos2",
    "midlane": "pos2", "mid lane": "pos2", "middle": "pos2",
    "keskilinja": "pos2", "kakkonen": "pos2",
    "3": "pos3", "pos3": "pos3", "p3": "pos3", "off": "pos3",
    "offlane": "pos3", "off lane": "pos3", "offlaner": "pos3",
    "offi": "pos3", "kolmonen": "pos3",
    "4": "pos4", "pos4": "pos4", "p4": "pos4", "soft support": "pos4",
    "softsupport": "pos4", "soft supp": "pos4", "soft": "pos4",
    "roamer": "pos4", "nelonen": "pos4",
    "5": "pos5", "pos5": "pos5", "p5": "pos5", "hard support": "pos5",
    "hardsupport": "pos5", "hard supp": "pos5", "full support": "pos5",
    "support": "pos5", "supp": "pos5", "tuki": "pos5", "viitonen": "pos5",
}
ROLE_LABELS = {"pos1": "Safelane (1)", "pos2": "Mid (2)", "pos3": "Offlane (3)",
               "pos4": "Soft support (4)", "pos5": "Hard support (5)"}

RANK_NAMES = {1: "Herald", 2: "Guardian", 3: "Crusader", 4: "Archon",
              5: "Legend", 6: "Ancient", 7: "Divine", 8: "Immortal"}
LANE_NAMES = {1: "Safe", 2: "Mid", 3: "Off", 4: "Jungle"}


# ---------------------------------------------------------------------------
# SYÖTTEEN LUKU
# ---------------------------------------------------------------------------

OWN_TEAM_MARKER = re.compile(r"\s*\((oma|own)\)\s*$", re.IGNORECASE)


PLAYER_LINE_LOOSE = re.compile(
    r"^(?P<nick>.+?)\s+(?P<mmr>\d[\d\s]*)\s+(?P<steam>STEAM_[0-5]:[01]:\d+)"
    r"(?:\s+(?P<role>.+?))?\s*$", re.IGNORECASE)


def split_player_line(line: str):
    """Pilkkoo pelaajarivin kenttiin [nick, mmr, steam_id(, pelipaikka)].

    Erotin on | tai /. Listat tulevat usein kopioituna eri lähteistä, joten
    hyväksytään myös pelkällä välilyönnillä erotellut rivit
    ("The Kivi 3750 STEAM_0:1:41379487") ja rivin lopun roskamerkit (", ").
    Palauttaa None jos rivi ei jäsenny.
    """
    line = line.strip().rstrip(",;").strip()
    if re.search(r"[|/]", line):
        parts = [p.strip().strip(",;").strip() for p in re.split(r"\s*[|/]\s*", line)]
        parts = [p for p in parts if p] if len(parts) > 4 else parts
        return parts if len(parts) in (3, 4) else None
    m = PLAYER_LINE_LOOSE.match(line)
    if not m:
        return None
    parts = [m.group("nick"), m.group("mmr").strip(), m.group("steam")]
    if m.group("role"):
        parts.append(m.group("role"))
    return parts


def parse_teams(path: str):
    """Lukee `joukkueet.txt`:n.

    Palauttaa ([(joukkue, [(nick, mmr, steam_id, on_sub, rooli), ...]), ...], oma)
    jossa `oma` on tiedostossa omaksi merkitty joukkue tai None.

    Rivimuoto:  Nick | MMR | STEAM_0:Y:Z [| pelipaikka]   (erotin | tai /)
    Sulkeissa oleva rivi tulkitaan varapelaajaksi: (Nick | MMR | STEAM_...)
    Otsikon perässä `(oma)` merkitsee oman joukkueen:  ## Joukkueeni (oma)

    Neljäs kenttä on valinnainen pelipaikka: "1".."5", "mid", "offlane",
    "hard support", "kantaja", ... (ks. ROLE_ALIASES). Kun se on annettu,
    näytetään pelaajan kortissa.
    """
    teams = []
    current = None
    own = None
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                name = line.lstrip("#").strip()
                if OWN_TEAM_MARKER.search(name):
                    name = OWN_TEAM_MARKER.sub("", name).strip()
                    if own and own != name:
                        print(f"  [VAROITUS] rivi {lineno}: omaksi on merkitty jo "
                              f"{own}, ohitetaan merkintä joukkueelle {name}")
                    else:
                        own = name
                current = (name, [])
                teams.append(current)
                continue
            if current is None:
                print(f"  [VAROITUS] rivi {lineno} ennen joukkueotsikkoa: {line}")
                continue
            is_sub = line.startswith("(") and line.rstrip().endswith(")")
            parts = split_player_line(line.strip("()").strip())
            if parts is None:
                print(f"  [VAROITUS] rivi {lineno} ei jäsenny: {line}")
                continue
            nick, mmr_s, steam_id = (p.strip() for p in parts[:3])
            role = None
            if len(parts) == 4 and parts[3].strip():
                key = re.sub(r"[^a-z0-9 ]", "", parts[3].strip().lower()).strip()
                role = ROLE_ALIASES.get(key)
                if not role:
                    print(f"  [VAROITUS] rivi {lineno}: tuntematon pelipaikka "
                          f"{parts[3].strip()!r} — jätetään huomiotta")
            try:
                mmr = int(re.sub(r"\D", "", mmr_s))
            except ValueError:
                print(f"  [VAROITUS] rivi {lineno}: MMR ei ole numero: {mmr_s}")
                mmr = 0
            current[1].append((nick, mmr, steam_id.upper(), is_sub, role))
    return teams, own


def resolve_own_team(teams, name: str):
    """Etsii käyttäjän antamaa nimeä vastaavan joukkueen.

    Sallii kirjainkoon, ääkkösten ja välimerkkien eroavan: "roshan" löytää
    joukkueen "Roshan ja Rähmäsilmät". Palauttaa (nimi, virheilmoitus).
    """
    name = (name or "").strip()
    if not name:
        return None, ""
    names = [t for t, _ in teams]
    for t in names:
        if t.lower() == name.lower():
            return t, ""
    slug = slugify(name)
    exact = [t for t in names if slugify(t) == slug]
    if exact:
        return exact[0], ""
    partial = [t for t in names if slug and slug in slugify(t)]
    if len(partial) == 1:
        return partial[0], ""
    if len(partial) > 1:
        return None, (f"Nimi \"{name}\" sopii useaan joukkueeseen: "
                      + ", ".join(partial))
    return None, (f"Joukkuetta \"{name}\" ei löydy. Tiedostossa ovat: "
                  + ", ".join(names))


def steam_id_to_account_id(steam_id: str) -> int:
    """Muuntaa STEAM_0:Y:Z (Steam2) -> OpenDotan käyttämä 32-bit account_id."""
    m = re.match(r"STEAM_[0-5]:([01]):(\d+)$", steam_id.strip(), re.IGNORECASE)
    if not m:
        raise ValueError(f"Tuntematon Steam ID -muoto: {steam_id}")
    return int(m.group(2)) * 2 + int(m.group(1))


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _cache_path(path: str, params: dict) -> str:
    key = path.strip("/").replace("/", "_")
    if params:
        key += "_" + "_".join(f"{k}{v}" for k, v in sorted(params.items()))
    return os.path.join(CACHE_DIR, key + ".json")


def api_get(path: str, params: dict = None, use_cache: bool = True):
    """GET OpenDota API:in. Palauttaa JSON:n tai None. Välimuistittaa levylle."""
    params = dict(params or {})
    cache_file = _cache_path(path, params)
    if use_cache and os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass  # rikkinäinen välimuisti -> haetaan uudelleen

    req_params = dict(params)
    if API_KEY:
        req_params["api_key"] = API_KEY

    url = f"{OPENDOTA_BASE}{path}"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=req_params, timeout=20)
        except requests.RequestException as e:
            print(f"  [VIRHE] Pyyntö epäonnistui ({url}): {e}")
            if attempt == MAX_RETRIES:
                return None
            time.sleep(3 * attempt)
            continue

        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                print(f"  [VIRHE] {url} -> vastaus ei ole JSON:ia")
                return None
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return data

        if resp.status_code in (429, 500, 502, 503, 504, 522):
            wait = 5 * attempt
            print(f"  [VAROITUS] {url} -> HTTP {resp.status_code}, "
                  f"odotetaan {wait}s (yritys {attempt}/{MAX_RETRIES})...")
            time.sleep(wait)
            continue

        print(f"  [VIRHE] {url} -> HTTP {resp.status_code}")
        return None

    print(f"  [VIRHE] {url} -> luovutettiin {MAX_RETRIES} yrityksen jälkeen")
    return None


HERO_SLUGS = {}  # hero_id -> kuvatiedoston nimi (täytetään load_hero_names:ssa)


def load_hero_names() -> dict:
    """hero_id -> hero-nimi."""
    print("Haetaan hero-nimistöä OpenDotasta...")
    data = api_get("/heroes")
    HERO_SLUGS.update({h["id"]: h["name"].removeprefix("npc_dota_hero_")
                       for h in (data or []) if h.get("name")})
    return {h["id"]: h.get("localized_name", f"Hero {h['id']}") for h in (data or [])}


def fetch_player(account_id: int) -> dict:
    """Hakee yhden pelaajan kaikki tarvittavat tiedot.

    `haettu` kertoo milloin data oikeasti noudettiin OpenDotasta: välimuistista
    luettaessa se on välimuistitiedoston aikaleima, ei ajohetki.
    """
    out = {}
    stamps = []
    for key, path, params in (
        ("profile", f"/players/{account_id}", None),
        ("wl",      f"/players/{account_id}/wl", None),
        ("heroes",  f"/players/{account_id}/heroes", None),
        ("matches", f"/players/{account_id}/matches", {"limit": MATCH_FETCH_LIMIT}),
        ("counts",  f"/players/{account_id}/counts", None),
    ):
        cache_file = _cache_path(path, params or {})
        cached = os.path.exists(cache_file)
        out[key] = api_get(path, params)
        if not cached:
            time.sleep(REQUEST_DELAY)
        if os.path.exists(cache_file):
            stamps.append(os.path.getmtime(cache_file))
    out["haettu"] = (datetime.date.fromtimestamp(min(stamps)).isoformat()
                     if stamps else datetime.date.today().isoformat())
    return out


# ---------------------------------------------------------------------------
# MUOTOILU
# ---------------------------------------------------------------------------

def rank_medal(rank_tier) -> str:
    if not rank_tier:
        return "–"
    tier, star = divmod(int(rank_tier), 10)
    name = RANK_NAMES.get(tier)
    if not name:
        return "–"
    return f"{name} {star}" if star else name


def top_heroes(hero_stats, top_n=TOP_HERO_COUNT):
    """[(hero_id, pelit, voitot)] kaikkien aikojen datasta."""
    rows = [h for h in (hero_stats or []) if h.get("games", 0) >= MIN_GAMES_FOR_HERO]
    rows.sort(key=lambda h: (h["games"], h["win"]), reverse=True)
    return [(h["hero_id"], h["games"], h["win"]) for h in rows[:top_n]]


def has_public_data(d) -> bool:
    """Onko pelaajalla oikeasti julkista pelidataa?

    OpenDota palauttaa yksityisillekin profiileille täyden hero-listan, jossa
    kaikki pelimäärät ovat nollia — pelkkä listan olemassaolo ei siis kerro
    mitään. Katsotaan siksi todelliset pelimäärät.
    """
    if d.get("matches"):
        return True
    return any(h.get("games", 0) > 0 for h in (d.get("heroes") or []))


def nick_matches_persona(nick: str, persona: str) -> bool:
    """Muistuttaako Steam-nimi listan nickiä? (Steam ID:n oikeellisuuden tarkistus)"""
    if not persona:
        return True  # ei nimeä -> ei voi väittää ristiriitaa
    norm = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())
    a, b = norm(nick), norm(persona)
    if not a or not b:
        return True
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.6


def match_is_win(m) -> bool:
    """OpenDota: player_slot < 128 => radiant."""
    return m.get("radiant_win") == (m.get("player_slot", 0) < 128)


def usable_matches(matches):
    """Analysoitavat ottelut + kuvaus otannasta.

    Pudottaa ottelut joilla ei ole tulosta (keskeytyneet/parsimattomat) ja
    suodattaa turbot pois, jos normaaleja otteluita on tarpeeksi jäljellä —
    turbo vääristää heropoolia eikä kerro draft-pelistä juuri mitään.
    """
    if not matches:
        return [], ""
    valid = [m for m in matches if m.get("radiant_win") is not None]
    if not valid:
        return [], ""
    non_turbo = [m for m in valid if m.get("game_mode") != TURBO_GAME_MODE]
    turbo_n = len(valid) - len(non_turbo)
    if len(non_turbo) >= MIN_RANKED_FOR_FILTER:
        note = f"turbot ({turbo_n} kpl) jätetty pois" if turbo_n else "ei turbo-otteluita"
        return non_turbo, note
    return valid, (f"sisältää {turbo_n} turbo-ottelua" if turbo_n else "")


def recent_form(matches):
    """(voitot, pelit, wr%, viimeisimmän ottelun pvm) tai None."""
    if not matches:
        return None
    wins = sum(1 for m in matches if match_is_win(m))
    total = len(matches)
    last_ts = max((m.get("start_time") or 0) for m in matches)
    last_date = (datetime.date.fromtimestamp(last_ts).isoformat() if last_ts else "?")
    return wins, total, wins / total * 100, last_date


def recent_heroes(matches, top_n=RECENT_HERO_COUNT):
    """Viimeaikaiset suosikkiheropit: [(hero_id, pelit, voitot)]."""
    games, wins = Counter(), Counter()
    for m in matches or []:
        hid = m.get("hero_id")
        if not hid:
            continue
        games[hid] += 1
        if match_is_win(m):
            wins[hid] += 1
    return [(hid, g, wins[hid]) for hid, g in games.most_common(top_n)]


def lane_split(counts):
    """'Safe 45% / Mid 30% / Off 25%' parsituista otteluista, tai None."""
    if not counts:
        return None
    lane = (counts.get("lane_role") or {})
    tally = {}
    for k, v in lane.items():
        try:
            role = int(k)
        except (TypeError, ValueError):
            continue
        if role in LANE_NAMES and v.get("games"):
            tally[role] = v["games"]
    total = sum(tally.values())
    if not total:
        return None
    parts = sorted(tally.items(), key=lambda kv: kv[1], reverse=True)
    return " / ".join(f"{LANE_NAMES[r]} {g / total * 100:.0f}%" for r, g in parts if g / total >= 0.05)


def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out




# ---------------------------------------------------------------------------
# BANNIT
# ---------------------------------------------------------------------------

def _strength_score(rg, rw, ag, aw, recent_total) -> float:
    """Yhden pelaajan yhden heropin uhka yhtenä lukuna.

    Kolme asiaa ratkaisee: kuinka usein heroa pelataan juuri nyt (osuus
    pelaajan viimeisimmistä otteluista), kuinka paljon sillä on kokemusta
    kaikkiaan ja kuinka hyvin sillä voitetaan. Voittoprosentti tasoitetaan
    50 %:iin päin, jottei kolmen pelin 100 % nouse listan kärkeen.
    """
    volume = rg / max(recent_total, 1)
    mastery = min(ag, 150) / 150
    wr = (rw + aw + 3) / (rg + ag + 6)
    edge = max(0.6, min(1.4, wr / 0.5))
    return 50 * (6 * volume + 0.3 * mastery) * edge


def player_threats(d):
    """Pelaajan heropit uhkajärjestyksessä.

    [{hero_id, rg, rw, ag, aw, score}] — `rg`/`rw` viimeaikaiset pelit ja
    voitot, `ag`/`aw` kaikkien aikojen. Satunnaiset heropit karsitaan.
    """
    matches = d.get("analyzed") or []
    rg, rw = Counter(), Counter()
    for m in matches:
        hid = m.get("hero_id")
        if not hid:
            continue
        rg[hid] += 1
        if match_is_win(m):
            rw[hid] += 1
    alltime = {h["hero_id"]: (h.get("games", 0), h.get("win", 0))
               for h in (d.get("heroes") or []) if h.get("games")}
    out = []
    for hid in set(rg) | set(alltime):
        a_g, a_w = alltime.get(hid, (0, 0))
        if rg[hid] < MIN_RECENT_FOR_BAN and a_g < MIN_ALLTIME_FOR_BAN:
            continue
        out.append({"hero_id": hid, "rg": rg[hid], "rw": rw[hid],
                    "ag": a_g, "aw": a_w,
                    "score": _strength_score(rg[hid], rw[hid], a_g, a_w,
                                             len(matches))})
    return sorted(out, key=lambda r: -r["score"])


def mmr_weights(players):
    """nick -> pelaajan paino banneissa: (MMR / joukkueen kovin MMR)^eksponentti.

    Bannit keskitetään kovimpiin pelaajiin: eksponentilla 3 tuhannen MMR:n ero
    puolittaa painon suunnilleen. Varapelaajan paino kerrotaan SUB_WEIGHT:llä,
    ja pelaaja jonka MMR:ää ei tiedetä saa joukkueen pienimmän painon.
    """
    top = max((p[1] for p in players if p[1] and not p[3]), default=0)
    raw = {nick: (mmr / top) ** MMR_WEIGHT_EXP if mmr and top else None
           for nick, mmr, _sid, _sub, _role in players}
    floor = min((w for w in raw.values() if w is not None), default=1.0)
    return {nick: (floor if raw[nick] is None else raw[nick])
                  * (SUB_WEIGHT if is_sub else 1.0)
            for nick, _mmr, _sid, is_sub, _role in players}


def team_bans(team, players, data):
    """Joukkueen bannilista paras ensin.

    Heropin bannipisteet = pelaajien uhkien summa MMR-painolla kerrottuna.
    [{hero_id, score, who: [{nick, mmr, sub, rg, rw, ag, aw, part}]}] —
    `who` sisältää vain pelaajat joiden osuus pisteistä on merkittävä.
    """
    weights = mmr_weights(players)
    bans = {}
    for nick, mmr, _sid, is_sub, _role in players:
        for t in player_threats(data.get((team, nick), {})):
            part = weights[nick] * t["score"]
            rec = bans.setdefault(t["hero_id"], {"hero_id": t["hero_id"],
                                                 "score": 0.0, "who": []})
            rec["score"] += part
            rec["who"].append({**t, "nick": nick, "mmr": mmr, "sub": is_sub,
                               "part": part})
    for rec in bans.values():
        rec["who"].sort(key=lambda w: -w["part"])
        rec["who"] = [w for i, w in enumerate(rec["who"])
                      if i == 0 or w["part"] >= BAN_WHO_SHARE * rec["score"]]
    return sorted(bans.values(), key=lambda r: -r["score"])


def games_text(rg, ag) -> str:
    """'12 viim. · 109 kaikkiaan' — tuoreet ensin, koska ne kertovat nykyhetkestä."""
    parts = []
    if rg:
        parts.append(f"{rg} viim.")
    if ag:
        parts.append(f"{ag} kaikkiaan")
    return " · ".join(parts) or "–"


def wr_of(w, g):
    return w / g * 100 if g else None


# ---------------------------------------------------------------------------
# JOUKKUEEN NÄKYMÄ (yhteinen malli Markdownille ja sivustolle)
# ---------------------------------------------------------------------------

def player_view(team, player, data, ban_rank):
    """Kaikki mitä pelaajasta näytetään, valmiiksi laskettuna."""
    nick, mmr, sid, is_sub, role = player
    d = data.get((team, nick), {})
    v = {"nick": nick, "mmr": mmr, "sid": sid, "sub": is_sub, "role": role,
         "error": d.get("error"), "account_id": d.get("account_id")}
    if v["error"]:
        return v
    prof = d.get("profile") or {}
    v["persona"] = (prof.get("profile") or {}).get("personaname")
    v["medal"] = rank_medal(prof.get("rank_tier"))
    v["public"] = has_public_data(d)
    if not v["public"]:
        return v
    wl = d.get("wl") or {}
    v["wl"] = (wl.get("win", 0), wl.get("lose", 0))
    v["form"] = recent_form(d.get("analyzed"))
    v["sample_note"] = d.get("sample_note")
    v["lanes"] = lane_split(d.get("counts"))
    v["recent"] = recent_heroes(d.get("analyzed"))
    v["alltime"] = top_heroes(d.get("heroes"))
    v["targets"] = player_threats(d)[:PLAYER_BAN_COUNT]
    v["ban_rank"] = ban_rank
    return v


def team_view(team, players, data, own_team=None):
    """Joukkueen sivun malli: bannit ja pelaajat MMR-järjestyksessä."""
    bans = team_bans(team, players, data)[:BAN_COUNT]
    ban_rank = {b["hero_id"]: i for i, b in enumerate(bans, 1)}
    order = sorted(players, key=lambda p: (p[3], -(p[1] or 0)))
    mains = [p for p in players if not p[3]]
    mmrs = [p[1] for p in mains if p[1]]
    return {
        "team": team, "slug": slugify(team), "own": team == own_team,
        "bans": bans, "ban_rank": ban_rank,
        "players": [player_view(team, p, data, ban_rank) for p in order],
        "n_mains": len(mains), "n_subs": len(players) - len(mains),
        "avg_mmr": sum(mmrs) / len(mmrs) if mmrs else None,
        "top_mmr": max(mmrs) if mmrs else None,
    }


def fmt_mmr(x) -> str:
    return f"{x:,.0f}".replace(",", " ") if x else "–"


# ---------------------------------------------------------------------------
# PDF-TULOSTUS
# ---------------------------------------------------------------------------

PDF_CSS = """
@page { size: A4; margin: 16mm 14mm; }
body { font-family: "DejaVu Sans", "Liberation Sans", Arial, sans-serif;
       font-size: 9.5pt; line-height: 1.45; color: #14171a; }
h1 { font-size: 20pt; margin: 0 0 4pt; color: #0b1220; }
h2 { font-size: 13pt; margin: 15pt 0 6pt; padding-bottom: 3pt;
     border-bottom: 1.5pt solid #b02a2a; page-break-after: avoid; }
h3 { font-size: 11pt; margin: 12pt 0 3pt; color: #b02a2a;
     page-break-after: avoid; }
p { margin: 4pt 0; }
ul { margin: 4pt 0 4pt 14pt; padding: 0; }
li { margin: 1.5pt 0; }
a { color: #14508c; text-decoration: none; }
code { font-family: "DejaVu Sans Mono", monospace; font-size: 8.5pt;
       background: #f0f2f4; padding: 0 2pt; }
blockquote { margin: 6pt 0; padding: 4pt 8pt; background: #fdf6e3;
             border-left: 3pt solid #d8a531; }
table { border-collapse: collapse; width: 100%; margin: 5pt 0 9pt;
        page-break-inside: avoid; }
th, td { border: 0.5pt solid #c4ccd4; padding: 2.5pt 4pt; text-align: left;
         vertical-align: top; }
th { background: #e8edf2; font-weight: bold; white-space: nowrap; }
td.num, th.num { white-space: nowrap; text-align: right; }
tr:nth-child(even) td { background: #f7f9fb; }
thead { display: table-header-group; }
tr { page-break-inside: avoid; }
"""


def _inline_md(text: str) -> str:
    """Rivinsisäinen Markdown -> HTML (linkit, lihavointi, kursivointi, koodi)."""
    out = html.escape(text, quote=False)
    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"\*([^*\n]+)\*", r"<em>\1</em>", out)
    # kursivointi vain kun _ ei ole sanan sisällä (esim. nick "Tot_Dog")
    out = re.sub(r"(?<![\w*])_([^_\n]+)_(?![\w])", r"<em>\1</em>", out)
    return out




def _col_widths(header, rows):
    """Laskee sarakeleveydet prosentteina sisällön perusteella.

    LibreOffice ei tottele CSS:n `width`- eikä `white-space: nowrap` -sääntöjä,
    vaan jakaa sarakkeet itse — jolloin kapeat otsikot katkeavat keskeltä sanaa
    ("MMR" -> "MM/R"). Se kuitenkin noudattaa HTML:n `width`-attribuuttia, joten
    leveys annetaan sellaisena jokaiselle <th>:lle. Sarake saa vähintään
    pisimmän yksittäisen sanansa verran tilaa ja sen päälle sisällön pituuteen
    suhteutetun osuuden.
    """
    plain = lambda t: re.sub(r"[*_`]|\[|\]\([^)]*\)", "", t)
    ncols = len(header)

    # Sivun tekstileveys A4:llä 14 mm marginaaleilla; merkkileveydet 9,5 pt
    # DejaVu Sansilla, lihavoidulle otsikolle hieman leveämpi. Turvakerroin
    # kattaa solun täytteen ja reunat.
    page_mm, char_mm, bold_mm, pad_mm, safety = 182.0, 2.15, 2.55, 5.0, 1.45

    weights, min_pcts = [], []
    for c in range(ncols):
        head = plain(header[c])
        cells = [plain(r[c]) for r in rows if c < len(r)]
        texts = [head] + cells
        max_len = max((len(t) for t in texts), default=1)
        weights.append(max(len(head) + 2, min(max_len, 45)))
        # kapein leveys jolla pisin yksittäinen sana mahtuu katkeamatta
        # Otsikko saa katketa väliviivasta ("Steam-nimi"), joten sitä ei
        # tarvitse varata kokonaisena; solujen sisältö (esim. päivämäärä
        # "2026-09-02") halutaan sen sijaan pitää yhdellä rivillä.
        head_tokens = [t for w in head.split() for t in w.split("-") if t]
        need_mm = max(
            [len(w) * bold_mm for w in head_tokens]
            + [len(w) * char_mm for t in cells for w in t.split()]
            + [0.0]) + pad_mm
        min_pcts.append(min(need_mm * safety / page_mm * 100, 100.0 / ncols * 2.2))

    total = sum(weights) or 1
    pcts = [w / total * 100 for w in weights]

    # Nosta liian kapeat sarakkeet minimiinsä ja kutista loput suhteessa.
    for _ in range(ncols):
        short = [i for i, p in enumerate(pcts) if p < min_pcts[i] - 1e-9]
        if not short:
            break
        fixed = sum(min_pcts[i] for i in short)
        free = [i for i in range(ncols) if i not in short]
        spare = sum(pcts[i] for i in free)
        if not free or spare <= 0 or fixed >= 100:
            pcts = [min_pcts[i] or 1 for i in range(ncols)]
            break
        scale = (100 - fixed) / spare
        for i in short:
            pcts[i] = min_pcts[i]
        for i in free:
            pcts[i] *= scale

    total_p = sum(pcts) or 1
    return [p / total_p * 100 for p in pcts]



def markdown_to_html(md_text: str, title: str, fragment: bool = False) -> str:
    """Kääntää raportin Markdownin siistiksi tulostettavaksi HTML:ksi.

    Tukee juuri sitä osajoukkoa jota tämä raportti käyttää: otsikot, taulukot,
    listat, lainauslohkot ja rivinsisäisen muotoilun. Ei vaadi ulkoisia
    kirjastoja, jotta skripti toimii ilman asennuksia.
    """
    body, lines = [], md_text.splitlines()
    i, n = 0, len(lines)
    first_h2 = True
    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Taulukko: otsikkorivi + erotinrivi + datarivit
        if (stripped.startswith("|") and i + 1 < n
                and re.fullmatch(r"\|[\s\-:|]+\|", lines[i + 1].strip())):
            header = [c.strip() for c in stripped.strip("|").split("|")]
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append([c.strip()
                             for c in lines[i].strip().strip("|").split("|")])
                i += 1
            widths = _col_widths(header, rows)
            body.append("<table><thead><tr>"
                        + "".join(f'<th width="{w:.1f}%">{_inline_md(c)}</th>'
                                  for c, w in zip(header, widths))
                        + "</tr></thead><tbody>")
            for row in rows:
                body.append("<tr>" + "".join(f"<td>{_inline_md(c)}</td>" for c in row)
                            + "</tr>")
            body.append("</tbody></table>")
            continue

        # Lista
        if stripped.startswith("- "):
            body.append("<ul>")
            while i < n and lines[i].strip().startswith("- "):
                body.append(f"<li>{_inline_md(lines[i].strip()[2:])}</li>")
                i += 1
            body.append("</ul>")
            continue

        # Lainauslohko
        if stripped.startswith(">"):
            chunk = []
            while i < n and lines[i].strip().startswith(">"):
                chunk.append(lines[i].strip().lstrip(">").strip())
                i += 1
            body.append(f"<blockquote>{_inline_md(' '.join(chunk))}</blockquote>")
            continue

        # Otsikot
        m = re.match(r"(#{1,6})\s+(.*)", stripped)
        if m:
            level = len(m.group(1))
            cls = ""
            if level == 2 and first_h2:
                cls, first_h2 = ' class="first"', False
            body.append(f"<h{level}{cls}>{_inline_md(m.group(2))}</h{level}>")
            i += 1
            continue

        body.append(f"<p>{_inline_md(stripped)}</p>")
        i += 1

    if fragment:
        return "\n".join(body)

    return (f"<!DOCTYPE html>\n<html lang=\"fi\"><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(title)}</title><style>{PDF_CSS}</style></head>"
            f"<body>\n" + "\n".join(body) + "\n</body></html>\n")


def _run(cmd, **kw) -> bool:
    try:
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=300, **kw)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def html_to_pdf(html_path: str, pdf_path: str) -> str:
    """Muuntaa HTML:n PDF:ksi ensimmäisellä löytyvällä työkalulla.

    Palauttaa käytetyn työkalun nimen, tai "" jos yhtäkään ei löytynyt.
    """
    out_dir = os.path.dirname(pdf_path) or "."

    if shutil.which("weasyprint") and _run(["weasyprint", html_path, pdf_path]):
        return "weasyprint"

    if shutil.which("wkhtmltopdf") and _run(
            ["wkhtmltopdf", "--enable-local-file-access", "--quiet",
             html_path, pdf_path]):
        return "wkhtmltopdf"

    for chrome in ("chromium", "chromium-browser", "google-chrome", "brave"):
        if shutil.which(chrome) and _run(
                [chrome, "--headless", "--disable-gpu", "--no-sandbox",
                 f"--print-to-pdf={pdf_path}", "--no-pdf-header-footer",
                 f"file://{html_path}"]):
            if os.path.exists(pdf_path):
                return chrome

    if shutil.which("pandoc") and _run(["pandoc", html_path, "-o", pdf_path]):
        return "pandoc"

    for office in ("soffice", "libreoffice"):
        if not shutil.which(office):
            continue
        # LibreOffice kirjoittaa aina <syotteen-nimi>.pdf valittuun hakemistoon
        if _run([office, "--headless", "--norestore",
                 "--convert-to", "pdf:writer_web_pdf_Export",
                 "--outdir", out_dir, html_path]):
            produced = os.path.join(
                out_dir, os.path.splitext(os.path.basename(html_path))[0] + ".pdf")
            if os.path.exists(produced):
                if os.path.abspath(produced) != os.path.abspath(pdf_path):
                    shutil.move(produced, pdf_path)
                return office

    return ""


def write_pdf(md_text: str, pdf_path: str) -> bool:
    """Kirjoittaa raportin PDF:nä. Ei kaada ajoa jos työkalua ei löydy."""
    html_doc = markdown_to_html(md_text, "Turnauksen pelikirja")
    tmpdir = tempfile.mkdtemp(prefix="pelikirja-")
    # LibreOffice nimeää tuloksen syötteen mukaan -> pidetään nimi samana
    html_path = os.path.join(tmpdir, os.path.splitext(os.path.basename(pdf_path))[0] + ".html")
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_doc)
        tool = html_to_pdf(html_path, pdf_path)
        if tool:
            print(f"PDF kirjoitettu ({tool}): {pdf_path}")
            return True
        fallback = os.path.splitext(pdf_path)[0] + ".html"
        shutil.copy(html_path, fallback)
        print("PDF:ää ei voitu luoda — mitään tuettua työkalua ei löytynyt.\n"
              "  Asenna jokin näistä: weasyprint / wkhtmltopdf / chromium / "
              "pandoc / libreoffice\n"
              f"  Tulostusvalmis HTML tallennettiin sen sijaan: {fallback}\n"
              "  (voit avata sen selaimessa ja tulostaa PDF:ksi)")
        return False
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# GITHUB PAGES -SIVUSTO
# ---------------------------------------------------------------------------

SITE_CSS = """
:root {
  --bg: #f4f5f7; --panel: #ffffff; --fg: #1a1d21; --muted: #5f6873;
  --line: #dfe3e8; --accent: #b3261e; --accent-soft: #fbeae8;
  --good: #1e7b3a; --bad: #b3261e; --link: #14508c;
  --warn-bg: #fff8e5; --warn-line: #d8a531;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111418; --panel: #1a1e23; --fg: #e6e9ec; --muted: #98a2ad;
    --line: #2b3138; --accent: #ff7a70; --accent-soft: #3a1f1d;
    --good: #5fd38a; --bad: #ff7a70; --link: #79b8ff;
    --warn-bg: #2a2313; --warn-line: #c9a227;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
}
a { color: var(--link); }
header.top {
  position: sticky; top: 0; z-index: 10; background: var(--panel);
  border-bottom: 1px solid var(--line); padding: 10px 16px;
  display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap;
}
header.top a.home { color: var(--accent); font-weight: 700; text-decoration: none; }
header.top .crumb { color: var(--muted); }
main { max-width: 1100px; margin: 0 auto; padding: 20px 16px 60px; }
h1 { font-size: 28px; line-height: 1.2; margin: 6px 0 4px; }
h2 { font-size: 19px; margin: 32px 0 6px; }
.sub, .note { color: var(--muted); }
.note { font-size: 13px; margin: 0 0 12px; max-width: 70ch; }
.num { font-variant-numeric: tabular-nums; }
.hero-img { display: block; border-radius: 4px; background: var(--line);
            object-fit: cover; flex: none; }
.good { color: var(--good); }
.bad { color: var(--bad); }

/* Bannit */
.bans { display: grid; gap: 10px;
        grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); }
.ban { display: flex; gap: 10px; align-items: flex-start; padding: 10px;
       background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
.ban .rank { font-weight: 800; font-size: 18px; color: var(--accent);
             width: 1.4em; text-align: center; flex: none; line-height: 40px; }
.ban .name { font-weight: 700; }
.ban .who { font-size: 13px; color: var(--muted); line-height: 1.35; }
.ban .who div + div { margin-top: 5px; }
.ban .who b { color: var(--fg); font-weight: 600; }

/* Pelaajakortit */
.players { display: grid; gap: 14px; }
.player { background: var(--panel); border: 1px solid var(--line);
          border-radius: 10px; padding: 14px 16px; }
.player.sub-player { opacity: .85; }
.phead { display: flex; justify-content: space-between; gap: 12px;
         align-items: flex-start; }
.pname { font-size: 20px; font-weight: 700; line-height: 1.2; }
.tag { display: inline-block; font-size: 12px; font-weight: 600; padding: 1px 7px;
       border-radius: 10px; background: var(--bg); color: var(--muted);
       border: 1px solid var(--line); vertical-align: 3px; margin-left: 6px; }
.mmr { text-align: right; flex: none; }
.mmr b { display: block; font-size: 22px; line-height: 1.1; }
.mmr span { font-size: 12px; color: var(--muted); }
.meta { color: var(--muted); font-size: 13px; margin: 4px 0 10px; }
.meta span + span::before { content: " · "; }
.targets { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
           padding: 8px 10px; margin-bottom: 12px; border-radius: 8px;
           background: var(--accent-soft); }
.targets .lbl { font-size: 12px; font-weight: 700; color: var(--accent);
                text-transform: uppercase; letter-spacing: .04em; margin-right: 2px; }
.chip { display: inline-flex; align-items: center; gap: 6px; font-weight: 600;
        font-size: 14px; }
.chip small { font-weight: 400; color: var(--muted); }
.pools { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 24px; }
.pools h3 { font-size: 12px; text-transform: uppercase; letter-spacing: .04em;
            color: var(--muted); margin: 0 0 4px; font-weight: 700; }
.hl { list-style: none; margin: 0; padding: 0; }
.hl li { display: grid; grid-template-columns: 36px 1fr auto 3.2em;
         gap: 8px; align-items: center; padding: 3px 0;
         border-top: 1px solid var(--line); font-size: 14px; }
.hl li:first-child { border-top: 0; }
.hl .g { color: var(--muted); text-align: right; }
.hl .w { text-align: right; }
.bt { font-size: 11px; font-weight: 700; color: var(--accent);
      border: 1px solid var(--accent); border-radius: 4px; padding: 0 4px;
      margin-left: 6px; }
.empty { color: var(--muted); font-style: italic; }

/* Etusivu */
.teams { display: grid; gap: 12px;
         grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); }
.team { display: block; padding: 14px 16px; background: var(--panel);
        border: 1px solid var(--line); border-radius: 10px;
        color: var(--fg); text-decoration: none; }
.team:hover { border-color: var(--accent); }
.team .tn { font-weight: 700; font-size: 17px; }
.team .ts { color: var(--muted); font-size: 13px; margin: 2px 0 10px; }
.team .tb { display: flex; flex-direction: column; gap: 5px; }

.quality { margin-top: 36px; padding: 12px 16px; background: var(--warn-bg);
           border-left: 4px solid var(--warn-line); border-radius: 0 8px 8px 0;
           font-size: 14px; }
.quality h2 { margin-top: 0; font-size: 16px; }
.quality ul { margin: 6px 0 10px 20px; padding: 0; }
footer { max-width: 1100px; margin: 0 auto; padding: 20px 16px 50px;
         color: var(--muted); font-size: 13px; border-top: 1px solid var(--line); }
@media (max-width: 640px) {
  h1 { font-size: 23px; }
  .pools { grid-template-columns: 1fr; }
  .pools > div + div { margin-top: 8px; }
}
"""

HERO_IMG_BASE = ("https://cdn.cloudflare.steamstatic.com/apps/dota2/images/"
                 "dota_react/heroes/")


def git_repo_web_url() -> str:
    """Päättelee GitHub-osoitteen `origin`-remotesta (tyhjä jos ei löydy)."""
    try:
        r = subprocess.run(["git", "-C", HERE, "remote", "get-url", "origin"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=10, text=True)
    except (OSError, subprocess.SubprocessError):
        return ""
    if r.returncode != 0:
        return ""
    url = r.stdout.strip()
    m = re.match(r"(?:https://github\.com/|git@github\.com:)(.+?)(?:\.git)?$", url)
    return f"https://github.com/{m.group(1)}" if m else ""


def site_page(body: str, title: str, crumb: str, depth: int, today: str,
              repo_url: str) -> str:
    root = "../" * depth
    nav = (f'<a class="home" href="{root or "./"}">Turnauksen pelikirja</a>'
           + (f'<span class="crumb">{html.escape(crumb)}</span>' if crumb else ""))
    src = (f' · <a href="{repo_url}">lähdekoodi ja raakadata GitHubissa</a>'
           if repo_url else "")
    return f"""<!DOCTYPE html>
<html lang="fi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{SITE_CSS}</style>
</head>
<body>
<header class="top">{nav}</header>
<main>
{body}
</main>
<footer>Generoitu {today} · data: <a href="https://www.opendota.com/">OpenDota</a>{src}</footer>
</body>
</html>
"""


class HeroFmt:
    """Heropien nimet ja kuvat HTML:ään."""

    def __init__(self, hero_names):
        self.names = hero_names

    def name(self, hid) -> str:
        return html.escape(self.names.get(hid, f"Hero {hid}"))

    def img(self, hid, w) -> str:
        slug = HERO_SLUGS.get(hid)
        h = round(w * 9 / 16)
        if not slug:
            return f'<span class="hero-img" style="width:{w}px;height:{h}px"></span>'
        return (f'<img class="hero-img" src="{HERO_IMG_BASE}{slug}.png" '
                f'width="{w}" height="{h}" alt="" loading="lazy">')


def wr_span(wr) -> str:
    if wr is None:
        return ""
    cls = " good" if wr >= 55 else " bad" if wr <= 45 else ""
    return f'<span class="w num{cls}">{wr:.0f} %</span>'


def ban_cards(bans, hf) -> str:
    out = ['<div class="bans">']
    for i, b in enumerate(bans, 1):
        who = []
        for w in b["who"]:
            wr = wr_of(w["rw"] + w["aw"], w["rg"] + w["ag"])
            who.append(f'<div><b>{html.escape(w["nick"])}</b>'
                       f'{" (vara)" if w["sub"] else ""} · {fmt_mmr(w["mmr"])}'
                       f'<br>{games_text(w["rg"], w["ag"])}'
                       + (f' · {wr:.0f} %' if wr is not None else "") + '</div>')
        out.append(f'<div class="ban"><span class="rank">{i}</span>'
                   f'{hf.img(b["hero_id"], 72)}<div>'
                   f'<div class="name">{hf.name(b["hero_id"])}</div>'
                   f'<div class="who">{"".join(who)}</div></div></div>')
    out.append("</div>")
    return "\n".join(out)


def hero_list(rows, hf, ban_rank) -> str:
    if not rows:
        return '<p class="empty">Ei tarpeeksi pelejä.</p>'
    out = ['<ul class="hl">']
    for hid, g, w in rows:
        tag = (f'<span class="bt" title="Joukkueen bannilistalla sijalla '
               f'{ban_rank[hid]}">B{ban_rank[hid]}</span>' if hid in ban_rank else "")
        out.append(f'<li>{hf.img(hid, 36)}<span>{hf.name(hid)}{tag}</span>'
                   f'<span class="g num">{g}</span>{wr_span(w / g * 100)}</li>')
    out.append("</ul>")
    return "".join(out)


def player_card(v, hf) -> str:
    tags = ""
    if v["sub"]:
        tags += '<span class="tag">varapelaaja</span>'
    if v.get("medal") and v["medal"] != "–":
        tags += f'<span class="tag">{html.escape(v["medal"])}</span>'
    head = (f'<div class="phead"><div><span class="pname">{html.escape(v["nick"])}'
            f'</span>{tags}</div><div class="mmr"><b class="num">'
            f'{fmt_mmr(v["mmr"])}</b><span>MMR</span></div></div>')
    cls = "player sub-player" if v["sub"] else "player"
    if v["error"]:
        return (f'<section class="{cls}">{head}<p class="empty">Steam ID -virhe: '
                f'{html.escape(v["error"])}</p></section>')
    link = (f'<a href="https://www.opendota.com/players/{v["account_id"]}">'
            f'OpenDota</a>')
    if not v["public"]:
        return (f'<section class="{cls}">{head}<p class="empty">Ei julkista dataa '
                f'OpenDotassa — skouttaa käsin. {link}</p></section>')

    meta = []
    if v["persona"] and v["persona"] != v["nick"]:
        meta.append(f'Steam: {html.escape(v["persona"])}')
    if v["role"]:
        meta.append(ROLE_LABELS[v["role"]])
    if v["lanes"]:
        meta.append(html.escape(v["lanes"]))
    if v["form"]:
        w, n, wr, last = v["form"]
        meta.append(f'viim. {n} peliä {w}–{n - w} ({wr:.0f} %)')
        meta.append(f'pelannut viimeksi {last}')
    meta.append(link)
    meta_html = "".join(f"<span>{m}</span>" for m in meta)

    targets = ""
    if v["targets"]:
        chips = "".join(
            f'<span class="chip">{hf.img(t["hero_id"], 48)}{hf.name(t["hero_id"])}'
            f'<small>{games_text(t["rg"], t["ag"])}</small></span>'
            for t in v["targets"])
        targets = f'<div class="targets"><span class="lbl">Bannaa</span>{chips}</div>'

    n_recent = v["form"][1] if v["form"] else 0
    pools = (f'<div class="pools">'
             f'<div><h3>Pelaa nyt · viim. {n_recent} peliä</h3>'
             f'{hero_list(v["recent"], hf, v["ban_rank"])}</div>'
             f'<div><h3>Eniten pelatut kaikkiaan</h3>'
             f'{hero_list(v["alltime"], hf, v["ban_rank"])}</div></div>')
    return (f'<section class="{cls}">{head}<div class="meta">{meta_html}</div>'
            f'{targets}{pools}</section>')


def team_html(view, hf, today, quality_md, repo_url) -> str:
    sub = (f'Keski-MMR {fmt_mmr(view["avg_mmr"])} · {view["n_mains"]} pelaajaa'
           + (f' + {view["n_subs"]} varalla' if view["n_subs"] else "")
           + (" · oma joukkue" if view["own"] else ""))
    B = [f'<h1>{html.escape(view["team"])}</h1><p class="sub">{sub}</p>']
    if view["bans"]:
        B.append(f'<h2>{"Mitä meiltä bannataan" if view["own"] else "Bannit"}</h2>'
                 f'<p class="note">{html.escape(ban_intro(view["own"]))}</p>')
        B.append(ban_cards(view["bans"], hf))
    B.append('<h2>Pelaajat</h2><p class="note">Kovin MMR ensin. '
             '<span class="bt">B1</span> = heropin sija joukkueen bannilistalla. '
             'Voittoprosentti vihreällä kun ≥ 55 %, punaisella kun ≤ 45 %.</p>')
    B.append('<div class="players">')
    B += [player_card(v, hf) for v in view["players"]]
    B.append("</div>")
    if quality_md:
        B.append('<div class="quality">'
                 + markdown_to_html("\n".join(quality_md), "", fragment=True)
                 + "</div>")
    if repo_url:
        B.append(f'<p class="note" style="margin-top:24px">Raakadata: '
                 f'<a href="{repo_url}/tree/main/scouting-results/{view["slug"]}/raw">'
                 f'scouting-results/{view["slug"]}/raw</a></p>')
    return site_page("\n".join(B), f"{view['team']} — pelikirja", view["team"],
                     1, today, repo_url)


def index_html(views, hf, today, quality_md, own_team, repo_url) -> str:
    B = ['<h1>Turnauksen pelikirja</h1>',
         f'<p class="sub">Päivitetty {today}'
         + (f' · näkökulma: {html.escape(own_team)}' if own_team else "") + '</p>',
         '<h2>Joukkueet</h2><p class="note">Keski-MMR:n mukaan. Kortissa '
         'joukkueen kovimmat pelaajat ja kolme ensimmäistä bannia.</p>',
         '<div class="teams">']
    for v in sorted(views, key=lambda v: -(v["avg_mmr"] or 0)):
        stars = [p for p in v["players"] if not p["sub"]][:2]
        bans = "".join(
            f'<span class="chip">{hf.img(b["hero_id"], 40)}{hf.name(b["hero_id"])}</span>'
            for b in v["bans"][:SUMMARY_BAN_COUNT])
        if v["own"] and bans:
            bans = '<span class="ts" style="margin:0">Meiltä bannataan:</span>' + bans
        B.append(f'<a class="team" href="{v["slug"]}/">'
                 f'<div class="tn">{html.escape(v["team"])}'
                 + ('<span class="tag">oma</span>' if v["own"] else "")
                 + f'</div><div class="ts">Keski-MMR {fmt_mmr(v["avg_mmr"])} · '
                 + " · ".join(f'{html.escape(p["nick"])} {fmt_mmr(p["mmr"])}'
                              for p in stars)
                 + f'</div><div class="tb">{bans}</div></a>')
    B.append("</div>")
    if quality_md:
        B.append('<div class="quality">'
                 + markdown_to_html("\n".join(quality_md), "", fragment=True)
                 + "</div>")
    return site_page("\n".join(B), "Turnauksen pelikirja", "", 0, today, repo_url)


def build_site(site_dir: str, views, hf, today: str, repo_url: str,
               quality, own_team):
    """Kirjoittaa staattisen sivuston GitHub Pagesia varten.

    Rakenne:  docs/index.html  ja  docs/<joukkue>/index.html
    `.nojekyll` estää GitHubia ajamasta Jekylliä turhaan.
    `quality` on funktio joukkue|None -> laatuhuomioiden Markdown-rivit.
    """
    os.makedirs(site_dir, exist_ok=True)
    with open(os.path.join(site_dir, ".nojekyll"), "w") as f:
        f.write("")
    with open(os.path.join(site_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html(views, hf, today, quality(None), own_team, repo_url))
    for v in views:
        d = os.path.join(site_dir, v["slug"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(team_html(v, hf, today, quality(v["team"]), repo_url))
    return len(views) + 1


# ---------------------------------------------------------------------------
# PÄÄOHJELMA
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """Joukkueen/pelaajan nimi -> tiedostonimeen kelpaava ASCII-slug."""
    t = name.lower()
    for a, b in (("ä", "a"), ("ö", "o"), ("å", "a"), ("é", "e"),
                 ("ü", "u"), ("ø", "o"), ("æ", "ae")):
        t = t.replace(a, b)
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t or "nimeton"


def intro_lines(today: str):
    """Raporttien yhteinen selitysteksti."""
    return [
        f"_Generoitu {today} · lähde: [OpenDota](https://www.opendota.com/) · "
        f"aineisto: `joukkueet.txt`_",
        "",
    ]


def ban_intro(own: bool) -> str:
    if own:
        return ("Sama laskenta kuin vastustajien sivuilla, käännettynä: nämä "
                "vastustaja todennäköisimmin bannaa meiltä.")
    return (f"Järjestys painottaa kovimpia pelaajia: pelaajan paino on "
            f"(MMR / joukkueen kovin MMR){'²³'[MMR_WEIGHT_EXP - 2] if MMR_WEIGHT_EXP in (2, 3) else f'^{MMR_WEIGHT_EXP}'}, eli kovimman "
            f"pelaajan heropit menevät kärkeen. Pelaajan oma uhka yhdistää "
            f"sen, mitä hän pelaa juuri nyt, kokemuksen heropilla ja "
            f"voittoprosentin.")


def quality_lines(dupes, no_data, bad_ids, mismatches, team=None, level=2):
    """Aineiston laatuhuomiot. `team` rajaa yhden joukkueen omiin huomioihin."""
    keep = lambda t: team is None or t == team
    d_items = [(sid, names) for sid, names in dupes.items()
               if any(keep(t) for t, _ in names)]
    nd = [(t, n) for t, n in no_data if keep(t)]
    bad = [(t, n, e) for t, n, e in bad_ids if keep(t)]
    mm = [(t, n, p, a) for t, n, p, a in mismatches if keep(t)]
    if not (d_items or nd or bad or mm):
        return []

    fmt = (lambda t, n: n) if team else (lambda t, n: f"{t} / {n}")
    h = "#" * level
    L = [f"{h} Huomioita aineiston laadusta", ""]
    if d_items:
        L += ["**Samat Steam ID:t esiintyvät useammalla pelaajalla** — "
              "todennäköisesti kopiointivirhe `joukkueet.txt`:ssä, ja näiden "
              "pelaajien tiedot raportissa ovat siksi epäluotettavia:", ""]
        for sid, names in d_items:
            L.append(f"- `{sid}` → " + ", ".join(fmt(t, n) for t, n in names))
        L.append("")
    if bad:
        L += ["**Steam ID ei jäsenny:**", ""]
        L += [f"- {fmt(t, n)}: {e}" for t, n, e in bad]
        L.append("")
    if nd:
        L += ["**Ei julkista dataa OpenDotassa** (yksityinen profiili tai "
              "Steam ID osoittaa väärään tiliin): "
              + ", ".join(fmt(t, n) for t, n in nd), ""]
    if mm:
        L += ["**Steam-nimi ei muistuta listan nickiä** — yleensä pelaaja on vain "
              "vaihtanut Steam-nimeään, mutta tarkista ettei Steam ID osoita "
              "väärään tiliin:", ""]
        for t, n, p, a in mm:
            L.append(f"- {fmt(t, n)} → Steam-nimi \"{p}\" "
                     f"([profiili](https://www.opendota.com/players/{a}))")
        L.append("")
    return L


def who_text(w) -> str:
    """'Satowi (6000): 12 viim. · 109 kaikkiaan · 67 %'"""
    wr = wr_of(w["rw"] + w["aw"], w["rg"] + w["ag"])
    return (f"{w['nick']}{' (vara)' if w['sub'] else ''} ({fmt_mmr(w['mmr'])}): "
            f"{games_text(w['rg'], w['ag'])}" + (f" · {wr:.0f} %" if wr is not None else ""))


def team_report(view, hero_names, today, quality):
    """Yhden joukkueen Markdown-pelikirja (repoa ja PDF:ää varten)."""
    name = lambda hid: hero_names.get(hid, f"Hero {hid}")
    L = [f"# {view['team']}" + (" (oma joukkue)" if view["own"] else ""), ""]
    L += intro_lines(today)
    L += [f"Keski-MMR **{fmt_mmr(view['avg_mmr'])}** · {view['n_mains']} pelaajaa"
          + (f" + {view['n_subs']} varalla" if view["n_subs"] else ""), ""]

    if view["bans"]:
        L += ["## " + ("Mitä meiltä bannataan" if view["own"] else "Bannit"), "",
              ban_intro(view["own"]), ""]
        L += md_table(["#", "Hero", "Kenen takia"],
                      [[i, f"**{name(b['hero_id'])}**",
                        "; ".join(who_text(w) for w in b["who"])]
                       for i, b in enumerate(view["bans"], 1)])
        L.append("")

    L += ["## Pelaajat (MMR-järjestyksessä)", ""]
    for v in view["players"]:
        head = f"### {v['nick']} — {fmt_mmr(v['mmr'])}"
        if v.get("medal"):
            head += f" · {v['medal']}"
        L += [head + (" · varapelaaja" if v["sub"] else ""), ""]
        if v["error"]:
            L += [f"- Steam ID -virhe: {v['error']}", ""]
            continue
        link = f"[OpenDota](https://www.opendota.com/players/{v['account_id']})"
        if not v["public"]:
            L += [f"- **Ei julkista dataa OpenDotassa** — skouttaa käsin. {link}", ""]
            continue
        facts = []
        if v["persona"] and v["persona"] != v["nick"]:
            facts.append(f"Steam-nimi {v['persona']}")
        if v["role"]:
            facts.append(ROLE_LABELS[v["role"]])
        if v["lanes"]:
            facts.append(v["lanes"])
        if v["form"]:
            w, n, wr, last = v["form"]
            facts.append(f"viim. {n}: {w}–{n - w} ({wr:.0f} %)")
            facts.append(f"viimeisin peli {last}")
        facts.append(link)
        L += ["- " + " · ".join(facts)]
        if v["targets"]:
            L.append("- **Bannikohteet:** " + " · ".join(
                f"{name(t['hero_id'])} ({games_text(t['rg'], t['ag'])})"
                for t in v["targets"]))
        L.append("")
        rows = []
        for i in range(max(len(v["recent"]), len(v["alltime"]))):
            row = []
            for lst in (v["recent"], v["alltime"]):
                if i < len(lst):
                    hid, g, w = lst[i]
                    row += [name(hid), f"{g} · {w / g * 100:.0f} %"]
                else:
                    row += ["", ""]
            rows.append(row)
        if rows:
            L += md_table(["Nyt pelaa", "Pelit · WR", "Kaikkiaan", "Pelit · WR"], rows)
            L.append("")

    L += quality
    return "\n".join(L).rstrip() + "\n"


def index_report(views, hero_names, today, quality, own_team, with_pdf=False):
    """Hakemistosivu: joukkueet, kovimmat pelaajat ja kärkibannit."""
    name = lambda hid: hero_names.get(hid, f"Hero {hid}")
    L = ["# Turnauksen pelikirja", ""]
    L += intro_lines(today)
    if own_team:
        L += [f"Näkökulma: **{own_team}**.", ""]
    rows = []
    for v in sorted(views, key=lambda v: -(v["avg_mmr"] or 0)):
        label = f"[{v['team']}]({v['slug']}/{v['slug']}.md)"
        if v["own"]:
            label += " _(oma)_"
        stars = [p for p in v["players"] if not p["sub"]][:2]
        row = [label, fmt_mmr(v["avg_mmr"]),
               ", ".join(f"{p['nick']} {fmt_mmr(p['mmr'])}" for p in stars),
               ("meiltä: " if v["own"] else "")
               + ", ".join(name(b["hero_id"]) for b in v["bans"][:SUMMARY_BAN_COUNT])]
        if with_pdf:
            row.append(f"[PDF]({v['slug']}/{v['slug']}.pdf)")
        rows.append(row)
    L += md_table(["Joukkue", "Keski-MMR", "Kovimmat", "Kärkibannit"]
                  + (["PDF"] if with_pdf else []), rows)
    L.append("")
    L += quality
    return "\n".join(L).rstrip() + "\n"


def write_raw_data(raw_dir: str, team: str, players, data, today: str) -> int:
    """Kirjoittaa OpenDotan raakavastaukset pelaajittain JSON-tiedostoiksi.

    Tiedosto nimetään nickin ja account_id:n mukaan, joten Steam ID:n
    korjaaminen `joukkueet.txt`:ssä tuottaa uuden tiedoston. Vanha jäisi
    kansioon vanhentuneena ja vääränä datana, joten lopuksi siivotaan pois
    kaikki tiedostot joita tämä ajo ei kirjoittanut.
    """
    os.makedirs(raw_dir, exist_ok=True)
    written = 0
    keep = set()
    for nick, mmr, sid, is_sub, role in players:
        d = data.get((team, nick), {})
        payload = {
            "haettu": d.get("haettu", today),
            "joukkue": team,
            "nick": nick,
            "mmr_listalla": mmr,
            "varapelaaja": is_sub,
            "steam_id": sid,
            "account_id": d.get("account_id"),
            "virhe": d.get("error"),
            "opendota": {k: d.get(k) for k in
                         ("profile", "wl", "heroes", "matches", "counts")},
        }
        acc = d.get("account_id") or "tuntematon"
        name = f"{slugify(nick)}-{acc}.json"
        keep.add(name)
        with open(os.path.join(raw_dir, name), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        written += 1

    for stale in sorted(set(os.listdir(raw_dir)) - keep):
        if stale.endswith(".json"):
            os.remove(os.path.join(raw_dir, stale))
            print(f"  [SIIVOUS] poistettu vanhentunut {stale}")
    return written


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Dota 2 -vastustajaskouttaus: raportit, sivusto ja "
                    "bannilistat OpenDotan datasta.")
    p.add_argument("--pdf", action="store_true",
                   help="kirjoita myös PDF per joukkue (valinnainen)")
    p.add_argument("--oma", metavar="JOUKKUE",
                   default=os.environ.get("OMA_JOUKKUE", ""),
                   help="oma joukkue: merkitään sivustolle, ja sen sivulla näytetään "
                        "mitä meiltä todennäköisesti bannataan. Oletuksena joukkueet.txt:ssä "
                        "merkintä \"(oma)\" otsikon perässä, tai "
                        "ympäristömuuttuja OMA_JOUKKUE.")
    return p.parse_args(argv)


def remove_stale_team_dirs(slugs):
    """Poistaa tulos- ja sivustohakemistoista joukkuekansiot joita ei enää
    ole syötteessä. Kansio tunnistetaan joukkueen omaksi vain jos siinä on
    skriptin itse kirjoittama pelikirja (slug.md tai index.html)."""
    keep = set(slugs)
    for base, marker in ((OUTPUT_DIR, "{slug}.md"), (SITE_DIR, "index.html")):
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if name in keep or not os.path.isdir(path):
                continue
            if os.path.exists(os.path.join(path, marker.format(slug=name))):
                shutil.rmtree(path)
                print(f"  [SIIVOUS] poistettu vanhentunut {os.path.relpath(path, HERE)}/")


def main(argv=None):
    args = parse_args(argv)

    if not os.path.exists(INPUT_FILE):
        sys.exit(f"Syötetiedostoa ei löydy: {INPUT_FILE}")

    teams, own_from_file = parse_teams(INPUT_FILE)
    if not teams:
        sys.exit(f"Yhtään joukkuetta ei löytynyt tiedostosta {INPUT_FILE}")

    own_team, own_err = resolve_own_team(teams, args.oma or own_from_file)
    if own_err:
        sys.exit(f"[VIRHE] --oma: {own_err}")
    if own_team:
        print(f"Oma joukkue: {own_team}")

    hero_names = load_hero_names()
    if not hero_names:
        sys.exit("Hero-nimistöä ei saatu OpenDotasta — API voi olla alhaalla. "
                 "Kokeile myöhemmin uudelleen.")

    # --- Duplikaatti-Steam ID:t syötteessä (kopiointivirheet) ---
    seen = defaultdict(list)
    for team, players in teams:
        for nick, _mmr, sid, _sub, _role in players:
            seen[sid].append((team, nick))
    dupes = {sid: names for sid, names in seen.items() if len(names) > 1}

    # --- Haku ---
    data, no_data, bad_ids, mismatches = {}, [], [], []
    for team, players in teams:
        print(f"\n=== Joukkue: {team} ===")
        for nick, mmr, sid, is_sub, role in players:
            print(f"  Pelaaja: {nick} ({sid})")
            try:
                account_id = steam_id_to_account_id(sid)
            except ValueError as e:
                bad_ids.append((team, nick, str(e)))
                data[(team, nick)] = {"error": str(e)}
                continue
            d = fetch_player(account_id)
            d["account_id"] = account_id
            d["analyzed"], d["sample_note"] = usable_matches(d.get("matches"))
            data[(team, nick)] = d
            if not has_public_data(d):
                no_data.append((team, nick))
            else:
                persona = ((d.get("profile") or {}).get("profile") or {}).get("personaname")
                if persona and not nick_matches_persona(nick, persona):
                    mismatches.append((team, nick, persona, account_id))

    # --- Kirjoitus: yksi kansio per joukkue ---
    today = datetime.date.today().isoformat()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    make_pdf = args.pdf                 # PDF on valinnainen, oletuksena pois
    pdf_ok = pdf_fail = 0
    quality = lambda team: quality_lines(dupes, no_data, bad_ids, mismatches,
                                         team=team, level=2)
    views = []

    print()
    for team, players in teams:
        view = team_view(team, players, data, own_team)
        views.append(view)
        team_dir = os.path.join(OUTPUT_DIR, view["slug"])
        os.makedirs(team_dir, exist_ok=True)

        md_text = team_report(view, hero_names, today, quality(team))
        md_path = os.path.join(team_dir, f"{view['slug']}.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_text)

        n_raw = write_raw_data(os.path.join(team_dir, "raw"), team, players,
                               data, today)
        print(f"{team}: {os.path.relpath(md_path, HERE)} (+{n_raw} raw-tiedostoa)")

        if make_pdf:
            if write_pdf(md_text, os.path.join(team_dir, f"{view['slug']}.pdf")):
                pdf_ok += 1
            else:
                pdf_fail += 1

    index_md = index_report(views, hero_names, today, quality(None), own_team,
                            with_pdf=make_pdf)
    index_path = os.path.join(OUTPUT_DIR, "README.md")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(index_md)

    # --- GitHub Pages -sivusto ---
    n_pages = build_site(SITE_DIR, views, HeroFmt(hero_names), today,
                         git_repo_web_url(), quality, own_team)

    # Joukkueet jotka on poistettu syötteestä jäisivät muuten sivustolle
    # ja tuloksiin vanhentuneina kansioina.
    remove_stale_team_dirs([slugify(t) for t, _ in teams])

    print(f"\nValmis! {len(teams)} joukkuetta -> {os.path.relpath(OUTPUT_DIR, HERE)}/")
    print(f"Hakemistosivu: {os.path.relpath(index_path, HERE)}")
    print(f"Sivusto: {n_pages} sivua -> {os.path.relpath(SITE_DIR, HERE)}/ "
          f"(GitHub Pages: main-haara, /docs)")
    if make_pdf:
        print(f"PDF:t: {pdf_ok} onnistui" + (f", {pdf_fail} epäonnistui" if pdf_fail else ""))
    if no_data:
        print(f"Ilman julkista dataa: {len(no_data)} pelaajaa")


if __name__ == "__main__":
    main()
