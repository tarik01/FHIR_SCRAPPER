# Plano 2 — MVP de migração FHIR R4

**Objetivo:** planejar a migração de ~50.000 pacientes e suas observações e demonstrar o fluxo com uma amostra em até três horas, incluindo este plano. Medir o volume de observações separadamente. O legado permanece como fonte oficial até a validação. Este documento apresenta uma proposta independente; não assume capacidades ou medições ainda não verificadas.

## 1. Abordagem e confiabilidade

`FHIR (leitura) → staging → transformação/validação → base candidata → serviço/UI`

**Preparação e extração.** Consultar `/metadata` e verificar filtros e paginação. Definir a população de pacientes e selecionar suas observações. Usar somente dados sintéticos, sem escrever no [HAPI público](https://hapi.fhir.org/), que é descartável. Para escala, avaliar Bulk Data `$export` se suportado; alternativa: paginar Patient e Observation separadamente, evitando 50 mil buscas individuais. Pedir `_count=100`, seguir `Bundle.link[relation=next]` sem construir cursores e aceitar links apenas da origem autorizada. Particionar por janelas de `_lastUpdated` se suportado; filtros temporais não garantem snapshot de uma fonte mutável. Referência: [busca FHIR R4](https://hl7.org/fhir/R4/search.html).

**Retomada e idempotência.** Persistir staging e checkpoint por página em transação. Se o cursor expirar, reiniciar a janela e deduplicar. Carregar pacientes antes das observações. Usar chave única `(source_system, resource_type, source_id)`; guardar versão da fonte, hash do conteúdo, versão do mapeamento e `run_id`. Reexecuções não duplicam registros; mudanças no mapeamento exigem reprocessamento. `meta.versionId` é opaco, não ordenável. Serializar atualizações por chave; versões com ordem incerta exigem reconciliação, evitando sobrescrita por dados antigos ([API REST FHIR](https://hl7.org/fhir/R4/http.html)).

**Checkpoint durável no MVP.** Usar SQLite em disco para staging e progresso. Cada unidade de extração (tipo + janela temporal, ou lote de pacientes na amostra) guarda `run_id`, consulta inicial, próximo link, estado (`pending/running/done/failed`) e último erro sanitizado. Persistir também a população selecionada e os limites da execução para retomar o mesmo escopo.

```text
buscar página pelo checkpoint salvo (fora da transação)
BEGIN
  salvar recursos no staging, deduplicando identidade + versão/hash
  salvar próximo link, ou marcar extração da unidade como concluída
COMMIT
```

| Falha | Recuperação |
|---|---|
| Antes do commit, inclusive durante a gravação | A transação é desfeita; repetir somente a página não confirmada. |
| Depois do commit | Continuar pelo próximo link persistido. |
| Cursor expirado | Reiniciar a consulta da unidade afetada, reaproveitando e deduplicando o staging. |
| Mapeamento/carga interrompidos | Retomar entradas locais pendentes; gravar upsert e estado de processamento na mesma transação. |

Manter progresso de extração separado do processamento por recurso e versão do mapeamento. Uma correção reprocessa o staging sem baixar novamente; falhas isoladas permanecem em quarentena. Retomar uma execução existente reutiliza seu `run_id`; não apaga o banco. A garantia é preservar trabalho confirmado enquanto o banco estiver íntegro, não eliminar toda releitura: cursores expirados exigem repetir a unidade, e mudanças na fonte exigem reconciliação final. Esta estratégia é prevista para o extrator; o script de discovery atual ainda salva seu relatório apenas no final.

**Limites e observabilidade.** Começar com um worker e teto configurável de 2 requisições/s, sem presumir limite oficial. Timeout de 20s; até cinco tentativas para falhas transitórias, `429` e `5xx`, com backoff, jitter e `Retry-After`. Falhas persistentes pausam a etapa; registros inválidos entram em quarentena. Medir progresso, latência, tentativas, recursos únicos aceitos/rejeitados e páginas pendentes por execução.

**Consistência final.** Na migração real, aplicar deltas com sobreposição temporal e deduplicação. Capturar exclusões via histórico/change feed, se disponível. Antes da troca, combinar congelamento breve de escrita, delta final e reconciliação de IDs, inclusive exclusões. Sem histórico confiável, comparar o inventário completo sob congelamento. Publicar somente após validação.

## 2. Modelo interno e mapeamento

UUID interno e metadados de origem acompanham cada registro. Campos opcionais ausentes permanecem `null`; nenhuma inferência clínica ou conversão automática de unidade.

| Origem → destino | Regras |
|---|---|
| [Patient](https://hl7.org/fhir/R4/patient.html) → `patients` | Nome: preferir `use=official`, depois o primeiro. Identificadores: pares `system/value`, sem usar nome/prontuário isolado como chave. `birthDate`: texto com precisão original. `gender`: preservar código e ausência. `deceased[x]`: boolean/data opcionais; ausência não significa vivo. |
| [Observation](https://hl7.org/fhir/R4/observation.html) → `observations` | `subject` resolve para `patient_id`; aceitar referências relativas e absolutas da origem conhecida. Referência não resolvida fica em quarentena; sujeito fora da população é exclusão explícita. Preservar `status`, códigos com sistema/display e texto de `code`, mesmo sem coding. |
| Valor e componentes | `value_type` + `value_json` tipado: quantidade preserva decimal, unidade, sistema, código e comparador; conceitos preservam codings; boolean permanece boolean. `components_json` mantém componentes agrupados. Preservar `dataAbsentReason`: ausência de valor pode ser válida. Tipo não suportado vai para quarentena. |
| Tempo | `effective_type` + `effective_json`: preservar datas parciais e início/fim de períodos. UTC apenas para instantes com fuso, mantendo o original. |

Colunas atendem identidade, vínculo e consulta; JSON preserva estruturas clínicas variáveis. A UI respeita status e distingue ausência de zero. Datas parciais não ganham precisão inventada ([tipos FHIR](https://hl7.org/fhir/R4/datatypes.html)).

## 3. Validação e aceite

- **Cobertura:** manifesto de IDs/versões por recurso e escopo. Recursos únicos extraídos = aceitos + quarentena + exclusões justificadas; aceitos = chaves correspondentes no destino. Repetições não entram na soma. Comparar com inventário da fonte estabilizada; contagens isoladas não provam completude.
- **Correção:** zero duplicatas e vínculos órfãos no destino. Comparar campos transformados do staging com o banco; revisar amostra estratificada de até 500 pacientes e suas observações, incluindo unidades, componentes, datas, ausências e distribuições de códigos/status.
- **Recuperação:** fixtures sintéticas exercitam `429`, cursor expirado e falhas antes/depois do commit. Reiniciar deve preservar páginas confirmadas, não perder nem duplicar recursos e permitir reprocessar o staging sem rede. Aceite exige nenhuma perda inexplicada, nenhuma página pendente e rejeições resolvidas ou explicitamente aprovadas pelo responsável; um percentual baixo não justifica perda clínica.

## 4. Segurança em uma versão real

Extrair somente campos necessários. Proteger staging, quarentena e backups como dados sensíveis: TLS, criptografia em repouso, credenciais em cofre, menor privilégio, auditoria e retenção com descarte definido. Logs não incluem payload, URLs com identificadores nem mensagens clínicas; IDs vinculáveis também são sensíveis. Separar ambientes e desenvolver com dados sintéticos. Validar requisitos legais e contratuais aplicáveis com os responsáveis por privacidade antes de produção.

## 5. Rollback

Construir uma base candidata isolada e preservar a versão ativa. Falha intermediária: pausar, corrigir e retomar pelo staging. Mapeamento incorreto: reconstruir a candidata. Publicar trocando atomicamente a referência do serviço; rollback retorna à base anterior. Excluir linhas por `run_id` não restaura valores sobrescritos. Inicialmente, manter o novo serviço somente leitura; se receber escritas, reconciliá-las antes de retornar ao legado. Ensaiar restauração antes da migração real.

## 6. Entrega em três horas

| Tempo | Entrega |
|---|---|
| 0–45 min | Plano e decisões de escopo. |
| 45–100 min | Python/httpx + SQLite: até 20 pacientes e suas observações, paginação, staging, checkpoints transacionais, retomada, upsert e retry. Teto de 1.000 observações; ao atingir, declarar amostra incompleta. |
| 100–140 min | FastAPI + HTML: lista de pacientes, detalhe e observações; acesso local, somente leitura. |
| 140–165 min | Testes essenciais de mapeamento, idempotência e retomada; reconciliação. |
| 165–180 min | README, demonstração e limitações. |

**Pronto quando:** importar a amostra, navegar, repetir sem duplicatas e identificar pendências no relatório. Fixtures sintéticas permitem demonstração com HAPI indisponível. Buscas por paciente ficam restritas à amostra. Próximos passos: ensaio de volume, extração em lotes, deltas/exclusões, publicação de base candidata e controles de produção. Se o tempo acabar, documentar estado e próximo passo verificável.
