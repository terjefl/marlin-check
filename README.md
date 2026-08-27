# Marlin Readiness Check

Webportal for Fisker Owners Association: medlemmer laster opp en diagnoserapport
fra **Ocean Link Pro (OLP)**, og portalen sjekker om bilens moduler oppfyller
minimumsversjonene for den store **Marlin**-programvareoppdateringen.

- Umiddelbart resultat per modul (OK / utdatert / mangler) og samlet verdikt
  (Marlin-klar eller «zebra» — trenger målrettede oppdateringer først).
- Nedlastbar PDF-rapport (WeasyPrint).
- Valgfritt samtykke til lagring → anonym flåtestatistikk (unike VIN,
  versjonsfordeling per modul). Uten samtykke lagres ingenting.
- 7 språk (en, nb, sv, da, de, fr, es) med autovalg fra nettleseren.
- Ingen LLM — deterministisk parsing og versjonssammenligning.

## Status / faser

| Fase | Innhold | Status |
|---|---|---|
| 1 | App-scaffold (denne koden) | ✅ |
| 2 | Ekte OLP-parser (`app/parser.py`) — venter på ekte rapport | ⏳ |
| 3 | Ekte Marlin-krav i `requirements.yaml` — venter på foreningen | ⏳ |
| 4 | Deploy på DMZ-DOCKER01 bak marlin.flagan.net | ⏳ |

`app/parser.py` inneholder i dag en midlertidig parser for et syntetisk
tekstformat (se `tests/fixtures/synthetic_report.txt`). Når det ekte
OLP-formatet foreligger byttes uttrekkslogikken ut — grensesnittet
(`parse_report(bytes) -> ParsedReport`) er stabilt.

## Kravfilen

`requirements.example.yaml` definerer modulkravene og bind-mountes i produksjon
som `/config/requirements.yaml`. Den leses på nytt ved hver analyse, så krav
kan oppdateres på hosten **uten** rebuild av imaget.

## Kjøre lokalt

```bash
docker compose up --build
```

Åpne <http://localhost:8000>. Test med `tests/fixtures/synthetic_report.txt`.

Uten Docker (PDF-nedlasting krever pango/cairo installert):

```bash
pip install -r requirements.txt pytest httpx
pytest
uvicorn app.main:app --reload
```

## Miljøvariabler

| Variabel | Standard | Beskrivelse |
|---|---|---|
| `MARLIN_DATA_DIR` | `/data` | SQLite-database (`marlin.sqlite3`) |
| `MARLIN_UPLOADS_DIR` | `/data/uploads` | Lagrede rapportfiler (kun ved samtykke) |
| `MARLIN_REQUIREMENTS_PATH` | `/config/requirements.yaml` | Kravfilen |

## Bygg og deploy

GitHub Actions bygger og publiserer `ghcr.io/terjefl/marlin-check` (`latest` +
git-SHA) ved push til `main`. Produksjons-stacken ligger i Docker-repoet under
`DMZ-DOCKER01/marlin/` og deployes via Portainer (GitOps).
