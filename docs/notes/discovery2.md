# Discovery 2 — HAPI FHIR R4

**Verificação:** 08/10/2026, concluída às 09:31 (America/Araguaina). **Base:** `https://hapi.fhir.org/baseR4`.

Consultas GET sem credenciais, sequenciais, sobre o servidor público de testes indicado no enunciado. Nenhuma escrita clínica ou exportação completa. Payloads foram processados em memória; somente estatísticas e metadados técnicos foram gravados. Este levantamento preserva `discovery.md` e os dois planos existentes.

Evidência reproduzível: [resultados agregados](discovery2-results.json) e [script](scripts/discover_fhir.py). O relatório representa esta execução; o conteúdo do servidor compartilhado pode mudar.

## 1. O que foi confirmado

| Consulta / comportamento | Resultado observado |
|---|---|
| `GET /metadata` | `200`, CapabilityStatement; FHIR **4.0.1**, HAPI **8.13.10-SNAPSHOT**. Anuncia JSON, XML e Turtle, entre outros formatos. |
| `GET /Patient?_summary=count` | **3.989 pacientes**. |
| `GET /Observation?_summary=count` | **34.126 observações**. |
| `GET /Patient?_count=100` | 100 pacientes e link `next`. |
| Seguir o `next` recebido | Mais 100 pacientes, sem sobreposição de IDs com a primeira página. O cursor usa `/baseR4?...`, não `/Patient?...`. |
| `GET /Patient?_count=1000` | **500** pacientes: o tamanho solicitado pode ser reduzido pelo servidor. |
| `GET /Patient?_sort=_lastUpdated&_count=10` | Aceito em modo estrito; os 10 timestamps retornados estavam em ordem não decrescente. |
| `GET /Patient?_lastUpdated=ge2026-01-01&_lastUpdated=lt2027-01-01&_summary=count` | Aceito em modo estrito, total 3.989. Não houve teste dos limites exatos da janela. |
| `GET /Patient?_elements=id&_count=2` | Apenas `resourceType`, `id` e `meta` em ambos os recursos. A projeção de campos funcionou neste teste. |
| Parâmetro inexistente + `Prefer: handling=strict` | `400`, OperationOutcome com código `processing`. |
| `GET /Patient?_has:Observation:subject:status=final&_count=2` | `200`; permitiu selecionar pacientes vinculados a observações finais. |
| `GET /Observation?subject=Patient/{id1},Patient/{id2}&_count=5` | 5 observações, todas com subject pertencente aos pacientes selecionados. |
| `GET /Observation/_history?_count=1` | `200`, Bundle com total 37.817. Esse total é de histórico, não de observações atuais. |
| `$export` | Anunciado pelo CapabilityStatement no nível de sistema e Patient. **Não executado nesta verificação.** |

Todas as consultas válidas concluíram com `200`; o `400` foi intencional. Não observamos `429`, cabeçalhos de rate limit ou exigência de autenticação nessas leituras. Isso não demonstra ausência de limites nem acesso irrestrito a todas as operações.

## 2. Como consumir a API no MVP

Enviar `Accept: application/fhir+json`. Para validar filtros durante desenvolvimento, enviar também `Prefer: handling=strict`.

```http
GET /baseR4/Patient?_has:Observation:subject:status=final&_count=20
GET /baseR4/Observation?subject=Patient/{id1},Patient/{id2}&_count=100
```

1. Receber um `Bundle` e extrair os recursos de `entry[].resource`, verificando tipo e eventuais OperationOutcome.
2. Guardar pacientes e resolver `subject.reference` das observações para a chave interna.
3. Seguir `Bundle.link` com `relation=next` até sua ausência ou o teto explícito da amostra. Não reconstruir cursores nem usar `total` como condição de término.
4. Validar esquema HTTPS, host e caminho dos links recebidos. Aceitar a raiz `/baseR4` e seus caminhos; não depender de `/Patient` no link de continuação.
5. Salvar checkpoint somente após persistir a página. Preparar reexecução idempotente e reinício quando o cursor expirar; a duração do cursor não foi medida.

O filtro `_has` facilita uma demonstração com observações, mas seleciona uma população enviesada. Uma migração completa deve incluir também pacientes sem observações e observações de outros status. Dividir listas de referências em lotes configuráveis: só o lote de dois pacientes foi validado aqui, não limites de URL ou lotes maiores.

## 3. Formatos e qualidade observados

A inspeção de conteúdo usou a primeira página de 100 recursos de cada tipo, **sem amostragem aleatória**.

| Amostra | Achados |
|---|---|
| 100 Patient | Todos com `name`; 4 sem `birthDate`; gênero: 57 male, 39 female e 4 ausentes. |
| 100 Observation | Todas `final`, com `valueQuantity`, `code.coding` e subject relativo de Patient; nenhuma com componentes. |

Essa homogeneidade não permite concluir que outros tipos de valor, status ou referências não existam. Não revalidamos os percentuais globais de qualidade registrados no discovery anterior. Para o mapeamento, manter campos opcionais, códigos com sistema, quantidades com unidade/comparador e suporte a componentes; usar fixtures sintéticas para variantes ausentes na amostra. Preservar e sinalizar casos ainda não suportados.

## 4. Desempenho e escala

Na execução completa, as consultas de contagem levaram 0,76–1,00s; páginas de 100 pacientes, 1,44–1,53s; 100 observações, 2,92s; 500 pacientes, 2,22s; `/metadata`, 3,24s. São medições pontuais de cliente/rede, sem teste de carga ou concorrência.

A razão atual é de aproximadamente **8,6 observações por paciente**, mas inclui todas as observações, sem verificar seus vínculos globalmente. Não é base suficiente para prometer volume ou duração da migração de outro sistema. Manter um worker no MVP e tornar tamanho da página, timeout e frequência configuráveis.

## 5. Implicações para o plan2.md

- **O fluxo paginado é viável:** selecionar até 20 pacientes, buscar observações por lotes e aplicar o teto de 1.000 previsto no plano. Limite atingido deve aparecer como importação parcial.
- **Amostra de demonstração:** `_has` evita depender da sorte para encontrar pacientes com observações; não usar esse filtro na carga completa.
- **Economia de tráfego:** `_elements` funcionou, mas a projeção precisa incluir todos os campos do mapeamento; payload projetado não equivale a cópia integral da fonte.
- **Validação de filtros:** usar modo estrito e tratar OperationOutcome; HTTP bem-sucedido sozinho não prova a semântica de todo filtro.
- **Consistência:** as contagens de Patient diferem do relatório anterior (3.978 → 3.989). Reconciliar IDs/versões em uma fonte estabilizada; não comparar contagens capturadas em momentos diferentes como se fossem um snapshot.
- **Carga completa:** `$export` permanece candidato, agora com anúncio confirmado. Ainda falta validar início assíncrono, polling, manifesto, downloads, erros e comportamento de `_since` nesta execução. A disponibilidade anunciada não prova consistência de snapshot ou captura de exclusões.

## 6. Limites e próximos testes

Não foram testados: expiração de cursor, alterações concorrentes, exclusões reais no histórico, retries sob `429`/`5xx`, `$export`, carga completa e autenticação de produção. Nenhuma dessas garantias deve ser inferida deste discovery.

Próximo passo de implementação: extrator paginado da amostra com persistência idempotente. Testar falhas e variantes com fixtures locais, evitando induzir erros ou sobrecarga no servidor público. Antes de migrar 50 mil pacientes, validar exportação e consistência em ensaio dedicado.

Para repetir (requer acesso à rede; substitui apenas o JSON agregado):

```bash
python3 scripts/discover_fhir.py
```
