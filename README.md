# FHIR Migration MVP

Extracts patients and observations from a FHIR R4 server, maps them into a simpler internal model, validates the result, and shows it in a small web UI.

> **Synthetic data only.** Everything here runs against the public HAPI test server (`https://hapi.fhir.org/baseR4`), read-only, into a disposable local database. No real patient data is used or stored.

- **Plan:** [Plan.md](Plan.md) covers the approach, mapping, validation, safety and rollback.
- **Discovery:** [discovery.md](discovery.md) records what the source API supports and what the data really looks like.

## Repository layout

```
app/
  backend/     Django 5.2 LTS (async) · Python 3.14 · SQLite
    pipeline/    extract → raw store → transform → validate (services + migrate_fhir command)
    clinical/    internal model (Patient, Observation…) + async JSON API
  frontend/    Vue 3 + Vite · Node 24 LTS
docs/          diagrams (.mmd + .png), screenshots, implementation plan
scripts/       one-off discovery script (pre-MVP)
```

## Quick start

**Prerequisites:**
- [uv](https://docs.astral.sh/uv/), which installs Python 3.14 automatically from `.python-version`;
- Node 24 LTS (`nvm use` reads `app/frontend/.nvmrc`).

```bash
# 1. Backend: install, create the database, run the pipeline
cd app/backend
uv sync
uv run python manage.py migrate
uv run python manage.py migrate_fhir extract      # $export from HAPI → raw store (~35 s)
uv run python manage.py migrate_fhir transform    # raw store → internal model (~6 s)
uv run python manage.py migrate_fhir validate     # checklist + run report
uv run uvicorn config.asgi:application --port 8010

# 2. Frontend (second terminal)
cd app/frontend
nvm use && npm install
npm run dev                                        # http://localhost:5173
```

### Pipeline commands

| Command | What it does |
|---|---|
| `migrate_fhir extract` | Starts the `$export` kick-off (or resumes the unfinished job), polls the job, stores the expected counts and downloads every NDJSON file with 3 workers into the raw store. Safe to re-run. |
| `migrate_fhir extract --from-dir PATH` | Loads local synthetic `.ndjson` files instead, for offline demos when HAPI is down |
| `migrate_fhir transform [--rebuild]` | Maps the latest version of each raw resource into the internal model, or into quarantine with a reason. `--rebuild` clears the internal model first (the MVP rollback). |
| `migrate_fhir validate [--sample-rate 0.01] [--seed N]` | Runs the acceptance checklist and stores a `RunReport`; exits with code 1 if any check fails |

### Run tests

```bash
cd app/backend && uv run python manage.py test     # 64 tests, no network needed
```

## Results against the live server

Measured on 2026-10-08. The server is shared and changes during the day.

| Step | Result |
|---|---|
| Extract | 40 NDJSON files (5 Patient, 35 Observation), **38,196 resources in 35 s**; lines match `_summary=count` at `transactionTime` exactly |
| Re-run of extract | New export, **0 inserted, 38,196 duplicates skipped** |
| Transform | **4,034 patients + 32,142 observations** loaded; 3 + 2,017 quarantined; re-run reports everything `unchanged` |
| Validate | 8/8 checks pass; with `--sample-rate 1.0`, **all 38,196 records re-mapped from raw and compared, 0 mismatches** |

Top quarantine reasons, all found earlier during discovery:

| Reason | Records |
|---|---|
| subject is not a Patient (Location) | 1,435 |
| subject has no reference | 453 |
| observation has no code | 66 |
| no value, components, members or data absent reason | 45 |

## How it works

```
FHIR $export ──▶ raw store ──▶ transform ──▶ internal model ──▶ async API ──▶ Vue UI
   (NDJSON)      (untouched)   (pure mappers)  (or quarantine)
                      ▲                              │
                      └──────── validate (counts + re-mapped sample) ◀┘
```

**Extract** ([extract.py](app/backend/pipeline/services/extract.py), [fhir_client.py](app/backend/pipeline/services/fhir_client.py)):
- **Job state is the cursor:** `poll_url`, `transactionTime` and per-file status are saved, so a crash resumes the same export.
- **Retries:** retries with exponential backoff and jitter, honouring `Retry-After`.
- **Fail fast:** permanent errors (`4xx`, `$export` unsupported) stop at once with a clear hint.
- **Circuit breaker:** stops all workers after 10 consecutive failures.
- **Origin check:** links pointing to another host are rejected.
- **One transaction per file:** a partial download never counts. The unique key `(resource_type, source_id, version_id)` makes re-runs idempotent.
- **Diagrams:** [raw store](docs/raw-store-erd.png) · [retry flow](docs/retry-flow.png)

**Transform** ([transform.py](app/backend/pipeline/services/transform.py), [mapping.py](app/backend/pipeline/services/mapping.py)):
- **`PatientMapper` / `ObservationMapper`** are pure classes: they take FHIR JSON and return a record or `Rejected(reason)`.
- **Upserts:** `TransformEngine` upserts in batches of 1,000. Unchanged versions are skipped, an older version never overwrites a newer one, and a `MAPPING_VERSION` change re-maps everything.
- **Duplicate patients** are only *reported*, never merged automatically, since a wrong merge is a clinical safety risk.
- **Diagram:** [internal model](docs/internal-model-erd.png)

**Validate** ([validate.py](app/backend/pipeline/services/validate.py)):
- **Counts:** compares source counts with export lines, and checks that loaded + quarantined equals extracted.
- **Integrity:** no duplicates, no orphan observations, no pending files.
- **Sample:** re-maps a sample from the raw store and compares it field by field with the database.
- **It caught a real bug** during development: decimals with more than 10 places were being rounded silently (5,044 values). Values are now kept exactly as text (`value_text`), with a numeric copy for sorting.

### Mapping decisions

The principle is to preserve, not infer. A missing field stays `null`, units are never converted, and dates never gain precision they didn't have.

- **Name:** the `official` name, else the first one, else `name.text`.
- **Identifiers:** all of them, in a child table, since there is no common MRN.
- **`birthDate`:** stored with its precision (`year` / `month` / `day`). Values with a time are truncated; future dates go to quarantine.
- **`gender`:** missing stays `null`, not `unknown`.
- **`subject`:** only `Patient/{id}` of a loaded patient; anything else goes to quarantine.
- **`code`:** LOINC preferred; `code.text` is kept when there is no coding.
- **`value[x]`:** the 5 types that actually occur (Quantity, CodeableConcept, string, boolean, integer). Any other type goes to quarantine.
- **`component[]`:** kept as JSON.
- **`effective[x]`:** converted to UTC. Date-only values keep `day` precision, and no time is invented.
- **`entered-in-error`:** loaded, but hidden by default in the UI.
- **Not migrated (data minimisation):** phone, address and other demographics. They remain in the raw store.

## Screenshots

| Patients | Patient detail |
|---|---|
| ![Patients](docs/screenshots/patients.png) | ![Patient detail](docs/screenshots/patient-detail.png) |
| **Entered-in-error shown on demand** | **Run report** |
| ![Entered in error](docs/screenshots/entered-in-error.png) | ![Run report](docs/screenshots/run-report.png) |

## How I used AI

I used **Claude Code** (Anthropic) as a pair programmer throughout this exercise.

| Area | How AI was used | What I did |
|---|---|---|
| API discovery | Ran read-only requests against the HAPI sandbox (`/metadata`, `$export`, `_summary=count`, page-size limits) and profiled the synthetic data quality | Reproduced the key calls myself in Postman (kick-off, poll, expected counts) and chose `$export` as the extraction strategy |
| Plan (Part 1) | Explained concepts (bulk export, backoff, circuit breaker, cutover) and drafted text for each section | Chose the structure, cut what I found unnecessary and wrote the final [Plan.md](Plan.md) |
| Diagrams | Generated the Mermaid diagrams in [docs/](docs/) | Asked for simplifications, e.g. removing `last_seen_job_id` and the redundant job link from the raw store |
| Code and tests | Wrote the backend, frontend and tests from my instructions | Chose the stack and the latest LTS versions, asked for class-based services and readable code, kept scope to the brief (no pagination, no auth), and reviewed the result |

**How AI output was checked:**
- every step was run against the live sandbox;
- the test suite runs without network access;
- the validation step re-maps the data from the raw store and compares it with the database. This caught a real decimal-precision bug in the generated code, which was then fixed.

## Known limitations (MVP)

- **SQLite:**
  - `value_num` keeps about 15 significant digits; `value_text` holds the exact value.
  - Writes go through a single connection.
  - Large `IN` queries are batched to stay under SQLite's parameter limit.
- **Naive datetimes:** 3 source values have no timezone (invalid in FHIR) and are assumed to be UTC.
- **Same `versionId`, different content:** this is skipped by the unique key, but not alerted on.
- **Deletes:** not detected, because the MVP runs a single full export and HAPI's manifest has no `deleted` list.
- **Paged-search fallback:** described in the plan, not implemented. `EXTRACT_STRATEGY` only supports `export`.
- **Expected-count differences** are reported, but not explained automatically through `_history`.
- **No authentication, encryption at rest or raw-store purge:** this is a local demo on synthetic data. `requiresAccessToken` is ignored because the public server does not enforce it.
- **Quarantine sign-off and duplicate review** are informational only, with no workflow.
- **No pagination**, which the brief puts out of scope. The list returns all 4,034 patients in ~80 ms, and the largest patient's 5,931 observations in ~170 ms. At 50k patients, cursor-based pagination would be the first thing to add.
- **No frontend tests;** the UI was checked manually in the browser.

## What I'd do next (version 2)

### 1. Background processing with Celery + RabbitMQ

Today each step is a management command run by hand. In v2 the pipeline becomes a set of background tasks:

```
Celery Beat ──▶ start_export ──▶ poll_export ──▶ download_file × N ──▶ transform_batch × N ──▶ validate
 (schedule)       (kick-off)     (countdown =     (one task per        (one task per           (chord callback)
                                  Retry-After)     NDJSON file)          1,000 resources)
```

- **Units of work:** one task per NDJSON file and per transform batch. Both are already idempotent, so `acks_late=True` is safe: if a worker dies, RabbitMQ redelivers the task and the unique keys prevent duplicates.
- **Retries:** `autoretry_for` with `retry_backoff` and `retry_jitter` replaces the hand-written retry loop. A task-level `rate_limit` protects the legacy API.
- **RabbitMQ rather than Redis as the broker:** durable queues, explicit acks and redelivery, plus a **dead-letter exchange** for files that keep failing.
- **Separate queues** (`extract`, `transform`, `validate`) scale independently, for example more transform workers than download workers.
- **Celery Beat** schedules delta exports (`_since=<last transactionTime>`) until the cutover.
- **Flower,** or metrics exported from Celery, shows queue depth, failures and throughput.

An alternative that keeps fewer moving parts is Django's built-in Tasks framework with a production backend, but Celery + RabbitMQ is the more proven choice at this scale.

### 2. Storage and scale

- **Pagination:** cursor-based pagination on the patient list and the observations endpoint.

- **Postgres instead of SQLite:**
  - `NUMERIC` for exact decimals;
  - `JSONB` for the raw payload;
  - concurrent writers;
  - `COPY` for bulk loads;
  - table partitioning for millions of observations.
- **NDJSON files streamed to object storage (S3)** and parsed line by line, instead of loaded into memory.
- **A candidate database plus an atomic switch** for the real cutover (described in the plan's Rollback section).

### 3. Completeness and correctness

- **Deletes:** detect them through `_history?_since=<transactionTime>`, and explain count differences automatically.
- **Cutover tooling:** a write freeze on the legacy system, a final delta, ID reconciliation, then the switch.
- **Paged-search fallback** (`_lastUpdated` windows, `_count=500`) for servers without `$export`.
- **Integrity alert** when the same `versionId` arrives with different content.
- **Mapping:** support the remaining `value[x]` types (Range, Ratio, SampledData…) and normalize local codes to LOINC.

### 4. Workflows for people

- **Quarantine review:** fix at the source, adjust the mapping, or accept the loss, with sign-off.
- **Duplicate review:** confirm or reject candidates, integrated with the hospital's MPI process.

### 5. Security and operations

- **Authentication and access:**
  - SMART Backend Services (OAuth client credentials) for `$export`;
  - SSO with role-based access on the UI;
  - an audit log of who viewed what.
- **Data protection:** encryption at rest, the raw store purged after acceptance (TTL), and secrets kept in a vault.
- **Monitoring:** Prometheus/Grafana dashboards, an alert on `migration_halted`, and OpenTelemetry tracing.
- **Delivery:**
  - Docker Compose (Django, worker, RabbitMQ, Postgres, frontend);
  - CI with tests, linting and type checks;
  - frontend tests (Vitest + Playwright).
