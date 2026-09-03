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
    python3 scout.py              # kirjoittaa .md, .pdf ja raakadatan
    python3 scout.py --no-pdf     # vain Markdown + raakadata

Tuloksena syntyy hakemisto `scouting-results/`, jossa on yksi kansio per
joukkue:

    scouting-results/
        README.md                    <- yleiskatsaus + linkit
        lph-voide/
            lph-voide.md             <- joukkueen pelikirja
            lph-voide.pdf            <- sama PDF:nä
            raw/                     <- OpenDotan raakavastaukset
                seinis-104984836.json
                ...

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



def markdown_to_html(md_text: str, title: str) -> str:
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
                no_data, bad_ids, mismatches):
    """Yhden joukkueen itsenäinen Markdown-raportti."""
    L = [f"# {team} — pelikirja", ""]
    L += intro_lines(today)
    L += quality_lines(dupes, no_data, bad_ids, mismatches, team=team, level=2)

    # Rosteri
    L += ["## Rosteri", ""]
    rows = []
    for nick, mmr, sid, is_sub in players:
        d = data.get((team, nick), {})
        prof = (d.get("profile") or {}).get("profile") or {}
        form = recent_form(d.get("analyzed"))
        rows.append([f"**{nick}**" + (" _(sub)_" if is_sub else ""),
                     mmr or "–",
                     prof.get("personaname") or "–",
                     rank_medal((d.get("profile") or {}).get("rank_tier")),
                     lane_split(d.get("counts")) or "–",
                     f"{form[2]:.0f}% ({form[0]}-{form[1] - form[0]})" if form else "–",
                     form[3] if form else "–"])
    L += md_table(["Pelaaja", "MMR", "Steam-nimi", "Medal", "Pelipaikat",
                   "Muoto", "Viim. peli"], rows)
    L.append("")

    # Joukkueen yhteinen heropooli
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
    for nick, mmr, sid, is_sub in players:
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


def index_report(teams, data, today, dupes, no_data, bad_ids, mismatches):
    """Hakemistosivu: yleiskatsaus + linkit joukkueiden raportteihin."""
    L = ["# Turnauksen pelikirja — vastustajaskouttaus", ""]
    L += intro_lines(today)
    L += ["Jokaisella joukkueella on oma kansionsa, josta löytyy raportti "
          "Markdownina ja PDF:nä sekä `raw/`-alikansiossa OpenDotan "
          "käsittelemätön vastausdata pelaajittain.", ""]

    L += ["## Joukkueet", ""]
    rows = []
    for team, players in teams:
        mains = [p for p in players if not p[3]]
        subs = [p for p in players if p[3]]
        mmrs = [p[1] for p in mains if p[1]]
        avg = sum(mmrs) / len(mmrs) if mmrs else 0
        slug = slugify(team)
        rows.append([f"[{team}]({slug}/{slug}.md)", len(mains), len(subs),
                     f"{avg:,.0f}".replace(",", " ") if mmrs else "–",
                     f"{min(mmrs)}–{max(mmrs)}" if mmrs else "–",
                     f"[PDF]({slug}/{slug}.pdf)", avg])
    rows.sort(key=lambda r: -r[-1])
    L += md_table(["Joukkue", "Pelaajia", "Varalla", "Keski-MMR", "MMR-haitari", "PDF"],
                  [r[:-1] for r in rows])
    L.append("")
    L += quality_lines(dupes, no_data, bad_ids, mismatches, team=None, level=2)
    return "\n".join(L).rstrip() + "\n"


def write_raw_data(raw_dir: str, team: str, players, data, today: str) -> int:
    """Kirjoittaa OpenDotan raakavastaukset pelaajittain JSON-tiedostoiksi."""
    os.makedirs(raw_dir, exist_ok=True)
    written = 0
    for nick, mmr, sid, is_sub in players:
        d = data.get((team, nick), {})
        payload = {
            "haettu": today,
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
        path = os.path.join(raw_dir, f"{slugify(nick)}-{acc}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        written += 1
    return written


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
            seen[sid].append((team, nick))
    dupes = {sid: names for sid, names in seen.items() if len(names) > 1}

    # --- Haku ---
    data, no_data, bad_ids, mismatches = {}, [], [], []
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
                no_data.append((team, nick))
            else:
                persona = ((d.get("profile") or {}).get("profile") or {}).get("personaname")
                if persona and not nick_matches_persona(nick, persona):
                    mismatches.append((team, nick, persona, account_id))

    # --- Kirjoitus: yksi kansio per joukkue ---
    today = datetime.date.today().isoformat()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    make_pdf = "--no-pdf" not in sys.argv
    pdf_ok = pdf_fail = 0

    print()
    for team, players in teams:
        slug = slugify(team)
        team_dir = os.path.join(OUTPUT_DIR, slug)
        os.makedirs(team_dir, exist_ok=True)

        md_text = team_report(team, players, data, hero_names, dupes, today,
                              no_data, bad_ids, mismatches)
        md_path = os.path.join(team_dir, f"{slug}.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_text)

        n_raw = write_raw_data(os.path.join(team_dir, "raw"), team, players,
                               data, today)
        print(f"{team}: {os.path.relpath(md_path, HERE)} (+{n_raw} raw-tiedostoa)")

        if make_pdf:
            if write_pdf(md_text, os.path.join(team_dir, f"{slug}.pdf")):
                pdf_ok += 1
            else:
                pdf_fail += 1

    index_path = os.path.join(OUTPUT_DIR, "README.md")
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(index_report(teams, data, today, dupes, no_data, bad_ids, mismatches))

    print(f"\nValmis! {len(teams)} joukkuetta -> {os.path.relpath(OUTPUT_DIR, HERE)}/")
    print(f"Hakemistosivu: {os.path.relpath(index_path, HERE)}")
    if make_pdf:
        print(f"PDF:t: {pdf_ok} onnistui" + (f", {pdf_fail} epäonnistui" if pdf_fail else ""))
    if no_data:
        print(f"Ilman julkista dataa: {len(no_data)} pelaajaa")


if __name__ == "__main__":
    main()
