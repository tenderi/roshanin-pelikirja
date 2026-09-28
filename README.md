# roshanin-pelikirja

Vastustajaskouttaus amatööri-Dota 2 -turnaukseen. `scout.py` hakee jokaisen
pelaajan tiedot [OpenDotasta](https://www.opendota.com/) ja koostaa niistä
joukkuekohtaisen pelikirjan.

## 📖 Pelikirja verkossa

**<https://tenderi.github.io/roshanin-pelikirja/>**

Sama sisältö löytyy myös repon sisältä: [`scouting-results/`](scouting-results/).

## Mitä sivu kertoo

Sivusto on englanniksi. Jokaisen joukkueen sivulla on kaksi osaa:

- **Bans** — kuusi heroa tärkeysjärjestyksessä, ja jokaisen kohdalla kenen
  takia se kannattaa bannata. Järjestys painottaa joukkueen kovimpia
  pelaajia: pelaajan paino on (MMR / joukkueen kovin MMR)³, joten esimerkiksi
  3 700 MMR:n pelaaja painaa 6 500 MMR:n tähden rinnalla vain noin viidesosan.
  Pelaajan oma uhka heropilla yhdistää sen, mitä hän pelaa juuri nyt
  (osuus viimeisimmistä otteluista), kokemuksen heropilla ja voittoprosentin.
- **Players** — kortti per pelaaja kovin MMR ensin: rank medal, linjat,
  muoto, kolme henkilökohtaista bannikohdetta sekä mitä hän pelaa nyt ja
  mitä on pelannut eniten kaikkiaan. Joukkueen bannilistalla olevat heropit
  on merkitty (`B1`, `B2`, ...).

Etusivulla on jokaisesta joukkueesta keski-MMR, kaksi kovinta pelaajaa ja
kolme ensimmäistä bannia. Steam-nimi näkyy kortissa, jotta näet heti
osoittaako listan Steam ID oikeaan tiliin.

## Käyttö

```bash
pip install requests
python3 scout.py                    # raportit, raakadata ja verkkosivusto
python3 scout.py --oma "Joukkueeni" # merkitse oma joukkue
python3 scout.py --pdf              # + PDF per joukkue (valinnainen)
```

Pelaajalista luetaan tiedostosta [`joukkueet.txt`](joukkueet.txt), joka on
ainoa paikka jossa joukkueita ylläpidetään. Muoto:

```
## Joukkueen nimi
Nick | MMR | STEAM_0:0:12345678
(Varapelaaja | MMR | STEAM_0:0:87654321)

## Oma joukkueeni (oma)
Nick | MMR | STEAM_0:0:11111111 | safelane
Toinen | MMR | STEAM_0:0:22222222 | hard support
```

### Pelipaikat

Neljäs kenttä on valinnainen **pelipaikka**. Kelpaavat esimerkiksi `1`–`5`,
`safelane`, `mid`, `offlane`, `soft support`, `hard support` sekä suomeksi
`kantaja`, `keskilinja`, `kolmonen`, `tuki`.

Pelipaikka näytetään pelaajan kortissa.

### Oman joukkueen valinta

Oma joukkue merkitään sivustolle, ja sen sivulla bannilista kertoo mitä
*meiltä* todennäköisesti bannataan. Valinta tehdään yhdellä kolmesta tavasta, tässä järjestyksessä:

1. `--oma "Joukkueen nimi"` — osittainen nimi riittää (`--oma roshan`)
2. ympäristömuuttuja `OMA_JOUKKUE`
3. `(oma)`-merkintä otsikon perässä `joukkueet.txt`:ssä

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
