"""
Checkpointer que fala com o Redis através da fachada HTTP (API Gateway + Lambda),
já que cross-account é proibido e o Redis não pode ser alcançado diretamente.

Implementa a interface assíncrona de BaseCheckpointSaver do LangGraph. Confirme
os métodos exatos (aget_tuple/aput/aput_writes/alist) contra a versão instalada
em langgraph.checkpoint.base — a ABC evolui entre releases.

Decisão de degradação: quando o breaker está OPEN, aget_tuple/aput/aput_writes
NÃO lançam para o chamador — leitura vira "sem checkpoint prévio" e escrita vira
no-op logado. Perder memória de curto prazo numa janela de instabilidade é melhor
que derrubar a invocação inteira com 5xx.
"""
import logging
from typing import Any, AsyncIterator, Optional

import httpx
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.harness.circuit_breaker import CircuitBreaker, CircuitOpenError

logger = logging.getLogger(__name__)


class HTTPRedisSaver(BaseCheckpointSaver):
    def __init__(
        self,
        base_url: str,
        breaker: CircuitBreaker,
        mtls_cert: str,
        mtls_key: str,
        timeout_s: float,
    ):
        super().__init__()
        self._breaker = breaker
        self._serde = JsonPlusSerializer()
        self._client = httpx.AsyncClient(
            base_url=base_url,
            cert=(mtls_cert, mtls_key),
            timeout=timeout_s,
        )

    async def aget_tuple(self, config: dict) -> Optional[CheckpointTuple]:
        session_id = config["configurable"]["thread_id"]
        try:
            resp = await self._breaker.call(self._client.get, f"/sessions/{session_id}/checkpoint")
        except CircuitOpenError:
            logger.error("redis-facade OPEN — sessão %s começa sem checkpoint", session_id)
            return None

        if resp.status_code == 404:
            return None
        resp.raise_for_status()

        body = resp.json()
        checkpoint: Checkpoint = self._serde.loads_typed(("json", body["checkpoint"]))
        metadata: CheckpointMetadata = self._serde.loads_typed(("json", body["metadata"]))
        return CheckpointTuple(config=config, checkpoint=checkpoint, metadata=metadata)

    async def aput(
        self,
        config: dict,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: dict,
    ) -> dict:
        session_id = config["configurable"]["thread_id"]
        payload = {
            "checkpoint": self._serde.dumps_typed(checkpoint)[1],
            "metadata": self._serde.dumps_typed(metadata)[1],
        }
        try:
            await self._breaker.call(self._client.put, f"/sessions/{session_id}/checkpoint", json=payload)
        except CircuitOpenError:
            logger.error("redis-facade OPEN — checkpoint da sessão %s não persistido", session_id)
        return config

    async def aput_writes(self, config: dict, writes: list, task_id: str) -> None:
        session_id = config["configurable"]["thread_id"]
        payload = {"task_id": task_id, "writes": [self._serde.dumps_typed(w)[1] for w in writes]}
        try:
            await self._breaker.call(self._client.post, f"/sessions/{session_id}/writes", json=payload)
        except CircuitOpenError:
            logger.error("redis-facade OPEN — pending writes da sessão %s não persistidos", session_id)

    async def alist(self, config: dict, **kwargs: Any) -> AsyncIterator[CheckpointTuple]:
        session_id = config["configurable"]["thread_id"]
        try:
            resp = await self._breaker.call(self._client.get, f"/sessions/{session_id}/checkpoints")
        except CircuitOpenError:
            logger.error("redis-facade OPEN — histórico da sessão %s indisponível", session_id)
            return
        resp.raise_for_status()
        for item in resp.json():
            checkpoint = self._serde.loads_typed(("json", item["checkpoint"]))
            metadata = self._serde.loads_typed(("json", item["metadata"]))
            yield CheckpointTuple(config=config, checkpoint=checkpoint, metadata=metadata)

    async def cleanup(self, session_id: str) -> None:
        """Espelha o StopRuntimeSession explícito — idempotente."""
        try:
            resp = await self._client.delete(f"/sessions/{session_id}")
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()
        except httpx.HTTPError:
            logger.warning("falha ao limpar sessão %s na fachada Redis", session_id, exc_info=True)
