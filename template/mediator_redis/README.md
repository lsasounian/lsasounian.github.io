# Agente Mediador com Harness — AgentCore + Redis cross-account proibido

## Arquitetura

```
SDK fixo (agentId, sessionId, prompt) 
  -> main.py (harness: parse, contexto, warmup, lifecycle)
    -> app/graph.py (LangGraph supervisor — "o que fazer")
      -> app/mcp_client.py -> Gateway MCP -> especialistas
      -> app/harness/redis_checkpointer.py -> API Gateway + Lambda (mTLS) -> Redis (sua conta)
```

Premissa que molda todo o design: o AgentCore Runtime está numa conta AWS sem
controle e sem acesso cross-account. Redis não pode ser alcançado diretamente
(ElastiCache não tem endpoint público) — a única via viável é uma fachada HTTP
autenticada por mTLS, tratada pelo harness como mais uma dependência externa
sujeita a circuit breaker, timeout e degradação controlada.

## O que está completo

- Separação harness/grafo (`main.py` nunca conhece lógica de negócio; `app/graph.py`
  nunca conhece AWS, Redis ou o contrato do SDK).
- `AgentState` com reducer `keep_existing` para `id_cliente` — capturado uma vez
  via `systemMessage` no primeiro turno, sobrevive nos turnos seguintes via checkpoint.
- Propagação de `session_id`/`agent_id`/`id_cliente` até o MCP via `contextvars`
  + headers HTTP — nunca entra no schema de tool, a LLM não decide isso.
- `HTTPRedisSaver`: checkpointer que fala com o Redis via fachada, com circuit
  breaker e degradação silenciosa (perde memória de curto prazo em vez de 5xx).
- Warmup (`__warmup__` sentinel) + prewarm (compila grafo, descobre tools MCP)
  + pre-bake de bytecode no Dockerfile.
- Cleanup explícito de sessão (`end_session` no payload → espelha `StopRuntimeSession`).

## O que precisa ser plugado (placeholders explícitos no código)

- `app/graph.py::route` — roteador GPT-5.4 com saída estruturada do projeto original.
- `app/graph.py::prepare_context` — pipeline RAG real (embed → retrieve → rerank).
- Contrato da fachada Redis (`/sessions/{id}/checkpoint`, `/writes`, `/checkpoints`)
  precisa existir do lado de API Gateway + Lambda, ver schema de chaves discutido
  na conversa (`agentcore:sess:{session_id}:ckpt:*`, TTL com teto absoluto de 8h).
- Certificados mTLS (`REDIS_FACADE_MTLS_CERT_PATH`/`_KEY_PATH`) — provisionar via
  Secrets Manager + init container ou volume montado, não hardcoded na imagem.

## Variáveis de ambiente

Ver `app/config.py` — todas com default sensato exceto as URLs/ARNs, que são
obrigatórias em produção (`REDIS_FACADE_BASE_URL`, `MCP_GATEWAY_URL`,
`MCP_GATEWAY_TOKEN_SECRET_ARN`).
