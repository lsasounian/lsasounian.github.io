# Ambiente local — Agente Mediador via SSE

LocalStack (SQS FIFO) + Redis real + o handler SSE + o worker, tudo em docker-compose.
Sem Azure, sem AWS de verdade — `LOCAL_AUTH_BYPASS=true` e `AGENT_MODE=mock` fazem o
pipeline inteiro funcionar sozinho pra testar SSE, Pub/Sub, fan-out, SQS FIFO e
idempotência.

## Subir o ambiente

```bash
docker compose up --build
```

Isso sobe, nessa ordem:
1. **localstack** — cria a fila `conversation-history.fifo` automaticamente via `localstack-init/create-resources.sh` assim que fica saudável.
2. **redis** — Redis real (ElastiCache localmente é só Redis mesmo).
3. **app** — `sse_handler.py` em `http://localhost:8000`, com `--reload` (edita o arquivo, reinicia sozinho).
4. **worker** — consumer do SQS FIFO, grava no Redis Stream com dedupe.

Confirma que a fila foi criada:
```bash
docker compose logs localstack | grep "fila conversation-history.fifo criada"
```

## Abrir o front

`front/index.html` é estático — não precisa de build. Duas formas:

```bash
# opção simples
cd front && python3 -m http.server 5500
# abre http://localhost:5500
```

Ou só abre o arquivo direto no navegador (`file://`) — como o CORS do `app` está
com `ALLOWED_ORIGINS=*` em dev, funciona também.

O front já vem com os campos de API/session/client/operator preenchidos com
defaults de dev. Ao carregar, ele: pega o stream token (bypass local, sem Azure
de verdade) → abre o SSE → dispara o warmup → carrega o histórico da sessão.
Manda uma mensagem, o agente mock ecoa token a token, e like/dislike vão pro
SQS → worker → Redis Stream.

## Testar sem front (curl)

```bash
SID=teste-$(date +%s)

TOKEN=$(curl -s -X POST http://localhost:8000/stream/token \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SID\",\"client_id\":\"c1\",\"operator_id\":\"o1\"}" | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# num terminal separado, deixa o stream aberto:
curl -N "http://localhost:8000/stream?session_id=$SID&token=$TOKEN"

# noutro terminal:
curl -X POST http://localhost:8000/message \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SID\",\"prompt\":\"oi, tudo bem?\"}"
```

## Verificar o que caiu no Redis

```bash
docker compose exec redis redis-cli XRANGE session:$SID:history - +
```

## Verificar a fila no LocalStack

```bash
docker compose exec localstack awslocal sqs get-queue-attributes \
  --queue-url http://localhost:4566/000000000000/conversation-history.fifo \
  --attribute-names ApproximateNumberOfMessages
```

## Variáveis de ambiente relevantes

| Var | Local (compose) | Prod |
|---|---|---|
| `LOCAL_AUTH_BYPASS` | `true` — aceita claims do body, sem Azure | **precisa ser `false`** — nunca commitar `true` |
| `AGENT_MODE` | `mock` — ecoa o prompt | `real` — plugar LangGraph/AgentCore em `invoke_agent_streaming` |
| `AWS_ENDPOINT_URL` | `http://localstack:4566` | não setado — boto3 usa a AWS real |
| `ENABLE_DYNAMODB_WRITE` (worker) | `false` | `true` quando o dual-write real estiver plugado |

## O que este setup local **não** cobre

- DynamoDB (dual-write fica documentado em `worker.py`/`architecture.html`, mas
  não sobe `dynamodb-local` aqui — só SQS + Redis, como pedido).
- CloudFront/ALB/API Gateway — em dev tudo bate direto em `localhost:8000`.
- Validação real do bearer Azure — `LOCAL_AUTH_BYPASS` existe só pra isso não
  ser um bloqueio pra testar o resto.
