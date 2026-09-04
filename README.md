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

## Draft-suunnitelma vastustajaa vastaan

Kun **oma joukkue** on valittu, jokaisen vastustajan sivulle syntyy
draft-suunnitelma juuri sinun näkökulmastasi:

- **Bannit tärkeysjärjestyksessä** — vastustajan heropit uhkaindeksin mukaan.
  Indeksi yhdistää viimeaikaisen pelivolyymin (mitä he oikeasti pickkaavat),
  kaikkien aikojen kokemuksen ja voittoprosentin.
- **Pickit omasta poolista** — mitä me osaamme pelata ja mikä siitä puree
  juuri heidän uhkaheropeihinsa.
- **Pelaajakohtaiset ehdotukset** — kunkin oman pelaajan omasta poolista.
- **Kiistellyt heropit** — mitä molemmat haluavat, eli mikä katoaa jos et
  banni tai pickkaa ensin.
- **Varo näitä** — oman poolin heropit joilla on huono matchup tätä
  vastustajaa vastaan.

Oman joukkueen omalle sivulle tulee lisäksi kaksi asiaa: **kunkin pelaajan
viisi vahvinta heroa** koko turnauskenttää vastaan (yleispätevä lähtökohta,
ei sidottu yhteen vastustajaan) sekä sama uhka-analyysi käännettynä eli mitä
*sinulta* todennäköisesti bannataan. Etusivulla on pikaviite kunkin
vastustajan kärkibanneista.

Pelaajakohtainen viiden kärki yhdistää pelaajan oman mukavuusalueen (60 %),
heropin pärjäämisen kaikkien vastustajien uhkaheropeille (22 %) ja heropin
yleisen voittoprosentin tässä patchissa (18 %, OpenDotan bracketit
Legend–Divine).

Matchup-luvut ovat OpenDotan hero-matchup-datasta, joka perustuu
ammattilaispeleihin. Otokset ovat pieniä, joten havaittu voittoprosenttiero
kutistetaan otoskoon mukaan kohti nollaa eikä se yksin nosta heroa listan
kärkeen — oma mukavuusalue painaa enemmän. Kohtele lukuja suuntaviivana, ei
totuutena.

## Käyttö

```bash
pip install requests
python3 scout.py                    # raportit, raakadata ja verkkosivusto
python3 scout.py --oma "Joukkueeni" # + draft-suunnitelmat tätä vastaan
python3 scout.py --pdf              # + PDF per joukkue (valinnainen)
python3 scout.py --ei-matchupeja    # ohita matchup-haku (nopeampi)
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

Kun pelipaikka on annettu, pelaajan pick-ehdotukset rajataan siihen sopiviin
heropeihin: hard support ei saa ehdotukseksi offlane-corea vaikka olisi
pelannut sitä paljon. Sopivuus päätellään OpenDotan roolitageista
(`Carry`, `Support`, `Initiator`, ...) ja se näkyy taulukossa merkkinä
✓ / ~ / ✗.

Rajoitus kannattaa tietää: roolitagit erottavat corit tukipelaajista, mutta
**eivät nelosta viitosesta** — molemmat ovat OpenDotalle vain "Support".
Soft ja hard support saavat siis samat ehdotukset.

### Oman joukkueen valinta

Työkalu ei tunne mitään joukkuetta erityisenä ennen kuin kerrot sen. Valinta
tehdään yhdellä kolmesta tavasta, tässä järjestyksessä:

1. `--oma "Joukkueen nimi"` — osittainen nimi riittää (`--oma roshan`)
2. ympäristömuuttuja `OMA_JOUKKUE`
3. `(oma)`-merkintä otsikon perässä `joukkueet.txt`:ssä

Ilman valintaa raportit syntyvät ennallaan, ilman draft-osioita.

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
- Draft-suunnitelmat hakevat matchup-datan vastustajien uhkaheropeista
  (muutamia kymmeniä lisäpyyntöjä ensimmäisellä ajolla, sen jälkeen
  välimuistista).
