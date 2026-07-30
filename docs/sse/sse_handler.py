"""
SSE handler para o agente mediador — ECS Fargate atrás de ALB interno (prod)
ou docker-compose com LocalStack + Redis local (dev).

Toda config sensível a ambiente vem de variável de ambiente, com defaults que
já funcionam contra o docker-compose.yml entregue junto (ver README.md).

Peças cobertas:
  - POST /stream/token   -> token exchange (bearer Azure -> JWT curto pro EventSource)
  - GET  /stream          -> conexão SSE, replay via Last-Event-ID + fan-out via Redis Pub/Sub
  - POST /warmup          -> dispara "__warmup__" no agente, seta estado warm:{id}
  - POST /message         -> invoca agente + persiste via SQS FIFO (dual-write assíncrono)
  - POST /feedback        -> like/dislike + comentário, mesmo padrão fire-and-forget
  - GET  /session/{id}    -> histórico da sessão (Redis Stream) pro front carregar ao abrir

AGENT_MODE=mock (default) usa um agente fake que ecoa o prompt token a token,
suficiente pra testar o pipeline inteiro (SSE, Pub/Sub, SQS, Redis) sem depender
do LangGraph/Bedrock real. Troque pra AGENT_MODE=real e implemente invoke_agent /
invoke_agent_streaming pra plugar o agente de verdade.
"""

import asyncio
import json
import logging
import os
import signal
import time
import uuid
from contextlib import asynccontextmanager

import boto3
import jwt
import redis.asyncio as aioredis
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from opentelemetry import propagate, trace

logger = logging.getLogger("sse_handler")
tracer = trace.get_tracer(__name__)

# ---------------------------------------------------------------------------
# Config — tudo via env, com defaults pro docker-compose local
# ---------------------------------------------------------------------------

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
STREAM_SIGNING_KEY = os.environ.get("STREAM_SIGNING_KEY", "local-dev-secret-troque-em-prod")
STREAM_TOKEN_TTL_S = int(os.environ.get("STREAM_TOKEN_TTL_S", "60"))
WARM_STATE_TTL_S = int(os.environ.get("WARM_STATE_TTL_S", "300"))
DEDUPE_TTL_S = int(os.environ.get("DEDUPE_TTL_S", "86400"))
HEARTBEAT_INTERVAL_S = int(os.environ.get("HEARTBEAT_INTERVAL_S", "15"))
SQS_QUEUE_URL = os.environ.get(
    "SQS_QUEUE_URL",
    "http://localhost:4566/000000000000/conversation-history.fifo",
)
AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL")  # setado pro LocalStack em dev; None em prod
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

# NUNCA true em prod — bypassa validação real do Azure e aceita claims do body.
# Existe só pra rodar o front localmente sem depender do EntraID.
LOCAL_AUTH_BYPASS = os.environ.get("LOCAL_AUTH_BYPASS", "false").lower() == "true"

# "mock" ecoa o prompt (bom pra testar o pipeline); "real" chama invoke_agent de verdade
AGENT_MODE = os.environ.get("AGENT_MODE", "mock")

ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "http://localhost:5500,http://127.0.0.1:5500").split(",")

redis_client: aioredis.Redis | None = None
sqs_client = boto3.client(
    "sqs",
    region_name=AWS_REGION,
    endpoint_url=AWS_ENDPOINT_URL,  # None -> boto3 usa o endpoint real da AWS
)

shutdown_event = asyncio.Event()
active_connections: set[asyncio.Queue] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)

    loop = asyncio.get_event_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(_handle_sigterm()))
    except NotImplementedError:
        # Windows não suporta add_signal_handler — sem problema pro fluxo de dev
        pass

    logger.info("sse_handler up — REDIS_URL=%s SQS_QUEUE_URL=%s AGENT_MODE=%s LOCAL_AUTH_BYPASS=%s",
                REDIS_URL, SQS_QUEUE_URL, AGENT_MODE, LOCAL_AUTH_BYPASS)
    yield
    await redis_client.close()


async def _handle_sigterm():
    """ECS manda SIGTERM antes de matar a task no rolling deploy. Sem isso, toda
    conexão SSE aberta morre em silêncio e o front só percebe pelo timeout de leitura."""
    logger.info("SIGTERM recebido, avisando %d conexões SSE ativas", len(active_connections))
    shutdown_event.set()
    await asyncio.sleep(5)


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Auth — token exchange
# ---------------------------------------------------------------------------
# EventSource nativo do browser não permite header Authorization customizado.
# Solução: troca o bearer real do Azure (que vai em header, sem essa limitação)
# por um JWT curto que vai na query string do /stream.

def validate_azure_bearer(authorization: str, body: dict) -> dict:
    """Em prod: valida contra EntraID / cache de ACL (DynamoDB), mesmo padrão
    fail-closed do M2M auth existente. Em dev (LOCAL_AUTH_BYPASS=true): aceita
    as claims direto do body, sem tocar em rede nenhuma — só pra testar o resto
    do pipeline sem precisar de um bearer real do Azure."""
    if LOCAL_AUTH_BYPASS:
        return {
            "client_id": body.get("client_id", "dev-client"),
            "operator_id": body.get("operator_id", "dev-operator"),
            "session_id": body["session_id"],
        }

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    # ... validação real (JWKS do Azure, escopos, fail-closed) ...
    raise NotImplementedError("plugar validação real do bearer Azure aqui")


@app.post("/stream/token")
async def issue_stream_token(request: Request, authorization: str = Header(None)):
    with tracer.start_as_current_span("issue_stream_token"):
        body = await request.json()
        claims = validate_azure_bearer(authorization, body)
        now = int(time.time())
        short_token = jwt.encode(
            {
                "session_id": claims["session_id"],
                "client_id": claims["client_id"],
                "operator_id": claims["operator_id"],
                "scope": "stream",
                "iat": now,
                "exp": now + STREAM_TOKEN_TTL_S,
            },
            STREAM_SIGNING_KEY,
            algorithm="HS256",
        )
        return {"token": short_token, "expires_in": STREAM_TOKEN_TTL_S}


def validate_stream_token(token: str) -> dict:
    try:
        return jwt.decode(token, STREAM_SIGNING_KEY, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="stream token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid stream token")


# ---------------------------------------------------------------------------
# GET /stream — conexão SSE
# ---------------------------------------------------------------------------

@app.get("/stream")
async def stream(session_id: str, token: str, request: Request):
    claims = validate_stream_token(token)
    if claims["session_id"] != session_id:
        raise HTTPException(status_code=403, detail="token não corresponde à sessão")

    last_event_id = request.headers.get("Last-Event-ID")

    async def event_generator():
        queue: asyncio.Queue = asyncio.Queue()
        active_connections.add(queue)
        history_key = f"session:{session_id}:history"
        channel = f"session:{session_id}:events"
        pubsub = None

        try:
            if last_event_id:
                missed = await redis_client.xrange(history_key, min=f"({last_event_id}", max="+")
                for entry_id, fields in missed:
                    yield _format_sse(entry_id, fields.get("role", "assistant"), fields)

            if await redis_client.get(f"warm:{session_id}"):
                yield _format_sse(str(int(time.time() * 1000)), "ready", {})

            pubsub = redis_client.pubsub()
            await pubsub.subscribe(channel)

            last_heartbeat = time.monotonic()
            while not await request.is_disconnected() and not shutdown_event.is_set():
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message["type"] == "message":
                    payload = json.loads(message["data"])
                    yield _format_sse(payload["id"], payload["event"], payload["data"])
                    last_heartbeat = time.monotonic()
                elif time.monotonic() - last_heartbeat > HEARTBEAT_INTERVAL_S:
                    yield ": keep-alive\n\n"
                    last_heartbeat = time.monotonic()

            if shutdown_event.is_set():
                yield _format_sse(str(int(time.time() * 1000)), "reconnect", {})

        finally:
            active_connections.discard(queue)
            if pubsub:
                await pubsub.unsubscribe(channel)
                await pubsub.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _format_sse(event_id: str, event: str, data: dict) -> str:
    return f"id: {event_id}\nevent: {event}\nretry: 3000\ndata: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# GET /session/{id} — histórico pro front carregar ao abrir
# ---------------------------------------------------------------------------

@app.get("/session/{session_id}")
async def get_session_history(session_id: str):
    history_key = f"session:{session_id}:history"
    entries = await redis_client.xrange(history_key, min="-", max="+")
    messages = [{"id": entry_id, **fields} for entry_id, fields in entries]
    return {"session_id": session_id, "exists": len(messages) > 0, "messages": messages}


# ---------------------------------------------------------------------------
# POST /warmup
# ---------------------------------------------------------------------------

@app.post("/warmup")
async def warmup(request: Request):
    body = await request.json()
    session_id = body["session_id"]

    with tracer.start_as_current_span("warmup") as span:
        span.set_attribute("session_id", session_id)
        asyncio.create_task(_run_warmup(session_id))
        return {"status": "accepted"}


async def _run_warmup(session_id: str):
    await invoke_agent(session_id=session_id, prompt="__warmup__")
    await redis_client.set(f"warm:{session_id}", "1", ex=WARM_STATE_TTL_S)
    await _publish_event(session_id, event="ready", data={})


# ---------------------------------------------------------------------------
# POST /message
# ---------------------------------------------------------------------------

@app.post("/message")
async def post_message(request: Request):
    body = await request.json()
    session_id = body["session_id"]
    prompt = body["prompt"]
    message_id = str(uuid.uuid4())

    with tracer.start_as_current_span("post_message") as span:
        span.set_attribute("session_id", session_id)
        span.set_attribute("message_id", message_id)

        asyncio.create_task(_generate_and_stream(session_id, message_id, prompt))
        await _enqueue_for_persistence(session_id, message_id, role="user", content=prompt)

        return {"status": "accepted", "message_id": message_id}


async def _generate_and_stream(session_id: str, message_id: str, prompt: str):
    full_response = []
    async for chunk in invoke_agent_streaming(session_id=session_id, prompt=prompt):
        full_response.append(chunk)
        await _publish_event(session_id, event="token", data={"message_id": message_id, "chunk": chunk})

    await _publish_event(session_id, event="done", data={"message_id": message_id})

    assistant_message_id = str(uuid.uuid4())
    await _enqueue_for_persistence(
        session_id, assistant_message_id, role="assistant", content="".join(full_response)
    )


async def _publish_event(session_id: str, event: str, data: dict):
    channel = f"session:{session_id}:events"
    payload = {"id": str(int(time.time() * 1000)), "event": event, "data": data}
    await redis_client.publish(channel, json.dumps(payload))


async def _enqueue_for_persistence(session_id: str, message_id: str, role: str, content: str):
    carrier = {}
    propagate.inject(carrier)

    sqs_client.send_message(
        QueueUrl=SQS_QUEUE_URL,
        MessageBody=json.dumps(
            {
                "session_id": session_id,
                "message_id": message_id,
                "role": role,
                "content": content,
                "ts": time.time(),
            }
        ),
        MessageGroupId=session_id,
        MessageDeduplicationId=message_id,
        MessageAttributes={
            "traceparent": {"DataType": "String", "StringValue": carrier.get("traceparent", "") or "none"}
        },
    )


# ---------------------------------------------------------------------------
# POST /feedback
# ---------------------------------------------------------------------------

@app.post("/feedback")
async def post_feedback(request: Request):
    body = await request.json()
    feedback_id = str(uuid.uuid4())

    with tracer.start_as_current_span("post_feedback"):
        carrier = {}
        propagate.inject(carrier)
        sqs_client.send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps(
                {
                    "session_id": body["session_id"],
                    "message_id": feedback_id,
                    "kind": "feedback",
                    "ref_message_id": body["message_id"],
                    "liked": body["liked"],
                    "comment": body.get("comment", ""),
                    "ts": time.time(),
                }
            ),
            MessageGroupId=body["session_id"],
            MessageDeduplicationId=feedback_id,
            MessageAttributes={
                "traceparent": {"DataType": "String", "StringValue": carrier.get("traceparent", "") or "none"}
            },
        )
        return {"status": "accepted"}


# ---------------------------------------------------------------------------
# Ponto de integração com o agente
# ---------------------------------------------------------------------------

async def invoke_agent(session_id: str, prompt: str) -> str:
    """Chamada não-streaming, usada pelo warmup."""
    if AGENT_MODE == "mock":
        await asyncio.sleep(0.2)
        return "ok"
    # ex.: await agentcore_client.invoke(session_id=session_id, input={"prompt": prompt})
    raise NotImplementedError("AGENT_MODE=real requer invoke_agent implementado")


async def invoke_agent_streaming(session_id: str, prompt: str):
    """Generator async. Em AGENT_MODE=mock, ecoa o prompt palavra a palavra com
    um delay pra simular streaming de verdade — suficiente pra testar SSE,
    Pub/Sub e persistência ponta a ponta sem depender do agente real.

    Em AGENT_MODE=real, plugar aqui o graph.astream_events do LangGraph:

        async for event in graph.astream_events(
            {"messages": [prompt]},
            config={"configurable": {"thread_id": session_id}},
            version="v2",
        ):
            if event["event"] == "on_chat_model_stream":
                yield event["data"]["chunk"].content
    """
    if AGENT_MODE == "mock":
        canned = f"Recebi: \"{prompt}\". Isso é uma resposta simulada streamada token a token."
        for word in canned.split(" "):
            await asyncio.sleep(0.08)
            yield word + " "
        return

    raise NotImplementedError("AGENT_MODE=real requer invoke_agent_streaming implementado")
    yield  # pragma: no cover
