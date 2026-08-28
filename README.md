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
| 1 | App-scaffold | ✅ |
| 2 | Ekte OLP-parser (`app/parser.py`) — verifisert mot ekte rapport 2026-08-28 | ✅ |
| 3 | Marlin-krav i `requirements.yaml` — utkast fra videomøte lagt inn; offisielle tall og to uttrekks-antakelser (BCM, ESP) må bekreftes | ⏳ |
| 4 | Deploy på DMZ-DOCKER01 bak marlin.flagan.net | ⏳ |

Parseren leser OLP-ens «ECU Software Version Report»-PDF (eller tekstuttrekket
av den): seksjoner (BODY/INFOTAINMENT/POWERTRAIN/CHASSIS/ADAS), ECU-blokker
(`KODE - Navn`) og feltet **Supplier SW Version**, som er det som sammenlignes
mot kravene. Kravmodellen er profilbasert (2.0/2.1): en bil som når 2.1-nivået
på alle kravmoduler er «100 % 2.1» og kan oppdateres direkte til Marlin;
ellers er den en «zebra» og må via SW 2.2 / målrettede oppdateringer først.
Se kommentarene i `requirements.example.yaml` for antakelsene.

## Kravfilen

`requirements.example.yaml` definerer modulkravene. I produksjon ligger den som
`/config/requirements.yaml` (bind-mountet katalog). Den leses på nytt ved hver
analyse, så krav kan oppdateres **uten** rebuild — enten direkte på hosten
eller via admin-siden.

## Admin-side (`/admin`)

Beskyttet side for web-redigering av kravspec:

- **Innlogging:** HTTP Basic med flere brukere. Brukerne ligger i
  `/config/admin_users.yaml` (se `admin_users.example.yaml`) som
  `brukernavn: pbkdf2-hash` — lag hasher med `python3 scripts/hash_password.py`.
  Kun hasher lagres. Feilede forsøk rate-limites (10 per 15 min per IP).
- **Redigering:** YAML-en valideres (syntaks, struktur, regexer, profiler) før
  lagring; ugyldig innhold avvises uten å røre filen. Lagring er atomisk og
  virker umiddelbart på neste analyse.
- **Revisjonslogg:** hver lagring logges i SQLite (`audit_log`) med tidspunkt,
  brukernavn, klient-IP (fra `CF-Connecting-IP`) og unified diff av endringen.
  Loggen vises nederst på admin-siden.

## Kjøre lokalt

```bash
docker compose up --build
```

Åpne <http://localhost:8000>. Test med `tests/fixtures/olp_report.txt` (anonymisert ekte rapport).

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
