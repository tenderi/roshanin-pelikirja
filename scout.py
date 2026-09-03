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

KÄYTTÖ
------
    pip install requests
    python3 scout.py

Tuloksena syntyy `pelikirja.md` samaan hakemistoon.

HUOM
----
- OpenDota API on ilmainen mutta rajoittaa pyyntömäärää (n. 60 pyyntöä/min
  ilman avainta). Jos sinulla on OpenDota API-avain, aseta se
  ympäristömuuttujaan:
      export OPENDOTA_API_KEY="oma-avaimesi"
- Pelaajan Dota-tilaston täytyy olla julkinen (Dota 2 -asetus
  "Expose Public Match Data"), tai OpenDota ei näe hänestä mitään. Tällöin
  raporttiin merkitään "Ei julkista dataa OpenDotassa".
- Syötteen Steam ID:t ovat STEAM_0:Y:Z -muodossa (Steam2). Ne muunnetaan
  OpenDotan 32-bit account_id -muotoon kaavalla: account_id = Z*2 + Y
- Vastaukset välimuistitetaan hakemistoon `.cache/`, joten ajon voi keskeyttää
  ja jatkaa myöhemmin ilman että kaikki haetaan uudelleen. Tyhjennä hakemisto
  kun haluat tuoreet luvut.
"""

import os
import re
import sys
import time
import json
import datetime
import difflib
from collections import defaultdict, Counter

try:
    import requests
except ImportError:
    sys.exit("Tarvitset requests-kirjaston: pip install requests")

HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_FILE = os.path.join(HERE, "joukkueet.txt")
OUTPUT_FILE = os.path.join(HERE, "pelikirja.md")
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

RANK_NAMES = {1: "Herald", 2: "Guardian", 3: "Crusader", 4: "Archon",
              5: "Legend", 6: "Ancient", 7: "Divine", 8: "Immortal"}
LANE_NAMES = {1: "Safe", 2: "Mid", 3: "Off", 4: "Jungle"}


# ---------------------------------------------------------------------------
# SYÖTTEEN LUKU
# ---------------------------------------------------------------------------

def parse_teams(path: str):
    """Lukee `joukkueet.txt`:n -> [(joukkue, [(nick, mmr, steam_id, on_sub), ...])].

    Rivimuoto:  Nick | MMR | STEAM_0:Y:Z      (erottimena | tai /)
    Sulkeissa oleva rivi tulkitaan varapelaajaksi: (Nick | MMR | STEAM_...)
    """
    teams = []
    current = None
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                current = (line.lstrip("#").strip(), [])
                teams.append(current)
                continue
            if current is None:
                print(f"  [VAROITUS] rivi {lineno} ennen joukkueotsikkoa: {line}")
                continue
            is_sub = line.startswith("(") and line.rstrip().endswith(")")
            parts = re.split(r"\s*[|/]\s*", line.strip("()").strip())
            if len(parts) != 3:
                print(f"  [VAROITUS] rivi {lineno} ei jäsenny: {line}")
                continue
            nick, mmr_s, steam_id = (p.strip() for p in parts)
            try:
                mmr = int(re.sub(r"\D", "", mmr_s))
            except ValueError:
                print(f"  [VAROITUS] rivi {lineno}: MMR ei ole numero: {mmr_s}")
                mmr = 0
            current[1].append((nick, mmr, steam_id.upper(), is_sub))
    return teams


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
    """Hakee yhden pelaajan kaikki tarvittavat tiedot."""
    out = {}
    for key, path, params in (
        ("profile", f"/players/{account_id}", None),
        ("wl",      f"/players/{account_id}/wl", None),
        ("heroes",  f"/players/{account_id}/heroes", None),
        ("matches", f"/players/{account_id}/matches", {"limit": MATCH_FETCH_LIMIT}),
        ("counts",  f"/players/{account_id}/counts", None),
    ):
        cached = os.path.exists(_cache_path(path, params or {}))
        out[key] = api_get(path, params)
        if not cached:
            time.sleep(REQUEST_DELAY)
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
# PÄÄOHJELMA
# ---------------------------------------------------------------------------

def main():
    if not os.path.exists(INPUT_FILE):
        sys.exit(f"Syötetiedostoa ei löydy: {INPUT_FILE}")

    teams = parse_teams(INPUT_FILE)
    if not teams:
        sys.exit(f"Yhtään joukkuetta ei löytynyt tiedostosta {INPUT_FILE}")

    hero_names = load_hero_names()
    if not hero_names:
        sys.exit("Hero-nimistöä ei saatu OpenDotasta — API voi olla alhaalla. "
                 "Kokeile myöhemmin uudelleen.")

    # --- Duplikaatti-Steam ID:t syötteessä (kopiointivirheet) ---
    seen = defaultdict(list)
    for team, players in teams:
        for nick, _mmr, sid, _sub in players:
            seen[sid].append(f"{team} / {nick}")
    dupes = {sid: names for sid, names in seen.items() if len(names) > 1}

    # --- Haku ---
    data = {}          # (team, nick) -> dict
    no_data = []       # pelaajat joilta ei saatu mitään
    bad_ids = []       # pelaajat joiden Steam ID ei jäsenny
    mismatches = []    # nick ei muistuta Steam-nimeä -> mahdollisesti väärä tili
    for team, players in teams:
        print(f"\n=== Joukkue: {team} ===")
        for nick, mmr, sid, is_sub in players:
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
                no_data.append(f"{team} / {nick}")
            else:
                persona = ((d.get("profile") or {}).get("profile") or {}).get("personaname")
                if persona and not nick_matches_persona(nick, persona):
                    mismatches.append((team, nick, persona, account_id))

    # --- Raportin koostaminen ---
    today = datetime.date.today().isoformat()
    L = []
    L.append("# Turnauksen pelikirja — vastustajaskouttaus")
    L.append("")
    L.append(f"_Generoitu {today} · lähde: [OpenDota](https://www.opendota.com/) · "
             f"aineisto: `joukkueet.txt`_")
    L.append("")
    L.append(f"Jokaisesta pelaajasta: kaikkien aikojen top-{TOP_HERO_COUNT} heropoolia, "
             f"viimeisimmät ottelut (muoto + tämänhetkinen heropooli, enintään "
             f"{MATCH_FETCH_LIMIT} ottelua) ja pelipaikkajakauma. Heropoolista on karsittu "
             f"heropit joita on pelattu alle {MIN_GAMES_FOR_HERO} kertaa. Turbo-ottelut "
             f"jätetään muoto- ja heropoolilaskennasta pois aina kun normaaleja otteluita "
             f"on tarpeeksi.")
    L.append("")

    # Joukkueiden yleiskatsaus
    L.append("## Joukkueiden yleiskatsaus")
    L.append("")
    rows = []
    for team, players in teams:
        mains = [p for p in players if not p[3]]
        subs = [p for p in players if p[3]]
        mmrs = [p[1] for p in mains if p[1]]
        avg = f"{sum(mmrs) / len(mmrs):,.0f}".replace(",", " ") if mmrs else "–"
        rng = f"{min(mmrs)}–{max(mmrs)}" if mmrs else "–"
        rows.append([f"[{team}](#{team.lower().replace(' ', '-').replace('/', '')})",
                     len(mains), len(subs), avg, rng])
    rows.sort(key=lambda r: (r[3] == "–", -float(str(r[3]).replace(" ", "")) if r[3] != "–" else 0))
    L += md_table(["Joukkue", "Pelaajia", "Varalla", "Keski-MMR", "MMR-haitari"], rows)
    L.append("")

    # Datan laatu
    if dupes or no_data or bad_ids or mismatches:
        L.append("## ⚠️ Huomioita aineiston laadusta")
        L.append("")
        if dupes:
            L.append("**Samat Steam ID:t esiintyvät useammalla pelaajalla** — "
                     "todennäköisesti kopiointivirhe `joukkueet.txt`:ssä, ja näiden "
                     "pelaajien tiedot raportissa ovat siksi epäluotettavia:")
            L.append("")
            for sid, names in dupes.items():
                L.append(f"- `{sid}` → {', '.join(names)}")
            L.append("")
        if bad_ids:
            L.append("**Steam ID ei jäsenny:**")
            L.append("")
            for team, nick, err in bad_ids:
                L.append(f"- {team} / {nick}: {err}")
            L.append("")
        if no_data:
            L.append("**Ei julkista dataa OpenDotassa** (yksityinen profiili tai "
                     "Steam ID osoittaa väärään tiliin): "
                     + ", ".join(no_data))
            L.append("")
        if mismatches:
            L.append("**Steam-nimi ei muistuta listan nickiä** — yleensä pelaaja on vain "
                     "vaihtanut Steam-nimeään, mutta tarkista ettei Steam ID osoita "
                     "väärään tiliin:")
            L.append("")
            for team, nick, persona, acc in mismatches:
                L.append(f"- {team} / **{nick}** → Steam-nimi \"{persona}\" "
                         f"([profiili](https://www.opendota.com/players/{acc}))")
            L.append("")
        L.append("> Tarkista aina että raportin **Steam-nimi** vastaa odotettua "
                 "pelaajaa. Jos ei vastaa, listan Steam ID on väärä.")
        L.append("")

    # Joukkuekohtaiset osiot
    for team, players in teams:
        L.append(f"## {team}")
        L.append("")

        # Rosteritaulukko
        rows = []
        for nick, mmr, sid, is_sub in players:
            d = data.get((team, nick), {})
            prof = (d.get("profile") or {}).get("profile") or {}
            persona = prof.get("personaname") or "–"
            medal = rank_medal((d.get("profile") or {}).get("rank_tier"))
            form = recent_form(d.get("analyzed"))
            form_s = f"{form[2]:.0f}% ({form[0]}-{form[1] - form[0]})" if form else "–"
            lanes = lane_split(d.get("counts")) or "–"
            last = form[3] if form else "–"
            rows.append([f"**{nick}**" + (" _(sub)_" if is_sub else ""),
                         mmr or "–", persona, medal, lanes, form_s, last])
        L += md_table(["Pelaaja", "MMR", "Steam-nimi", "Medal", "Pelipaikat",
                       "Muoto (viim. ottelut)", "Viim. peli"], rows)
        L.append("")

        # Joukkueen yhteinen heropooli viimeaikaisista otteluista
        sig_games, sig_wins, sig_players = Counter(), Counter(), defaultdict(set)
        for nick, _mmr, _sid, _sub in players:
            for m in (data.get((team, nick), {}).get("analyzed") or []):
                hid = m.get("hero_id")
                if not hid:
                    continue
                sig_games[hid] += 1
                sig_players[hid].add(nick)
                if match_is_win(m):
                    sig_wins[hid] += 1
        if sig_games:
            L.append("**Joukkueen viimeaikaiset picksit** "
                     "(kaikkien pelaajien viimeaikaiset ottelut yhdessä) — "
                     "todennäköisimmät bannikohteet:")
            L.append("")
            rows = []
            for hid, g in sig_games.most_common(TEAM_SIGNATURE_COUNT):
                rows.append([hero_names.get(hid, f"Hero {hid}"), g, sig_wins[hid],
                             f"{sig_wins[hid] / g * 100:.0f}%",
                             ", ".join(sorted(sig_players[hid]))])
            L += md_table(["Hero", "Pelit", "Voitot", "WR%", "Kuka pelaa"], rows)
            L.append("")

        # Pelaajakohtaiset osiot
        for nick, mmr, sid, is_sub in players:
            d = data.get((team, nick), {})
            title = f"### {nick}"
            if is_sub:
                title += " _(varapelaaja)_"
            L.append(title)
            L.append("")

            if d.get("error"):
                L.append(f"- ❌ Steam ID -virhe: {d['error']}")
                L.append("")
                continue

            acc = d["account_id"]
            prof_root = d.get("profile") or {}
            prof = prof_root.get("profile") or {}
            persona = prof.get("personaname")

            meta = [f"Listan MMR ~{mmr}" if mmr else "MMR ei tiedossa",
                    f"medal {rank_medal(prof_root.get('rank_tier'))}",
                    f"Steam ID `{sid}`",
                    f"[OpenDota-profiili](https://www.opendota.com/players/{acc})"]
            if persona:
                meta.insert(0, f"Steam-nimi **{persona}**")
            L.append("- " + " · ".join(meta))

            if sid in dupes:
                L.append(f"- ⚠️ Sama Steam ID listalla myös: "
                         f"{', '.join(n for n in dupes[sid] if not n.endswith('/ ' + nick))} "
                         f"— tiedot voivat koskea väärää pelaajaa.")

            if not has_public_data(d):
                L.append("- **Ei julkista dataa OpenDotassa** — tili on olemassa mutta "
                         "Dota 2:n asetus *Expose Public Match Data* on pois päältä "
                         "(tai Steam ID osoittaa väärään tiliin). Skouttaa manuaalisesti.")
                L.append("")
                continue

            wl = d.get("wl") or {}
            w, l = wl.get("win", 0), wl.get("lose", 0)
            if w + l:
                L.append(f"- Kaikkien aikojen W/L: **{w}V / {l}H** ({w / (w + l) * 100:.0f}%)")

            form = recent_form(d.get("analyzed"))
            if form:
                fw, ft, fwr, last = form
                note = d.get("sample_note")
                L.append(f"- Viimeiset {ft} ottelua: **{fw}V / {ft - fw}H ({fwr:.0f}% WR)** "
                         f"· viimeisin peli {last}"
                         + (f" · _{note}_" if note else ""))

            lanes = lane_split(d.get("counts"))
            if lanes:
                L.append(f"- Pelipaikat: {lanes}")
            L.append("")

            rec = recent_heroes(d.get("analyzed"), hero_names)
            if rec:
                L.append(f"**Viimeaikaiset heropit** (viim. {len(d.get('analyzed') or [])} ottelua)")
                L.append("")
                L += md_table(["Hero", "Pelit", "Voitot", "WR%"],
                              [[n, g, wn, f"{wr:.0f}%"] for n, g, wn, wr in rec])
                L.append("")

            pool = top_heroes(d.get("heroes"), hero_names)
            if pool:
                L.append(f"**Top-heropit, kaikki ajat** (väh. {MIN_GAMES_FOR_HERO} peliä)")
                L.append("")
                L += md_table(["Hero", "Pelit", "Voitot", "WR%"],
                              [[n, g, wn, f"{wr:.0f}%"] for n, g, wn, wr in pool])
                L.append("")
            elif not rec:
                L.append("- Ei riittävästi heropelidataa.")
                L.append("")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(L).rstrip() + "\n")

    print(f"\nValmis! Raportti kirjoitettu tiedostoon: {OUTPUT_FILE}")
    if no_data:
        print(f"Ilman julkista dataa: {len(no_data)} pelaajaa")


if __name__ == "__main__":
    main()
