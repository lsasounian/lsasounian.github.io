# Template de agente filho — AgentCore Runtime

Base padrão para os times criarem agentes especialistas invocados pelo
mediador via MCP. Cobre: harness completo, MCP client (tool calling
externo), e KBs expostas como tool com gate por intent classificado.

## O que este template resolve

| Camada | Arquivo | Decisão |
|---|---|---|
| Circuit breaker | `harness/circuit_breaker.py` | CLOSED/OPEN/HALF_OPEN por resource key (`mcp:*`, `kb:*`, `llm`). Estado por microVM — não distribuído (mesma limitação já aceita no mediador). |
| Timeout em cascata | `harness/timeout_budget.py` | Budget total fatiado por fase; uma fase lenta não rouba tempo de outra sem log. |
| Retry | `harness/retry.py` | Exponential backoff + full jitter, integrado ao breaker (não retria breaker aberto). |
| Guardrails | `harness/guardrails.py` | Input size, allowlist de intent, threshold de confiança, limite de iterações de tool. |
| Observability | `harness/observability.py` | OTel + correlação `trace_id`/`session_id` pra juntar spans do mediador com os deste agente. |
| Lifecycle | `harness/lifecycle.py` | `_prewarm()`, sentinel `__warmup__`, `/ping` sem tocar `time_of_last_update` (evita a armadilha do idle timeout). |

## O que NÃO está neste template

Como vocês disseram que já resolvem a exposição deste agente como tool MCP
pro mediador por conta própria, este template cobre só o handler
`/invocations` que essa camada de exposição acaba chamando — não há um
MCP *server* aqui dentro.

## Decisão de design: KB como tool gated por intent

Pedido original: KB precisa ser chamada como tool (não RAG-as-context cego)
porque isso empiricamente deu respostas mais precisas, já que existe um
"filtro de intent" no meio do caminho.

Implementação (`graph/nodes.py` + `tools/kb_tool.py`):

1. `classify_intent` roda **antes** do reasoning principal e produz um
   `IntentResult` estruturado (`needs_kb`, `kb_targets`).
2. `bind_tools_and_reason` só inclui as tools `retrieve_<kb_name>` no
   `bind_tools()` do turno se o intent classificado sinalizou relevância
   — o LLM nem vê a tool existir fora disso.
3. Dado que cada agente tem só 1-2 KBs, cada KB vira sua própria tool
   nomeada (`retrieve_vendas`, `retrieve_rh`, ...) — não há necessidade
   de uma tool "router" genérica selecionando entre elas.
4. Dentro do turno liberado, o LLM ainda decide via function calling
   normal *se* e *com qual query* chamar — o gate por intent só restringe
   o menu de opções, não substitui a decisão do LLM.

Isso separa dois sinais que ficariam misturados se você usasse só
function-calling a partir do prompt cru: "este turno pode precisar de
uma KB" (decisão do classificador, mais robusta, com contexto de
conversa) vs. "esta pergunta específica precisa buscar isso agora, com
esta query" (decisão do LLM no reasoning, mais granular).

**Dependência em aberto:** `tools/kb_tool.py::SimilaritySearchClient` é
um `Protocol` — o SDK próprio de RAG por similaridade que vocês vão
construir precisa só implementar `async def search(kb_name, query, top_k,
similarity_threshold) -> list[str]`. Até lá, `_kb_search_client = None`
em `main.py` e o agente simplesmente nunca oferece tool de KB.

## Checklist de customização por time

- [ ] `config.py`: `AGENT_NAME`, `mcp_servers`, `kbs` (nome + `intent_tags`), `allowed_intents`
- [ ] `prompts/system_prompt.py`: todas as seções marcadas `TODO(time)`
- [ ] `graph/nodes.py::classify_intent`: taxonomia real de intents + `llm.with_structured_output`
- [ ] `main.py`: plugar `SimilaritySearchClient` real e provider de LLM real
- [ ] `graph/nodes.py::final_guardrail`: validações de output específicas do domínio

## Não fizemos ainda (próximos passos em aberto)

- Circuit breaker distribuído (Redis/DynamoDB) — só vira prioridade se
  múltiplas instâncias abrindo/fechando de forma inconsistente virar
  problema real observado.
- `classify_intent` ainda é um placeholder retornando `unclassified` —
  precisa da chamada estruturada real antes de qualquer agente ir pra
  produção.
