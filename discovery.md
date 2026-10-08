# Discovery — HAPI FHIR R4 público

Levantamento feito em 2026-10-08 contra `https://hapi.fhir.org/baseR4` (somente leitura, dados sintéticos). O servidor é compartilhado e reiniciado periodicamente, então os números abaixo são uma fotografia, não um contrato.

## Servidor

| Item | Resultado |
|---|---|
| Software | HAPI FHIR 8.13.10-SNAPSHOT, FHIR 4.0.1 |
| Autenticação | Nenhuma (`security` ausente no CapabilityStatement) |
| Formatos | JSON, XML, Turtle |
| Rate limit | Nenhum header de limite. 10 requisições concorrentes → todas `200`, mas latência sobe de ~1,5s para ~3s (servidor serializa). Nenhum `429` observado |
| Latência típica | 1,5–2s por página de 200 recursos |
| `_count` máximo | Pedido 1000 → servidor devolve **500** |

## Volume atual

| Recurso | `_summary=count` | `$export` (linhas NDJSON) |
|---|---|---|
| Patient | 3.978 | 3.978 |
| Observation | 34.126 | 34.126 |

`$get-resource-counts` retorna números diferentes (5.217 / 35.536), provavelmente incluindo excluídos ou cache. **Usar `_summary=count` para reconciliação.**

## Capacidades úteis

| Recurso | Funciona? | Observação |
|---|---|---|
| **`$export` (Bulk Data)** | ✅ | `Prefer: respond-async` → `202` + `Content-Location`. O job completou em segundos. Saída em NDJSON, 1.000 recursos por arquivo (4 de Patient, 35 de Observation, ~33 MB, ~2 min para baixar sequencialmente). `requiresAccessToken: true` no manifesto, mas o download funcionou sem token |
| `$export?_since=` | ✅ (aceito) | Base para cargas delta |
| Paginação `Bundle.link[next]` | ✅ | Cursor `_getpages=<uuid>&_getpagesoffset=N`. Continuou válido após alguns minutos; o TTL não está documentado |
| `_sort=_lastUpdated` | ✅ | Ordem consistente entre páginas |
| `_lastUpdated=ge..&_lastUpdated=lt..` | ✅ | Viabiliza particionar por janelas de tempo |
| `_summary=count` | ✅ | Contagem rápida (<1s) por filtro/janela |
| `Observation?subject=id1,id2,...` | ✅ | 50 pacientes em uma query → 1.460 observations. Evita N buscas individuais |
| `Patient?_revinclude=Observation:subject` | ✅ | 5 pacientes → 155 observations no mesmo Bundle |
| `Patient/{id}/$everything` | ✅ | Traz também Appointment, ServiceRequest, Practitioner etc., ou seja, mais do que o necessário |
| `Patient?_has:Observation:subject:status=final` | ✅ | 1.233 pacientes têm ao menos uma observation final. Útil para escolher a amostra |
| `_elements` | ✅ | Marca o recurso como `SUBSETTED`. Não reduziu o payload de forma relevante neste teste |
| `history-type` / `history-instance` | ✅ | Possível fonte para detectar exclusões |

## Qualidade dos dados — Patient (3.978)

| Campo | Presença | Achados |
|---|---|---|
| `name` | 92% | 311 sem nome; 3.574 com 1 nome, alguns com até 19. `use=official` em ~50%, `use` ausente em ~48%. 151 sem `family`, 184 sem `given`, 123 só com `text` |
| `identifier` | 71% | Sistemas muito heterogêneos (MRN demo, NHANES, Synthea, SSN, passaporte…). Tipo `MR` só em 451. **Não há identificador universal confiável** |
| `birthDate` | 85% | 3.376 `YYYY-MM-DD`, 3 só `YYYY`, **12 com hora** (`1967-08-22T00:00:00`, inválido em R4), **1 no ano 5032** |
| `gender` | 89,5% | female 1.912, male 1.524, **ausente 419**, unknown 87, other 36 |
| `deceased[x]` | 1,8% | Boolean (27) e DateTime (42) |
| `telecom` / `address` | 19% / 17% | Opcionais |
| `meta.versionId > 1` | 5,8% | Há recursos atualizados |
| `id` | — | 77% numéricos, o resto texto (`patient-001`, `nhanes-…`). Tratar como string |

## Qualidade dos dados — Observation (34.126)

| Aspecto | Achados |
|---|---|
| `status` | final 33.744, preliminary 338, registered 12, **entered-in-error 11**, ausente 21 |
| `subject` | `Patient/{id}` 32.221 (todos resolvem, 0 órfãos), **`Location/{id}` 1.435**, **só `identifier`/`display` sem `reference` 453**, **URL absoluta de outro host 2**, ausente 15 |
| `value[x]` | Quantity 24.978, CodeableConcept 7.987, **nenhum 847**, String 244, Boolean 38, Integer 32. `dataAbsentReason` em apenas 8 |
| `valueQuantity` | 86,5% com sistema UCUM; 10 sem `value`; 45 sem unidade |
| `component` | 20% das observations (ex.: pressão arterial, scores de modelo de ECG) |
| `hasMember` | 50 (painéis) |
| `effective[x]` | DateTime 33.707, Period 204, ausente 215. Fusos mistos (`Z`, `+00:00`, `-05:00`) e precisão de ms a µs |
| `category` | vital-signs 15.200, laboratory 13.860, survey, exam…; **2.833 sem category** |
| `code` | LOINC em 27.044; também ICD-10, SNOMED e sistemas locais/fictícios; **3.304 sem `coding` (só `text`)** |
| Distribuição | Mediana de 20 obs/paciente; **`patient-001` tem 5.931** (todas LOINC 11524-6, ECG). **2.736 pacientes (69%) não têm nenhuma observation** |
| Tamanho médio | ~850 B por Patient, ~940 B por Observation |

Todos os recursos são sintéticos; vários trazem `meta.tag` explícita (`synthetic-identity`, `synthea-5-2019`, `SYNTHETIC`).

## Implicações para o MVP

1. **Extração:** para a amostra, `Patient?_has:Observation:subject:status=final&_count=20` seguido de `Observation?subject=<ids>&_count=200` com paginação. São poucas requisições e não depende de `$export`. Para escala, o `$export` funciona aqui e é o caminho preferido; paginar com `_lastUpdated` fica como fallback.
2. **Amostra:** filtrar pacientes com observations (69% não têm nenhuma) e tratar outliers como `patient-001` com o teto de 1.000 observations já previsto no plano.
3. **Rate limit:** sem limite declarado, mas a latência dobra sob concorrência. Manter 1 worker e ~2 req/s, com backoff para `429`/`5xx` preparado mesmo sem ter observado nenhum.
4. **Quarentena precisa cobrir:** `subject` apontando para Location, `subject` sem `reference`, referência absoluta de outro host, `value[x]` ausente sem `dataAbsentReason`, `birthDate` inválida ou futura.
5. **Mapeamento:** `code` pode não ter coding (usar `text`); `value_type` deve cobrir pelo menos Quantity, CodeableConcept, String, Boolean e Integer; `component` é frequente o bastante para aparecer na UI; `effectivePeriod` existe.
6. **UI:** sinalizar `entered-in-error` e `preliminary`; mostrar "sem valor" diferente de zero; gênero e nascimento ausentes são comuns.
7. **Reconciliação:** `_summary=count` bate exatamente com o `$export` e serve como referência de contagem.
