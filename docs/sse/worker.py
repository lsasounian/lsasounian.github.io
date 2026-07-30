"""
Worker — consumer do SQS FIFO. Roda como processo/container separado do
sse_handler, de propósito: throttling ou latência aqui nunca deve atrasar a
resposta do agente pro usuário.

Escopo local: persiste em Redis Stream (histórico) com idempotência via
SET NX. O dual-write em DynamoDB (fonte de verdade durável em prod) fica
marcado como extensão — ative com ENABLE_DYNAMODB_WRITE=true e rode o
DynamoDB Local junto no docker-compose se quiser testar esse caminho também;
por padrão fica desligado pra manter o setup local enxuto (só SQS + Redis,
como pedido).
"""

import json
import logging
import os
import signal
import time

import boto3
import redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("worker")

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
SQS_QUEUE_URL = os.environ.get(
    "SQS_QUEUE_URL",
    "http://localhost:4566/000000000000/conversation-history.fifo",
)
AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
DEDUPE_TTL_S = int(os.environ.get("DEDUPE_TTL_S", "86400"))
ENABLE_DYNAMODB_WRITE = os.environ.get("ENABLE_DYNAMODB_WRITE", "false").lower() == "true"

r = redis.from_url(REDIS_URL, decode_responses=True)
sqs = boto3.client("sqs", region_name=AWS_REGION, endpoint_url=AWS_ENDPOINT_URL)

_running = True


def _handle_shutdown(signum, frame):
    global _running
    logger.info("sinal de shutdown recebido, terminando após o batch atual")
    _running = False


signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT, _handle_shutdown)


def process_message(body: dict) -> None:
    session_id = body["session_id"]
    message_id = body["message_id"]
    dedupe_key = f"processed:{message_id}"

    # SET NX = "reservei esse message_id"; se já existir, é redelivery do SQS
    if not r.set(dedupe_key, "1", nx=True, ex=DEDUPE_TTL_S):
        logger.info("message_id=%s já processado (redelivery), pulando", message_id)
        return

    if body.get("kind") == "feedback":
        r.xadd(
            f"session:{session_id}:feedback",
            {
                "message_id": message_id,
                "ref_message_id": body["ref_message_id"],
                "liked": str(body["liked"]),
                "comment": body.get("comment", ""),
                "ts": str(body["ts"]),
            },
        )
        logger.info("feedback gravado — session=%s ref=%s liked=%s", session_id, body["ref_message_id"], body["liked"])
    else:
        r.xadd(
            f"session:{session_id}:history",
            {
                "message_id": message_id,
                "role": body["role"],
                "content": body["content"],
                "ts": str(body["ts"]),
            },
        )
        logger.info("mensagem gravada — session=%s role=%s message_id=%s", session_id, body["role"], message_id)

    if ENABLE_DYNAMODB_WRITE:
        _write_to_dynamodb(session_id, message_id, body)


def _write_to_dynamodb(session_id: str, message_id: str, body: dict) -> None:
    """Extensão pra quando você quiser testar o dual-write completo localmente.
    Requer um serviço dynamodb-local no compose e a tabela criada com:
      PK = SESSION#{session_id}
      SK = MSG#{iso_timestamp}#{message_id}
    Ver architecture.html, seção "Dual-write + idempotência", pro schema completo
    e o ConditionExpression que evita duplicar em retry."""
    raise NotImplementedError("ligar dynamodb-local no compose e implementar aqui, se precisar")


def main():
    logger.info("worker up — REDIS_URL=%s SQS_QUEUE_URL=%s ENABLE_DYNAMODB_WRITE=%s",
                REDIS_URL, SQS_QUEUE_URL, ENABLE_DYNAMODB_WRITE)

    while _running:
        response = sqs.receive_message(
            QueueUrl=SQS_QUEUE_URL,
            MaxNumberOfMessages=5,
            WaitTimeSeconds=10,  # long polling
            MessageAttributeNames=["traceparent"],
        )

        for msg in response.get("Messages", []):
            try:
                body = json.loads(msg["Body"])
                process_message(body)
                sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=msg["ReceiptHandle"])
            except Exception:
                logger.exception("falha processando mensagem, deixando pro retry/DLQ")
                # não deleta — volta pra fila após o visibility timeout

    logger.info("worker encerrado")


if __name__ == "__main__":
    main()
