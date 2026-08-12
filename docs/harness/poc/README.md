# Mediator Harness POC

Single-agent harness que descobre skills e RAG via MCP (não hardcoded no
código do agente), refina a pergunta do usuário antes de rotear, e é exposto
via FastAPI.

## Rodar

```bash
cp .env.example .env
# preencha AWS_* (precisa de acesso ao Bedrock) -- LANGFUSE_* é opcional
docker compose up --build
```

## Testar

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "quero contestar uma cobranca no cartao", "thread_id": "t1"}'
```

## Estrutura

- `app/mcp/` -- MCP server: catálogo de skills (`catalog.py`) + RAG genérico
  por kb_id (`kb_store.py`), expostos como tools em `server.py`
- `app/agent/` -- grafo LangGraph: `refine_query -> router -> skill_loader ->
  agent <-> tools`
- `app/api/` -- FastAPI, endpoint `POST /chat`
- `app/telemetry.py` -- OTel -> Langfuse via OTLP/HTTP

## Pendências conhecidas (herdadas do design anterior)

- `instructions` de `pf` e `seguros` em `app/mcp/catalog.py` são placeholder.
- Catálogo e KB são in-memory -- trocar por DynamoDB e um vector store real
  ao evoluir pra produção, mantendo a mesma assinatura de função.
- Sem `clarify_node` / `interrupt()` implementado nesse POC.
