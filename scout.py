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

Kun oma joukkue on valittu, jokaiselle vastustajalle lasketaan lisäksi
draft-suunnitelma: bannijärjestys heidän uhkiensa mukaan sekä pick-ehdotukset
omasta heropoolista (mukavuusalue + OpenDotan hero-matchup-data).

KÄYTTÖ
------
    pip install requests
    python3 scout.py              # raportit, raakadata ja verkkosivusto
    python3 scout.py --pdf        # sama + PDF per joukkue (valinnainen)
    python3 scout.py --oma "Joukkueeni"   # draft-suunnitelmat tätä varten

Pelipaikat ilmoitetaan `joukkueet.txt`:ssä neljäntenä kenttänä:

    Nick | MMR | STEAM_0:Y:Z | hard support

Kelpaavat mm. "1".."5", "safelane", "mid", "offlane", "soft support",
"hard support", "kantaja", "keskilinja" (ks. ROLE_ALIASES). Kun pelipaikka
on annettu, pelaajan pick-ehdotukset rajataan siihen sopiviin heropeihin
OpenDotan roolitagien perusteella. Tagit erottavat corit tukipelaajista,
mutta eivät nelosta viitosesta.

Oman joukkueen voi valita kolmella tavalla, tässä järjestyksessä:
    1. lipulla  --oma "Joukkueen nimi"  (osittainen nimi riittää)
    2. ympäristömuuttujalla  OMA_JOUKKUE
    3. merkitsemällä joukkueet.txt:ssä otsikko:  ## Joukkueeni (oma)
Ilman valintaa raportit syntyvät ennallaan, ilman draft-osioita.

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
- Draft-osion matchup-luvut tulevat OpenDotan hero-matchup-datasta, joka
  perustuu ammattilaispeleihin. Otokset ovat pieniä (satoja pelejä paria
  kohden), joten havaittu ero kutistetaan otoskoon mukaan kohti nollaa ja
  pick-järjestyksessä oma mukavuusalue painaa enemmän kuin matchup. Haun voi
  ohittaa lipulla `--ei-matchupeja`.
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
TOP_HERO_COUNT = 8               # montako heropia raporttiin per pelaaja
RECENT_HERO_COUNT = 6            # montako viimeaikaista heropia per pelaaja
MIN_GAMES_FOR_HERO = 3           # jätä pois heropit joita pelattu alle N kertaa
TEAM_SIGNATURE_COUNT = 12        # montako heropia joukkueen yhteispooliin

# Draft-analyysi (pick/ban-ehdotukset oman joukkueen näkökulmasta)
MIN_RECENT_FOR_DRAFT = 2         # väh. näin monta tuoretta peliä...
MIN_ALLTIME_FOR_DRAFT = 15       # ...tai näin monta kaikkiaan, jotta hero huomioidaan
THREAT_POOL = 15                 # montako vastustajan heropia otetaan uhka-analyysiin
DRAFT_BAN_COUNT = 10             # montako bannikohdetta listataan
DRAFT_PICK_COUNT = 12            # montako pick-ehdotusta listataan
PLAYER_PICK_COUNT = 3            # montako ehdotusta per oma pelaaja
CONTESTED_POOL = 25              # kuinka syvältä omaa poolia kiistellyt heropit etsitään
AVOID_COUNT = 5                  # montako vältettävää heropia listataan
SUMMARY_BAN_COUNT = 3            # montako bannia hakemistosivun pikaviitteeseen
AVOID_EDGE_LIMIT = -0.8          # tätä huonompi matchup-etu (pp) = varoitus
MATCHUP_MIN_GAMES = 50           # matchup-pari huomioidaan vasta näin monesta pelistä
MATCHUP_SHRINK = 150             # otoskoon tasoitus: n/(n+tämä) kutistaa etua nollaa kohti
MATCHUP_EDGE_SCALE = 2.5         # ±tämä prosenttiyksikköä = pickki-indeksin ääripäät
PICK_COMFORT_WEIGHT = 0.65       # mukavuusalueen paino pickki-indeksissä (loppu matchupille)
PICK_COMFORT_EXP = 1.5           # >1 korostaa kärkiheropeja satunnaisen historian sijaan

# Yleispätevät pick-ehdotukset omille pelaajille ("all-around")
ALLROUND_COUNT = 5               # montako heropia per oma pelaaja
FIELD_THREAT_POOL = 20           # kuinka monta heropia koko kentän uhkalistalta
PATCH_BRACKETS = (5, 6, 7)       # Legend / Ancient / Divine — bracket 8 on tyhjä
PATCH_EDGE_SCALE = 3.0           # ±tämä prosenttiyksikköä = patch-osuuden ääripäät
PATCH_MIN_PICKS = 2000           # tätä harvinaisemmasta heropista ei lasketa patch-lukua
ALLROUND_COMFORT_WEIGHT = 0.60   # oma mukavuusalue
ALLROUND_FIELD_WEIGHT = 0.22     # matchup koko kenttää vastaan
ALLROUND_PATCH_WEIGHT = 0.18     # heropin yleinen voittoprosentti tässä patchissa
MATCHUP_GIVE_UP_AFTER = 5        # näin monen peräkkäisen epäonnistumisen jälkeen luovutetaan

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

# Pelipaikka -> (ensisijaiset heroroolit, toissijaiset). OpenDotan roolitagit
# erottavat coret tukipelaajista, mutta eivät nelosta viitosesta — molemmat
# ovat "Support". Sitä eroa ei tästä datasta saa, eikä sitä siksi väitetä.
ROLE_TAGS = {
    "pos1": ({"Carry"}, {"Durable", "Escape", "Pusher"}),
    "pos2": ({"Carry"}, {"Nuker", "Escape"}),
    "pos3": ({"Initiator", "Durable"}, {"Carry", "Disabler", "Pusher"}),
    "pos4": ({"Support"}, {"Initiator", "Escape", "Nuker"}),
    "pos5": ({"Support"}, {"Disabler", "Nuker"}),
}
ROLE_FIT = (1.0, 0.55, 0.2)      # ensisijainen / toissijainen / ei sovi
ROLE_OFF_TAGS = {"pos1": "Support", "pos2": "Support", "pos3": "Support"}

RANK_NAMES = {1: "Herald", 2: "Guardian", 3: "Crusader", 4: "Archon",
              5: "Legend", 6: "Ancient", 7: "Divine", 8: "Immortal"}
LANE_NAMES = {1: "Safe", 2: "Mid", 3: "Off", 4: "Jungle"}


# ---------------------------------------------------------------------------
# SYÖTTEEN LUKU
# ---------------------------------------------------------------------------

OWN_TEAM_MARKER = re.compile(r"\s*\((oma|own)\)\s*$", re.IGNORECASE)


def parse_teams(path: str):
    """Lukee `joukkueet.txt`:n.

    Palauttaa ([(joukkue, [(nick, mmr, steam_id, on_sub, rooli), ...]), ...], oma)
    jossa `oma` on tiedostossa omaksi merkitty joukkue tai None.

    Rivimuoto:  Nick | MMR | STEAM_0:Y:Z [| pelipaikka]   (erotin | tai /)
    Sulkeissa oleva rivi tulkitaan varapelaajaksi: (Nick | MMR | STEAM_...)
    Otsikon perässä `(oma)` merkitsee oman joukkueen:  ## Joukkueeni (oma)

    Neljäs kenttä on valinnainen pelipaikka: "1".."5", "mid", "offlane",
    "hard support", "kantaja", ... (ks. ROLE_ALIASES). Kun se on annettu,
    pick-ehdotukset rajataan pelipaikkaan sopiviin heropeihin.
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
            parts = re.split(r"\s*[|/]\s*", line.strip("()").strip())
            if len(parts) not in (3, 4):
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


def load_hero_names() -> dict:
    """hero_id -> hero-nimi."""
    print("Haetaan hero-nimistöä OpenDotasta...")
    data = api_get("/heroes")
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


def top_heroes(hero_stats, hero_names, top_n=TOP_HERO_COUNT):
    """[(nimi, pelit, voitot, wr%)] kaikkien aikojen datasta."""
    if not hero_stats:
        return []
    rows = [h for h in hero_stats if h.get("games", 0) >= MIN_GAMES_FOR_HERO]
    rows.sort(key=lambda h: (h["games"], h["win"]), reverse=True)
    out = []
    for h in rows[:top_n]:
        games, wins = h["games"], h["win"]
        out.append((hero_names.get(h["hero_id"], f"Hero {h['hero_id']}"),
                    games, wins, wins / games * 100 if games else 0.0))
    return out


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


def recent_heroes(matches, hero_names, top_n=RECENT_HERO_COUNT):
    """Viimeaikaiset suosikkiheropit: [(nimi, pelit, voitot, wr%)]."""
    if not matches:
        return []
    games, wins = Counter(), Counter()
    for m in matches:
        hid = m.get("hero_id")
        if not hid:
            continue
        games[hid] += 1
        if match_is_win(m):
            wins[hid] += 1
    out = []
    for hid, g in games.most_common(top_n):
        out.append((hero_names.get(hid, f"Hero {hid}"), g, wins[hid], wins[hid] / g * 100))
    return out


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
# DRAFT: UHKA-ANALYYSI JA PICK/BAN-EHDOTUKSET
# ---------------------------------------------------------------------------

def _strength_score(rg, rw, ag, aw, team_recent) -> float:
    """Yhden pelaajan yhden heropin "voima" yhtenä lukuna.

    Kolme asiaa ratkaisee: kuinka usein heroa pelataan juuri nyt (volyymi),
    kuinka paljon sillä on kokemusta kaikkiaan (rutiini) ja kuinka hyvin sillä
    voitetaan. Voittoprosentti tasoitetaan 50 %:iin päin, jottei kolmen pelin
    100 % nouse listan kärkeen.
    """
    volume = rg / max(team_recent, 1)
    mastery = min(ag, 150) / 150
    wr = (rw + aw + 3) / (rg + ag + 6)
    edge = max(0.6, min(1.4, wr / 0.5))
    return 50 * (6 * volume + 0.6 * mastery) * edge


def hero_strengths(team, players, data):
    """Yhden joukkueen heropit voimakkuusjärjestyksessä."""
    return hero_strengths_multi([(team, players)], data)


def hero_strengths_multi(rosters, data):
    """Yhden tai useamman joukkueen heropit voimakkuusjärjestyksessä.

    Sama laskenta kelpaa molempiin suuntiin: vastustajalle se on uhka-arvio
    (mitä he pickkaavat ja millä he voittavat), omalle joukkueelle se on
    mukavuusalueen kartoitus (mitä me osaamme pelata).

    Palauttaa listan tietueita paras ensin:
        {hero_id, score, rg, rw, ag, aw, wr, players: [{nick, sub, ...}]}
    joissa `rg`/`rw` ovat viimeaikaiset pelit ja voitot, `ag`/`aw` kaikkien
    aikojen vastaavat. Usealla rosterilla kutsuttuna tuloksena on koko
    kentän yhteinen uhkalista.
    """
    per_player, team_recent = [], 0
    for team, players in rosters:
        for nick, _mmr, _sid, is_sub, role in players:
            d = data.get((team, nick), {})
            matches = d.get("analyzed") or []
            team_recent += len(matches)
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
            per_player.append((team, nick, is_sub, role, rg, rw, alltime))

    out = {}
    for team, nick, is_sub, role, rg, rw, alltime in per_player:
        for hid in set(rg) | set(alltime):
            r_g, r_w = rg.get(hid, 0), rw.get(hid, 0)
            a_g, a_w = alltime.get(hid, (0, 0))
            if r_g < MIN_RECENT_FOR_DRAFT and a_g < MIN_ALLTIME_FOR_DRAFT:
                continue
            rec = out.setdefault(hid, {"hero_id": hid, "rg": 0, "rw": 0,
                                       "ag": 0, "aw": 0, "players": []})
            rec["rg"] += r_g
            rec["rw"] += r_w
            rec["ag"] += a_g
            rec["aw"] += a_w
            rec["players"].append({
                "nick": nick, "team": team, "sub": is_sub, "role": role,
                "rg": r_g, "rw": r_w,
                "ag": a_g, "aw": a_w,
                "wr": (r_w + a_w) / (r_g + a_g) * 100 if r_g + a_g else 0.0,
                "score": _strength_score(r_g, r_w, a_g, a_w, team_recent),
            })

    for rec in out.values():
        rec["score"] = _strength_score(rec["rg"], rec["rw"], rec["ag"],
                                       rec["aw"], team_recent)
        games = rec["rg"] + rec["ag"]
        rec["wr"] = (rec["rw"] + rec["aw"]) / games * 100 if games else 0.0
        rec["players"].sort(key=lambda p: -p["score"])
    return sorted(out.values(), key=lambda r: -r["score"])


def fetch_matchups(hero_ids):
    """hero_id -> {vastahero_id: (pelit, voitot)} OpenDotan matchup-datasta.

    `voitot` on kyselyheropin voitot vastaheroa vastaan, eli suoraan
    vastakkainasettelun voittoprosentti kyselyheropin näkökulmasta. Data on
    ammattilaispeleistä, joten otokset ovat pieniä (tyypillisesti satoja
    pelejä paria kohden) — ks. `matchup_edge`.
    """
    ids = sorted(set(hero_ids))
    if not ids:
        return {}
    print(f"\nHaetaan heromatchup-dataa ({len(ids)} heropia)...")
    out, misses = {}, 0
    for hid in ids:
        path = f"/heroes/{hid}/matchups"
        cached = os.path.exists(_cache_path(path, {}))
        rows = api_get(path)
        if not cached:
            time.sleep(REQUEST_DELAY)
        if not rows:
            misses += 1
            # Peräkkäiset epäonnistumiset tarkoittavat käytännössä aina
            # katkennutta yhteyttä. Ei jäädä yrittämään kymmeniä kertoja.
            if misses >= MATCHUP_GIVE_UP_AFTER and not out:
                print(f"  [VAROITUS] {misses} peräkkäistä epäonnistunutta "
                      f"matchup-hakua — jatketaan ilman matchup-dataa.")
                return {}
            continue
        misses = 0
        out[hid] = {r["hero_id"]: (r.get("games_played", 0), r.get("wins", 0))
                    for r in rows if isinstance(r, dict) and r.get("games_played")}
    return out


def matchup_edge(hero_id, threats, matchups):
    """Kuinka hyvin `hero_id` pärjää vastustajan uhkaheropeille.

    Painotettu vastustajan uhkaindeksillä: iso etu vastustajan tärkeintä
    heroa vastaan painaa enemmän kuin etu heroa vastaan jota tuskin nähdään.

    OpenDotan matchup-otokset ovat pieniä, joten havaittu ero kutistetaan
    kohti nollaa otoskoon mukaan (`n / (n + MATCHUP_SHRINK)`): sadan pelin
    otoksesta jää noin 40 % ja tuhannen pelin otoksesta lähes kaikki. Näin
    yksittäinen pieni otos ei nosta heroa listan kärkeen.

    Palauttaa (etu prosenttiyksikköinä tai None, [(vastahero_id, etu), ...]).
    """
    num = den = 0.0
    details = []
    for t in threats:
        pair = matchups.get(t["hero_id"], {}).get(hero_id)
        if not pair:
            continue
        games, wins = pair
        if games < MATCHUP_MIN_GAMES:
            continue
        adv = (0.5 - wins / games) * 100   # + = meidän hero voittaa heitä
        adv *= games / (games + MATCHUP_SHRINK)
        weight = max(t["score"], 0.1)
        num += weight * adv
        den += weight
        details.append((t["hero_id"], adv))
    if not den:
        return None, []
    details.sort(key=lambda d: -d[1])
    return num / den, details


def _pick_index(score, best, edge) -> float:
    """Pickki-indeksi: oma mukavuusalue + vastakkainasetteluetu, 0-100.

    Mukavuus painaa selvästi enemmän — turnauksessa pelataan sitä mitä
    osataan pelata, eikä teoreettisesti hyvä matchup korvaa harjoittelua.
    Eksponentti korostaa oikeaa kärkeä, jottei kauan sitten pelattu hero
    nouse listan huipulle pelkän matchup-edun voimalla.
    """
    comfort = min(score / best, 1.0) ** PICK_COMFORT_EXP
    if edge is None:
        return 100 * comfort
    m = max(-1.0, min(1.0, edge / MATCHUP_EDGE_SCALE))
    return 100 * (PICK_COMFORT_WEIGHT * comfort
                  + (1 - PICK_COMFORT_WEIGHT) * (0.5 + 0.5 * m))


def pick_candidates(own_strengths, threats, matchups, hero_stats=None):
    """Omat heropit paremmuusjärjestyksessä tätä vastustajaa vastaan.

    Kun pelipaikat on ilmoitettu, heropi arvioidaan sen pelaajan mukaan
    jolle se parhaiten sopii — joukkueessa on viisi eri paikkaa, joten
    heropin ei tarvitse sopia kaikille.
    """
    hero_stats = hero_stats or {}
    best = max((r["score"] for r in own_strengths), default=0.0) or 1.0
    out = []
    for rec in own_strengths:
        edge, details = matchup_edge(rec["hero_id"], threats, matchups)
        fit = max((role_fit(p["role"], rec["hero_id"], hero_stats)
                   for p in rec["players"]), default=1.0)
        out.append(dict(rec, edge=edge, vs=details, fit=fit,
                        pick=_pick_index(rec["score"], best, edge) * fit))
    out.sort(key=lambda r: -r["pick"])
    return out


def _pp(x) -> str:
    """Prosenttiyksikköetu suomalaisittain: +4,1 pp."""
    return f"{x:+.1f}".replace(".", ",") + " pp"


def _pct1(x) -> str:
    """Prosentti yhdellä desimaalilla suomalaisittain: 52,9 %."""
    return f"{x:.1f}".replace(".", ",") + "%"


def _who_plays(rec) -> str:
    """"Kuka pelaa" -solu: nick ja tuoreet pelit, tai kokemus jos ei tuoreita."""
    bits = []
    for p in rec["players"][:4]:
        tag = f"{p['nick']}" + (" (sub)" if p["sub"] else "")
        if p["rg"]:
            bits.append(f"{tag} {p['rg']}")
        else:
            bits.append(f"{tag} –/{p['ag']}")
    return ", ".join(bits)


def ban_table(strengths, hero_names, count):
    """Bannijärjestystaulukko uhkaindeksin mukaan."""
    rows = []
    for i, rec in enumerate(strengths[:count], 1):
        rows.append([i, f"**{hero_names.get(rec['hero_id'], rec['hero_id'])}**",
                     f"{rec['score']:.0f}", rec["rg"], rec["ag"],
                     f"{rec['wr']:.0f}%", _who_plays(rec)])
    return md_table(["#", "Hero", "Uhka", "Viim.", "Kaikkiaan", "WR",
                     "Kuka pelaa"], rows)


def draft_plan_lines(opp_team, opp_strengths, own_team, own_strengths,
                     hero_names, matchups, hero_stats=None, level=2):
    """Draft-suunnitelma yhtä vastustajaa vastaan oman joukkueen kannalta."""
    h = "#" * level
    threats = opp_strengths[:THREAT_POOL]
    if not threats:
        return [f"{h} 🎯 Draft: {own_team} vs. {opp_team}", "",
                "Vastustajasta ei ole tarpeeksi julkista pelidataa "
                "draft-suunnitelmaa varten.", ""]

    L = [f"{h} 🎯 Draft: {own_team} vs. {opp_team}", "",
         f"Bannit vastustajan uhkaindeksin mukaan, pickit oman joukkueen "
         f"heropoolista. Uhkaindeksi yhdistää viimeaikaisen pelivolyymin, "
         f"kaikkien aikojen kokemuksen ja voittoprosentin — suurempi on "
         f"vaarallisempi. **Viim.** = pelit viimeisimmissä otteluissa, "
         f"**Kaikkiaan** = pelit kaikkiaan. Sarakkeessa *Kuka pelaa* luku on "
         f"pelaajan tuoreet pelit kyseisellä heropilla (`–/N` = ei tuoreita, "
         f"N peliä historiassa).", ""]

    L += [f"{h}# Bannit — tässä järjestyksessä", ""]
    L += ban_table(opp_strengths, hero_names, DRAFT_BAN_COUNT)
    L.append("")

    if not own_strengths:
        L += ["_Omasta joukkueesta ei ole julkista pelidataa, joten "
              "pick-ehdotuksia ei voi laskea._", ""]
        return L

    hero_stats = hero_stats or {}
    cands = pick_candidates(own_strengths, threats, matchups, hero_stats)
    have_edges = any(c["edge"] is not None for c in cands)

    L += [f"{h}# Pickit — omasta poolista tätä vastaan", ""]
    if have_edges:
        L += [f"**Etu** on painotettu voittoprosenttiero vastustajan "
              f"uhkaheropeille OpenDotan ammattilaispelidatassa (väh. "
              f"{MATCHUP_MIN_GAMES} peliä paria kohden, ja lukua on "
              f"kutistettu otoskoon mukaan). Otokset ovat pieniä ja "
              f"ammattilaispelit eri peliä kuin amatööriturnaus, joten tämä "
              f"on karkea suuntaviiva — oma mukavuusalue painaa enemmän.", ""]
    else:
        L += ["_Matchup-dataa ei ole käytettävissä, joten ehdotukset "
              "perustuvat pelkkään omaan mukavuusalueeseen._", ""]

    rows = []
    for rec in cands[:DRAFT_PICK_COUNT]:
        row = [f"**{hero_names.get(rec['hero_id'], rec['hero_id'])}**",
               f"{rec['pick']:.0f}", _who_plays(rec), f"{rec['wr']:.0f}%"]
        if have_edges:
            row.append(_pp(rec["edge"]) if rec["edge"] is not None else "–")
            row.append(", ".join(
                f"{hero_names.get(hid, hid)} {_pp(adv)}"
                for hid, adv in rec["vs"][:2]) or "–")
        rows.append(row)
    headers = ["Hero", "Pickki", "Kuka meiltä", "WR"]
    if have_edges:
        headers += ["Etu vs. uhat", "Toimii erityisesti vastaan"]
    L += md_table(headers, rows)
    L.append("")

    # Pelaajakohtaiset ehdotukset: kunkin oman pelaajan parhaat heropit.
    # Järjestys lasketaan pelaajan omasta poolista, ei joukkueen yhteisestä —
    # muuten kaikille suositeltaisiin samoja heropeja.
    per_player = defaultdict(list)
    for rec in cands:
        for p in rec["players"]:
            per_player[p["nick"]].append(
                (rec, p, role_fit(p["role"], rec["hero_id"], hero_stats)))
    if per_player:
        L += [f"{h}# Pelaajakohtaisesti", "",
              "Kunkin oman pelaajan omasta poolista parhaat vaihtoehdot "
              "tätä vastustajaa vastaan:", ""]
        for nick in sorted(per_player):
            items = per_player[nick]
            fitting = [p["score"] for _r, p, f in items if f >= ROLE_FIT[1]]
            best = max(fitting or [p["score"] for _r, p, _f in items]) or 1.0
            rank = lambda ip: _pick_index(ip[1]["score"], best, ip[0]["edge"]) * ip[2]
            bits = []
            for rec, p, _fit in sorted(items, key=rank,
                                       reverse=True)[:PLAYER_PICK_COUNT]:
                name = hero_names.get(rec["hero_id"], rec["hero_id"])
                games = p["rg"] + p["ag"]
                s = f"**{name}** ({games} peliä, {p['wr']:.0f}%"
                if rec["edge"] is not None:
                    s += f", {_pp(rec['edge'])}"
                bits.append(s + ")")
            L.append(f"- **{nick}**: " + " · ".join(bits))
        L.append("")

    # Kiistellyt: heropit joita molemmat haluavat
    opp_ids = {r["hero_id"]: r for r in threats}
    contested = [c for c in cands[:CONTESTED_POOL] if c["hero_id"] in opp_ids]
    if contested:
        L += [f"{h}# Kiistellyt heropit", "",
              "Näitä haluavat molemmat. Jos et banni, varaudu siihen että "
              "vastustaja ottaa ne — tai pickkaa itse ensin:", ""]
        L += md_table(["Hero", "Meillä", "Heillä", "Heidän uhkansa"],
                      [[f"**{hero_names.get(c['hero_id'], c['hero_id'])}**",
                        _who_plays(c), _who_plays(opp_ids[c["hero_id"]]),
                        f"{opp_ids[c['hero_id']]['score']:.0f}"]
                       for c in contested])
        L.append("")

    # Vältettävät: oman poolin heropit joilla on selvä miinusmatchup
    if have_edges:
        weak = [c for c in cands
                if c["edge"] is not None and c["edge"] <= AVOID_EDGE_LIMIT]
        weak.sort(key=lambda c: c["edge"])
        weak = [c for c in weak if c["score"] >= own_strengths[0]["score"] * 0.25]
        if weak:
            L += [f"{h}# Varo näitä ensimmäisillä pickeillä", "",
                  "Oman poolin heropit jotka pärjäävät heikoiten juuri tätä "
                  "vastustajaa vastaan:", ""]
            L += md_table(["Hero", "Kuka meiltä", "Etu vs. uhat", "Kärsii vastaan"],
                          [[hero_names.get(c["hero_id"], c["hero_id"]),
                            _who_plays(c), _pp(c["edge"]),
                            ", ".join(f"{hero_names.get(hid, hid)} {_pp(adv)}"
                                      for hid, adv in c["vs"][-2:]) or "–"]
                           for c in weak[:AVOID_COUNT]])
            L.append("")
    return L


def fetch_hero_stats() -> dict:
    """hero_id -> {"wr": voitto-% tässä patchissa, "picks": otos, "roles": [...]}.

    Voittoprosentti lasketaan bracketeista Legend-Divine (`PATCH_BRACKETS`),
    jotka vastaavat amatööriturnauksen tasoa. Immortal-bracket on OpenDotan
    datassa tyhjä, joten sitä ei käytetä.
    """
    print("Haetaan heropien patch-tilastoja OpenDotasta...")
    rows = api_get("/heroStats")
    out = {}
    for h in (rows or []):
        picks = sum(h.get(f"{b}_pick") or 0 for b in PATCH_BRACKETS)
        wins = sum(h.get(f"{b}_win") or 0 for b in PATCH_BRACKETS)
        if picks < PATCH_MIN_PICKS:
            continue
        out[h["id"]] = {"wr": wins / picks * 100, "picks": picks,
                        "roles": h.get("roles") or []}
    return out


def role_fit(role, hero_id, hero_stats):
    """Kuinka hyvin heropi sopii pelaajan pelipaikkaan: 1.0 / 0.55 / 0.2.

    Perustuu OpenDotan roolitageihin, jotka erottavat luotettavasti corit
    tukipelaajista. Tagit eivät erota nelosta viitosesta — molemmat ovat
    "Support" — joten niiden välillä tämä ei ota kantaa. Ilman ilmoitettua
    pelipaikkaa tai roolidataa palautetaan 1.0, jolloin mikään ei muutu.
    """
    if not role:
        return 1.0
    tags = set((hero_stats.get(hero_id) or {}).get("roles") or [])
    if not tags:
        return 1.0
    primary, secondary = ROLE_TAGS[role]
    off = ROLE_OFF_TAGS.get(role)
    if tags & primary and not (off and off in tags):
        return ROLE_FIT[0]
    if tags & secondary and not (off and off in tags):
        return ROLE_FIT[1]
    return ROLE_FIT[2]


def fit_symbol(fit) -> str:
    """Sopivuus taulukkoon: ✓ / ~ / ✗."""
    return "✓" if fit >= ROLE_FIT[0] else ("~" if fit >= ROLE_FIT[1] else "✗")


def role_tags_display(role, hero_id, hero_stats, limit=2) -> str:
    """Heropin roolitagit niin, että pelipaikkaan osuneet näkyvät ensin.

    Taulukkoon mahtuu vain pari tagia, joten järjestys ratkaisee: muuten
    ✓ näyttäisi virheeltä kun sen perusteena oleva tagi jää pois näkyvistä
    (esim. Sand Kingin "Support" on vasta kolmantena).
    """
    tags = (hero_stats.get(hero_id) or {}).get("roles") or []
    if not tags:
        return "–"
    if role:
        primary, secondary = ROLE_TAGS[role]
        tags = sorted(tags, key=lambda t: (t not in primary, t not in secondary))
    return ", ".join(tags[:limit])


def allround_index(score, best, field_edge, patch_edge) -> float:
    """Yleispätevän pickin indeksi 0-100.

    Kolme signaalia: pelaajan oma mukavuusalue, heropin pärjääminen koko
    turnauskentän uhkaheropeille ja heropin yleinen voittoprosentti tässä
    patchissa. Puuttuvan signaalin paino jaetaan muille, jottei heropi
    rankaistu siitä ettei siitä satu olemaan dataa.
    """
    terms = [(ALLROUND_COMFORT_WEIGHT,
              min(score / best, 1.0) ** PICK_COMFORT_EXP)]
    if field_edge is not None:
        m = max(-1.0, min(1.0, field_edge / MATCHUP_EDGE_SCALE))
        terms.append((ALLROUND_FIELD_WEIGHT, 0.5 + 0.5 * m))
    if patch_edge is not None:
        m = max(-1.0, min(1.0, patch_edge / PATCH_EDGE_SCALE))
        terms.append((ALLROUND_PATCH_WEIGHT, 0.5 + 0.5 * m))
    total = sum(w for w, _ in terms)
    return 100 * sum(w * v for w, v in terms) / total


def allround_picks(own_strengths, field_threats, matchups, hero_stats):
    """Oma pelaaja -> [(hero-tietue, pelaajan osuus, indeksi)] paras ensin.

    Ei sidottu yhteen vastustajaan: kenttäetu lasketaan kaikkien
    vastustajien uhkaheropeista yhtenä joukkona.
    """
    edges, out = {}, defaultdict(list)
    for rec in own_strengths:
        hid = rec["hero_id"]
        if hid not in edges:
            edges[hid] = matchup_edge(hid, field_threats, matchups)[0]
        stat = hero_stats.get(hid)
        patch = stat["wr"] - 50 if stat else None
        for p in rec["players"]:
            out[p["nick"]].append((rec, p, edges[hid], patch,
                                   role_fit(p["role"], hid, hero_stats)))

    ranked = {}
    for nick, items in out.items():
        # Vertailukohta lasketaan pelipaikkaan sopivista heropeista, jotta
        # off-role-hero ei jää kärkeen vain siksi että sitä on pelattu paljon.
        fitting = [p["score"] for _r, p, _e, _q, f in items if f >= ROLE_FIT[1]]
        best = max(fitting or [p["score"] for _r, p, _e, _q, _f in items]) or 1.0
        scored = [(rec, p, edge, patch, fit,
                   allround_index(p["score"], best, edge, patch) * fit)
                  for rec, p, edge, patch, fit in items]
        scored.sort(key=lambda t: -t[5])
        ranked[nick] = scored
    return ranked


def allround_lines(own_team, own_players, own_strengths, field_threats,
                   matchups, hero_stats, hero_names, level=2):
    """Kunkin oman pelaajan yleispätevimmät heropit koko turnausta vastaan."""
    h = "#" * level
    ranked = allround_picks(own_strengths, field_threats, matchups, hero_stats)
    if not ranked:
        return []

    have_field = any(t[2] is not None for v in ranked.values() for t in v)
    have_patch = any(t[3] is not None for v in ranked.values() for t in v)
    have_roles = any(p[4] for p in own_players)

    L = [f"{h} ⭐ Kunkin pelaajan {ALLROUND_COUNT} vahvinta heroa", "",
         f"Yleispätevät pickit koko turnauskenttää vastaan — nämä eivät ole "
         f"sidottuja yhteen vastustajaan, vaan kelpaavat lähtökohdaksi ketä "
         f"tahansa vastaan. Indeksi yhdistää kolme asiaa: pelaajan oman "
         f"mukavuusalueen (paino {ALLROUND_COMFORT_WEIGHT:.0%}), heropin "
         f"pärjäämisen kaikkien vastustajien uhkaheropeille "
         f"({ALLROUND_FIELD_WEIGHT:.0%}) ja heropin yleisen voittoprosentin "
         f"tässä patchissa ({ALLROUND_PATCH_WEIGHT:.0%}).", ""]
    if have_roles:
        L += ["Ehdotukset on rajattu pelaajan **pelipaikkaan** sopiviin "
              "heropeihin: sarake *Sopii* on ✓ kun heropin roolitagit "
              "vastaavat pelipaikkaa, ~ kun ne sopivat osittain ja ✗ kun "
              "eivät sovi. Roolitagit erottavat corit tukipelaajista, mutta "
              "eivät nelosta viitosesta — soft ja hard support saavat siis "
              "saman kohtelun.", ""]

    headers = ["Pelaaja", "Pelipaikka", "Hero", "Rooli", "Sopii", "Pelit", "Oma WR"]
    if not have_roles:
        headers = ["Pelaaja", "Hero", "Rooli", "Pelit", "Oma WR"]
    if have_patch:
        headers.append("Patch-WR")
    if have_field:
        headers.append("Kenttäetu")
    headers.append("Indeksi")

    rows, missing = [], []
    for nick, _mmr, _sid, is_sub, role in own_players:
        items = ranked.get(nick)
        if not items:
            missing.append(nick)
            continue
        label = f"**{nick}**" + (" _(sub)_" if is_sub else "")
        role_label = ROLE_LABELS.get(role, "–")
        for rec, p, edge, patch, fit, idx in items[:ALLROUND_COUNT]:
            row = [label]
            if have_roles:
                row.append(role_label)
            row += [hero_names.get(rec["hero_id"], rec["hero_id"]),
                    role_tags_display(role, rec["hero_id"], hero_stats)]
            if have_roles:
                row.append(fit_symbol(fit))
            row += [p["rg"] + p["ag"], f"{p['wr']:.0f}%"]
            if have_patch:
                row.append(_pct1(patch + 50) if patch is not None else "–")
            if have_field:
                row.append(_pp(edge) if edge is not None else "–")
            row.append(f"{idx:.0f}")
            rows.append(row)
            label = role_label = ""   # nimi vain lohkon ensimmäiselle riville
    if not rows:
        return []

    L += md_table(headers, rows)
    L.append("")
    L += ["**Pelit** on pelaajan omat pelit heropilla (tuoreet + kaikkien "
          "aikojen), **Oma WR** hänen voittoprosenttinsa sillä. "
          + ("**Patch-WR** on heropin voittoprosentti kaikilla pelaajilla "
             "bracketeissa Legend-Divine. " if have_patch else "")
          + ("**Kenttäetu** on painotettu voittoprosenttiero turnauksen "
             "uhkaheropeille ammattilaisdatassa." if have_field else ""), ""]
    if missing:
        L += [f"_Ei riittävästi julkista pelidataa: {', '.join(missing)}._", ""]
    return L


def own_team_lines(own_team, own_strengths, hero_names, level=2):
    """Oman joukkueen sivulle: mitä meiltä todennäköisesti bannataan."""
    h = "#" * level
    if not own_strengths:
        return []
    return ([f"{h} 🪞 Oma joukkue — mitä meiltä bannataan", "",
             "Tämä on **oma joukkueesi**, joten draft-suunnitelmien sijaan "
             "tässä sama uhka-analyysi käännettynä: näin oma poolisi näyttää "
             "vastustajan skoutille, eli tästä päästä bannit todennäköisesti "
             "tulevat. Varmista että kärjen takana on vaihtoehtoja.", ""]
            + ban_table(own_strengths, hero_names, DRAFT_BAN_COUNT) + [""])


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
  --bg: #ffffff; --fg: #1a1d21; --muted: #5c6670; --line: #d8dee4;
  --accent: #b02a2a; --card: #f6f8fa; --thead: #eef2f6; --zebra: #fafbfc;
  --link: #14508c; --warn-bg: #fff8e5; --warn-line: #d8a531;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171a; --fg: #e6e9ec; --muted: #9aa4ae; --line: #2c3238;
    --accent: #ff6b6b; --card: #1b1f23; --thead: #21262c; --zebra: #191d21;
    --link: #79b8ff; --warn-bg: #2a2313; --warn-line: #c9a227;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        "Helvetica Neue", Arial, sans-serif;
}
header.top {
  position: sticky; top: 0; z-index: 10;
  background: var(--bg); border-bottom: 1px solid var(--line);
  padding: 12px 20px; display: flex; gap: 12px; align-items: baseline;
  flex-wrap: wrap;
}
header.top a.home { color: var(--accent); font-weight: 700; text-decoration: none; }
header.top .crumb { color: var(--muted); font-size: 14px; }
main { max-width: 1000px; margin: 0 auto; padding: 24px 20px 80px; }
h1 { font-size: 28px; line-height: 1.25; margin: 8px 0 4px; }
h2 {
  font-size: 21px; margin: 36px 0 10px; padding-bottom: 6px;
  border-bottom: 2px solid var(--accent);
}
h3 {
  font-size: 17px; margin: 26px 0 6px; color: var(--accent);
  scroll-margin-top: 70px;
}
p { margin: 10px 0; }
ul { margin: 10px 0 10px 22px; padding: 0; }
li { margin: 4px 0; }
a { color: var(--link); }
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.88em; background: var(--card); padding: 1px 5px;
  border-radius: 4px;
}
blockquote {
  margin: 14px 0; padding: 10px 14px; background: var(--warn-bg);
  border-left: 4px solid var(--warn-line); border-radius: 0 6px 6px 0;
}
.tw { overflow-x: auto; margin: 12px 0 20px; -webkit-overflow-scrolling: touch; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td {
  border: 1px solid var(--line); padding: 7px 10px; text-align: left;
  vertical-align: top; white-space: nowrap;
}
td:last-child, th:last-child { white-space: normal; }
th { background: var(--thead); font-weight: 600; position: sticky; top: 0; }
tbody tr:nth-child(even) td { background: var(--zebra); }
footer {
  max-width: 1000px; margin: 0 auto; padding: 24px 20px 60px;
  color: var(--muted); font-size: 13px; border-top: 1px solid var(--line);
}
.teamgrid {
  display: grid; gap: 12px; margin: 18px 0 4px;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
}
.teamgrid a {
  display: block; padding: 14px 16px; background: var(--card);
  border: 1px solid var(--line); border-radius: 8px; text-decoration: none;
  color: var(--fg); font-weight: 600;
}
.teamgrid a:hover { border-color: var(--accent); }
.teamgrid span { display: block; font-weight: 400; color: var(--muted);
                 font-size: 13px; margin-top: 3px; }
@media (max-width: 640px) {
  body { font-size: 15px; }
  h1 { font-size: 23px; }
  main { padding: 16px 14px 60px; }
  th, td { padding: 6px 8px; }
}
"""


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


def site_page(md_text: str, title: str, crumb: str, depth: int, today: str,
              repo_url: str, extra_body: str = "") -> str:
    """Kääntää raportin Markdownin sivuston HTML-sivuksi."""
    body = markdown_to_html(md_text, title, fragment=True)

    # Taulukot vieritettäviksi kapealla näytöllä
    body = body.replace("<table>", '<div class="tw"><table>')
    body = body.replace("</table>", "</table></div>")
    # Sivuston sisäiset linkit: joukkue/joukkue.md -> joukkue/
    body = re.sub(r'href="([^"/]+)/\1\.md"', r'href="\1/"', body)
    # PDF-linkkejä ei julkaista sivustolla
    body = re.sub(r'\s*\(?<a href="[^"]+\.pdf">[^<]*</a>\)?', "", body)

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
{extra_body}
</main>
<footer>Generoitu {today} · data: <a href="https://www.opendota.com/">OpenDota</a>{src}</footer>
</body>
</html>
"""


def build_site(site_dir: str, index_md: str, team_pages, today: str, repo_url: str):
    """Kirjoittaa staattisen sivuston GitHub Pagesia varten.

    Rakenne:  docs/index.html  ja  docs/<joukkue>/index.html
    `.nojekyll` estää GitHubia ajamasta Jekylliä turhaan.
    """
    os.makedirs(site_dir, exist_ok=True)
    with open(os.path.join(site_dir, ".nojekyll"), "w") as f:
        f.write("")

    # Etusivulle korttilinkit joukkueisiin
    cards = ['<div class="teamgrid">']
    for team, slug, _md, sub in team_pages:
        cards.append(f'<a href="{slug}/">{html.escape(team)}'
                     f'<span>{sub}</span></a>')
    cards.append("</div>")

    with open(os.path.join(site_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(site_page(index_md, "Turnauksen pelikirja", "", 0, today,
                          repo_url, extra_body="\n".join(cards)))

    for team, slug, md, _sub in team_pages:
        d = os.path.join(site_dir, slug)
        os.makedirs(d, exist_ok=True)
        raw_link = ""
        if repo_url:
            raw_link = (f'<h2>Raakadata</h2><p>Tämän joukkueen käsittelemätön '
                        f'OpenDota-data: <a href="{repo_url}/tree/main/'
                        f'scouting-results/{slug}/raw">scouting-results/{slug}/raw</a></p>')
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(site_page(md, f"{team} — pelikirja", team, 1, today,
                              repo_url, extra_body=raw_link))
    return len(team_pages) + 1


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
        f"Jokaisesta pelaajasta: kaikkien aikojen top-{TOP_HERO_COUNT} heropoolia, "
        f"viimeisimmät ottelut (muoto + tämänhetkinen heropooli, enintään "
        f"{MATCH_FETCH_LIMIT} ottelua) ja pelipaikkajakauma. Heropoolista on karsittu "
        f"heropit joita on pelattu alle {MIN_GAMES_FOR_HERO} kertaa. Turbo-ottelut "
        f"jätetään muoto- ja heropoolilaskennasta pois aina kun normaaleja otteluita "
        f"on tarpeeksi.",
        "",
    ]


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
    L = [f"{h} ⚠️ Huomioita aineiston laadusta", ""]
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
    L += ["> Tarkista aina että raportin **Steam-nimi** vastaa odotettua "
          "pelaajaa. Jos ei vastaa, listan Steam ID on väärä.", ""]
    return L


def team_report(team, players, data, hero_names, dupes, today,
                no_data, bad_ids, mismatches, draft=None):
    """Yhden joukkueen itsenäinen Markdown-raportti.

    `draft` on valinnainen draft-konteksti oman joukkueen näkökulmasta:
    {"team": oma joukkue, "strengths": {joukkue: heropit}, "matchups": {...}}.
    """
    own_team = (draft or {}).get("team")
    L = [f"# {team} — pelikirja"
         + (" · oma joukkue" if own_team and team == own_team else ""), ""]
    L += intro_lines(today)
    L += quality_lines(dupes, no_data, bad_ids, mismatches, team=team, level=2)

    if own_team:
        strengths = draft["strengths"]
        if team == own_team:
            L += allround_lines(own_team, players, strengths.get(team, []),
                                (draft.get("field") or [])[:FIELD_THREAT_POOL],
                                draft.get("matchups") or {},
                                draft.get("hero_stats") or {}, hero_names)
            L += own_team_lines(own_team, strengths.get(team, []), hero_names)
        else:
            L += draft_plan_lines(team, strengths.get(team, []), own_team,
                                  strengths.get(own_team, []), hero_names,
                                  draft.get("matchups") or {},
                                  draft.get("hero_stats") or {})

    # Rosteri
    L += ["## Rosteri", ""]
    has_roles = any(p[4] for p in players)
    rows = []
    for nick, mmr, sid, is_sub, role in players:
        d = data.get((team, nick), {})
        prof = (d.get("profile") or {}).get("profile") or {}
        form = recent_form(d.get("analyzed"))
        row = [f"**{nick}**" + (" _(sub)_" if is_sub else ""), mmr or "–"]
        if has_roles:
            row.append(ROLE_LABELS.get(role, "–"))
        row += [prof.get("personaname") or "–",
                rank_medal((d.get("profile") or {}).get("rank_tier")),
                lane_split(d.get("counts")) or "–",
                f"{form[2]:.0f}% ({form[0]}-{form[1] - form[0]})" if form else "–",
                form[3] if form else "–"]
        rows.append(row)
    headers = ["Pelaaja", "MMR"] + (["Pelipaikka"] if has_roles else []) + [
        "Steam-nimi", "Medal", "Linjat", "Muoto", "Viim. peli"]
    L += md_table(headers, rows)
    L.append("")

    # Joukkueen yhteinen heropooli
    sig_games, sig_wins, sig_players = Counter(), Counter(), defaultdict(set)
    for nick, _mmr, _sid, _sub, _role in players:
        for m in (data.get((team, nick), {}).get("analyzed") or []):
            hid = m.get("hero_id")
            if not hid:
                continue
            sig_games[hid] += 1
            sig_players[hid].add(nick)
            if match_is_win(m):
                sig_wins[hid] += 1
    if sig_games:
        L += ["## Joukkueen viimeaikaiset picksit", "",
              "Kaikkien pelaajien viimeaikaiset ottelut yhdessä — "
              "todennäköisimmät bannikohteet:", ""]
        rows = []
        for hid, g in sig_games.most_common(TEAM_SIGNATURE_COUNT):
            rows.append([hero_names.get(hid, f"Hero {hid}"), g, sig_wins[hid],
                         f"{sig_wins[hid] / g * 100:.0f}%",
                         ", ".join(sorted(sig_players[hid]))])
        L += md_table(["Hero", "Pelit", "Voitot", "WR%", "Kuka pelaa"], rows)
        L.append("")

    # Pelaajat
    L += ["## Pelaajat", ""]
    for nick, mmr, sid, is_sub, role in players:
        d = data.get((team, nick), {})
        L.append(f"### {nick}" + (" _(varapelaaja)_" if is_sub else ""))
        L.append("")

        if d.get("error"):
            L += [f"- ❌ Steam ID -virhe: {d['error']}", ""]
            continue

        acc = d["account_id"]
        prof_root = d.get("profile") or {}
        persona = (prof_root.get("profile") or {}).get("personaname")
        meta = [f"Listan MMR ~{mmr}" if mmr else "MMR ei tiedossa",
                f"medal {rank_medal(prof_root.get('rank_tier'))}",
                f"Steam ID `{sid}`",
                f"[OpenDota-profiili](https://www.opendota.com/players/{acc})"]
        if persona:
            meta.insert(0, f"Steam-nimi **{persona}**")
        L.append("- " + " · ".join(meta))

        others = [f"{t} / {n}" for t, n in dupes.get(sid, []) if n != nick]
        if others:
            L.append(f"- ⚠️ Sama Steam ID listalla myös: {', '.join(others)} "
                     f"— tiedot voivat koskea väärää pelaajaa.")

        if not has_public_data(d):
            L += ["- **Ei julkista dataa OpenDotassa** — tili on olemassa mutta "
                  "Dota 2:n asetus *Expose Public Match Data* on pois päältä "
                  "(tai Steam ID osoittaa väärään tiliin). Skouttaa manuaalisesti.", ""]
            continue

        wl = d.get("wl") or {}
        w, l = wl.get("win", 0), wl.get("lose", 0)
        if w + l:
            L.append(f"- Kaikkien aikojen W/L: **{w}V / {l}H** "
                     f"({w / (w + l) * 100:.0f}%)")
        form = recent_form(d.get("analyzed"))
        if form:
            fw, ft, fwr, last = form
            note = d.get("sample_note")
            L.append(f"- Viimeiset {ft} ottelua: **{fw}V / {ft - fw}H ({fwr:.0f}% WR)** "
                     f"· viimeisin peli {last}" + (f" · _{note}_" if note else ""))
        lanes = lane_split(d.get("counts"))
        if lanes:
            L.append(f"- Pelipaikat: {lanes}")
        L.append("")

        rec = recent_heroes(d.get("analyzed"), hero_names)
        if rec:
            L += [f"**Viimeaikaiset heropit** (viim. {len(d.get('analyzed') or [])} ottelua)", ""]
            L += md_table(["Hero", "Pelit", "Voitot", "WR%"],
                          [[n, g, wn, f"{wr:.0f}%"] for n, g, wn, wr in rec])
            L.append("")
        pool = top_heroes(d.get("heroes"), hero_names)
        if pool:
            L += [f"**Top-heropit, kaikki ajat** (väh. {MIN_GAMES_FOR_HERO} peliä)", ""]
            L += md_table(["Hero", "Pelit", "Voitot", "WR%"],
                          [[n, g, wn, f"{wr:.0f}%"] for n, g, wn, wr in pool])
            L.append("")
        elif not rec:
            L += ["- Ei riittävästi heropelidataa.", ""]

    return "\n".join(L).rstrip() + "\n"


def index_report(teams, data, today, dupes, no_data, bad_ids, mismatches,
                 with_pdf=False, draft=None):
    """Hakemistosivu: yleiskatsaus + linkit joukkueiden raportteihin."""
    own_team = (draft or {}).get("team")
    L = ["# Turnauksen pelikirja — vastustajaskouttaus", ""]
    L += intro_lines(today)
    if own_team:
        L += [f"Näkökulma: **{own_team}**. Jokaisen vastustajan sivulla on "
              f"draft-suunnitelma — bannijärjestys heidän uhkiaan vastaan ja "
              f"pick-ehdotukset omasta heropoolista.", ""]
    L += ["Jokaisella joukkueella on oma kansionsa, josta löytyy raportti "
          "Markdownina" + (" ja PDF:nä" if with_pdf else "") + " sekä "
          "`raw/`-alikansiossa OpenDotan käsittelemätön vastausdata "
          "pelaajittain.", ""]

    L += ["## Joukkueet", ""]
    rows = []
    for team, players in teams:
        mains = [p for p in players if not p[3]]
        subs = [p for p in players if p[3]]
        mmrs = [p[1] for p in mains if p[1]]
        avg = sum(mmrs) / len(mmrs) if mmrs else 0
        slug = slugify(team)
        label = f"[{team}]({slug}/{slug}.md)"
        if team == own_team:
            label += " _(oma)_"
        row = [label, len(mains), len(subs),
               f"{avg:,.0f}".replace(",", " ") if mmrs else "–",
               f"{min(mmrs)}–{max(mmrs)}" if mmrs else "–"]
        if with_pdf:
            row.append(f"[PDF]({slug}/{slug}.pdf)")
        rows.append((row, avg))
    rows.sort(key=lambda r: -r[1])
    headers = ["Joukkue", "Pelaajia", "Varalla", "Keski-MMR", "MMR-haitari"]
    if with_pdf:
        headers.append("PDF")
    L += md_table(headers, [r for r, _ in rows])
    L.append("")
    L += ban_summary_lines(teams, draft, hero_names=(draft or {}).get("hero_names"))
    L += quality_lines(dupes, no_data, bad_ids, mismatches, team=None, level=2)
    return "\n".join(L).rstrip() + "\n"


def ban_summary_lines(teams, draft, hero_names, level=2):
    """Pikaviite: kunkin vastustajan kärkibannit yhdellä silmäyksellä."""
    own_team = (draft or {}).get("team")
    if not own_team or not hero_names:
        return []
    h = "#" * level
    rows = []
    for team, _players in teams:
        if team == own_team:
            continue
        top = (draft["strengths"].get(team) or [])[:SUMMARY_BAN_COUNT]
        if not top:
            continue
        slug = slugify(team)
        rows.append([f"[{team}]({slug}/{slug}.md)"]
                    + [f"{hero_names.get(r['hero_id'], r['hero_id'])} "
                       f"({r['score']:.0f})" for r in top]
                    + ["–"] * (SUMMARY_BAN_COUNT - len(top)))
    if not rows:
        return []
    return ([f"{h} 🎯 Bannikärki joukkueittain", "",
             f"Näkökulma **{own_team}**. Suluissa uhkaindeksi. Koko "
             f"draft-suunnitelma pick-ehdotuksineen on joukkueen omalla "
             f"sivulla.", ""]
            + md_table(["Vastustaja"]
                       + [f"{i}. banni" for i in range(1, SUMMARY_BAN_COUNT + 1)],
                       rows) + [""])


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
                    "draft-suunnitelmat OpenDotan datasta.")
    p.add_argument("--pdf", action="store_true",
                   help="kirjoita myös PDF per joukkue (valinnainen)")
    p.add_argument("--oma", metavar="JOUKKUE",
                   default=os.environ.get("OMA_JOUKKUE", ""),
                   help="oma joukkue: draft-suunnitelmat lasketaan tämän "
                        "näkökulmasta. Oletuksena joukkueet.txt:ssä "
                        "merkintä \"(oma)\" otsikon perässä, tai "
                        "ympäristömuuttuja OMA_JOUKKUE.")
    p.add_argument("--ei-matchupeja", dest="matchups", action="store_false",
                   help="älä hae heropien matchup-dataa; pick-ehdotukset "
                        "perustuvat silloin pelkkään omaan heropooliin")
    return p.parse_args(argv)


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
        print(f"Oma joukkue: {own_team} — draft-suunnitelmat lasketaan "
              f"tämän näkökulmasta.")
    else:
        print("Omaa joukkuetta ei ole valittu, joten draft-suunnitelmia ei "
              "lasketa.\n  Valitse se lipulla --oma \"Joukkueen nimi\" tai "
              "merkitsemällä joukkueet.txt:ssä otsikko: ## Joukkueeni (oma)")

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

    # --- Draft-analyysi oman joukkueen näkökulmasta ---
    draft = None
    if own_team:
        strengths = {team: hero_strengths(team, players, data)
                     for team, players in teams}
        # Koko kentän yhteinen uhkalista: kaikki vastustajat yhtenä joukkona.
        # Sitä vastaan lasketaan omien pelaajien yleispätevät heropit.
        field = hero_strengths_multi([(t, p) for t, p in teams if t != own_team],
                                     data)
        hero_stats = fetch_hero_stats()
        matchups = {}
        if args.matchups:
            threat_ids = set(r["hero_id"] for r in field[:FIELD_THREAT_POOL])
            for team, _players in teams:
                if team == own_team:
                    continue
                threat_ids.update(r["hero_id"]
                                  for r in strengths[team][:THREAT_POOL])
            matchups = fetch_matchups(threat_ids)
            if not matchups:
                print("  [VAROITUS] matchup-dataa ei saatu — pick-ehdotukset "
                      "perustuvat pelkkään omaan heropooliin.")
        draft = {"team": own_team, "strengths": strengths, "field": field,
                 "matchups": matchups, "hero_stats": hero_stats,
                 "hero_names": hero_names}

    # --- Kirjoitus: yksi kansio per joukkue ---
    today = datetime.date.today().isoformat()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    make_pdf = args.pdf                 # PDF on valinnainen, oletuksena pois
    pdf_ok = pdf_fail = 0
    team_pages = []

    print()
    for team, players in teams:
        slug = slugify(team)
        team_dir = os.path.join(OUTPUT_DIR, slug)
        os.makedirs(team_dir, exist_ok=True)

        md_text = team_report(team, players, data, hero_names, dupes, today,
                              no_data, bad_ids, mismatches, draft=draft)
        md_path = os.path.join(team_dir, f"{slug}.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_text)

        n_raw = write_raw_data(os.path.join(team_dir, "raw"), team, players,
                               data, today)
        mains = [p for p in players if not p[3]]
        mmrs = [p[1] for p in mains if p[1]]
        avg = f"{sum(mmrs) / len(mmrs):,.0f}".replace(",", " ") if mmrs else "–"
        sub = f"{len(players)} pelaajaa · keski-MMR {avg}"
        if team == own_team:
            sub += " · oma joukkue"
        team_pages.append((team, slug, md_text, sub))
        print(f"{team}: {os.path.relpath(md_path, HERE)} (+{n_raw} raw-tiedostoa)")

        if make_pdf:
            if write_pdf(md_text, os.path.join(team_dir, f"{slug}.pdf")):
                pdf_ok += 1
            else:
                pdf_fail += 1

    index_md = index_report(teams, data, today, dupes, no_data, bad_ids,
                            mismatches, with_pdf=make_pdf, draft=draft)
    index_path = os.path.join(OUTPUT_DIR, "README.md")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(index_md)

    # --- GitHub Pages -sivusto ---
    n_pages = build_site(SITE_DIR, index_md, team_pages, today,
                         git_repo_web_url())

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
