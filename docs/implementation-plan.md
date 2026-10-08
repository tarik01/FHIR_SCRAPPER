# Plano de implementação do MVP

Tempo disponível: ~2h15 (após o Plan.md).

## Estrutura

```
FHIR/
├── Plan.md, discovery.md, docs/
├── app/backend/                               Django 5.2 LTS (async), Python 3.14
│   ├── config/                            settings, urls, asgi, logging JSON
│   ├── pipeline/                          extração + transformação
│   │   ├── models.py                      ExportJob, ExportFile, RawResource, Quarantine, RunReport
│   │   ├── services/
│   │   │   ├── fhir_client.py             httpx async: retry, backoff, circuit breaker
│   │   │   ├── extract.py                 kick-off → poll → counts → download → raw
│   │   │   ├── mapping.py                 funções puras: map_patient(), map_observation()
│   │   │   ├── transform.py               raw → modelo interno (lotes, upsert, quarentena, duplicatas)
│   │   │   └── validate.py                checklist da validação → RunReport
│   │   ├── management/commands/migrate_fhir.py
│   │   └── tests/
│   └── clinical/                          modelo interno + API
│       ├── models.py                      Patient, PatientIdentifier, Observation, PatientDuplicateCandidate
│       └── views.py                       views async (JSON)
└── app/frontend/                             Vue 3 + Vite + vue-router
    └── src/pages/                         PatientList, PatientDetail, RunReport
```

Dependências: `django`, `httpx`, `uvicorn` / `vue`, `vue-router`, `vite`. Sem `django-cors-headers` (proxy do Vite).

## Configurações

```
FHIR_BASE_URL = "https://hapi.fhir.org/baseR4"
EXTRACT_STRATEGY = "export"
EXPORT_WORKERS = 3
EXPORT_POLL_MAX_WAIT = 10
RETRY_MAX_ATTEMPTS = 5
CIRCUIT_BREAKER_THRESHOLD = 10
CONN_MAX_AGE = 0
LOGGING → JSON em stdout + logs/migration.log
```

## Blocos

| # | Tempo | Entrega | Pronto quando |
|---|---|---|---|
| 1 | 0–15 min | Scaffold Django + apps + settings + logging JSON; Vue/Vite com proxy `/api` | `runserver` e `npm run dev` sobem |
| 2 | 15–50 min | Models do raw store, `fhir_client`, `migrate_fhir extract` | ~40 arquivos baixados, 3 tabelas preenchidas, re-run sem duplicar |
| 3 | 50–85 min | `mapping.py` + testes dos casos do discovery, `migrate_fhir transform` | testes verdes, ~4k pacientes / ~34k observations, quarentena com motivos |
| 4 | 85–100 min | `migrate_fhir validate` → `RunReport` | checklist com cada check ✅/❌ |
| 5 | 100–125 min | 4 endpoints async + 3 telas Vue | lista → paciente → observations; relatório visível |
| 6 | 125–135 min | README | clonar e rodar em 5 min |

### Bloco 2 — Extract
- `migrate_fhir extract`: retoma job `in_progress` ou faz kick-off; poll; grava `expected_counts`; downloads com `asyncio.Semaphore(3)`; cada arquivo em uma transação (`sync_to_async(thread_sensitive=True)` + `transaction.atomic`); `bulk_create(ignore_conflicts=True)` pela chave `(resource_type, source_id, version_id)`.
- `--from-dir <pasta>`: carrega NDJSON sintético local (demo offline e testes).

### Bloco 3 — Mapping
- `map_patient(json)` / `map_observation(json, patient_ids)` → `Mapped(record)` ou `Rejected(reason)`, sem banco.
- Testes por caso real do discovery: nome sem `use`/só `text`; `birthDate` `YYYY`, com hora, no futuro; `gender` ausente; `subject` → `Location` / sem `reference` / host externo; `code` sem coding; cada um dos 5 `value[x]` + tipo não suportado; sem valor com `component`; `effectivePeriod`.
- `transform`: versão mais recente do raw; pacientes primeiro; pula inalterados; versão nunca regride; substitui identificadores; detecta duplicatas no fim.
- `--rebuild`: limpa as tabelas internas e reconstrói do raw (rollback do MVP).

### Bloco 5 — API + UI

| Endpoint | Tela |
|---|---|
| `GET /api/patients?page=&q=` | PatientList: paginada, busca por nome |
| `GET /api/patients/{id}` + `/observations` | PatientDetail: dados, identificadores, observations (`entered-in-error` oculto, "no value" ≠ 0, componentes) |
| `GET /api/runs/latest` | RunReport: checklist, contagens, motivos da quarentena, candidatos a duplicata |

## Cortes se o tempo apertar (nesta ordem)
1. Tela RunReport (relatório continua no terminal)
2. Detecção de duplicatas
3. Circuit breaker (retry continua)
4. Busca na lista de pacientes

Não cortar: extract com retomada, mapping com testes, quarentena, navegação básica na UI.

## Definition of done

```
python manage.py migrate_fhir extract     → raw store preenchido, idempotente
python manage.py migrate_fhir transform   → modelo interno + quarentena
python manage.py migrate_fhir validate    → checklist ✅
python manage.py test                     → verde
npm run dev                               → lista → paciente → observations
```
