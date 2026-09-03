# roshanin-pelikirja

Vastustajaskouttaus amatööri-Dota 2 -turnaukseen. `scout.py` hakee jokaisen
pelaajan tiedot [OpenDotasta](https://www.opendota.com/) ja koostaa niistä
joukkuekohtaisen pelikirjan.

## 📖 Pelikirja verkossa

**<https://tenderi.github.io/roshanin-pelikirja/>**

Sama sisältö löytyy myös repon sisältä: [`scouting-results/`](scouting-results/).

## Mitä raportti kertoo

Jokaisesta pelaajasta:

- **Heropooli** — sekä kaikkien aikojen suosikit että se mitä hän pelaa juuri nyt
- **Muoto** — viimeisimpien otteluiden voittoprosentti
- **Pelipaikka** — safe / mid / off -jakauma
- **Henkilöllisyyden tarkistus** — Steam-nimi ja rank medal, jotta näet heti
  osoittaako listan Steam ID oikeaan tiliin

Joukkuetasolla lisäksi yhteenveto viimeaikaisista pickseistä eli
todennäköisimmistä bannikohteista.

## Käyttö

```bash
pip install requests
python3 scout.py            # raportit, raakadata ja verkkosivusto
python3 scout.py --pdf      # sama + PDF per joukkue (valinnainen)
```

Pelaajalista luetaan tiedostosta [`joukkueet.txt`](joukkueet.txt), joka on
ainoa paikka jossa joukkueita ylläpidetään. Muoto:

```
## Joukkueen nimi
Nick | MMR | STEAM_0:0:12345678
(Varapelaaja | MMR | STEAM_0:0:87654321)
```

Ajo kirjoittaa:

| Hakemisto | Sisältö |
|---|---|
| `docs/` | Julkaistava sivusto (GitHub Pages) |
| `scouting-results/<joukkue>/<joukkue>.md` | Joukkueen pelikirja Markdownina |
| `scouting-results/<joukkue>/raw/*.json` | OpenDotan käsittelemättömät vastaukset |

Vastaukset välimuistitetaan hakemistoon `.cache/`, joten ajon voi keskeyttää ja
jatkaa. Tyhjennä välimuisti kun haluat tuoreet luvut:

```bash
rm -rf .cache && python3 scout.py
```

## Julkaisu

Sivusto on staattista HTML:ää hakemistossa `docs/`. Kytke GitHub Pages päälle
kerran: **Settings → Pages → Source: "Deploy from a branch" → Branch: `main`,
kansio `/docs`**. Sen jälkeen jokainen push päivittää sivuston.

## Huomioitavaa

- Pelaajan **Dota 2 -asetuksen "Expose Public Match Data" on oltava päällä**,
  muuten OpenDota ei näe hänestä mitään ja raporttiin tulee merkintä
  "Ei julkista dataa".
- Raportti varoittaa jos sama Steam ID esiintyy useammalla pelaajalla tai jos
  Steam-nimi ei muistuta listan nickiä — kumpikin viittaa virheeseen
  `joukkueet.txt`:ssä.
- OpenDota rajoittaa pyyntömäärää (n. 60/min ilman avainta). Nopeampaa ajoa
  varten: `export OPENDOTA_API_KEY="oma-avaimesi"`.
