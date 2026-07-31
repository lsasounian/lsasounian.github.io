"""
Circuit breaker genérico, instanciado por rota/dependência externa. Reaproveitado
aqui para a chamada HTTP da fachada Redis, que agora é uma dependência externa
no caminho crítico do checkpointer e precisa do mesmo tratamento que as rotas MCP.
"""
import asyncio
import logging
import random
import time
from enum import Enum
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    pass


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 15.0,
        max_retries: int = 2,
        base_backoff_s: float = 0.2,
    ):
        self.name = name
        self._failure_threshold = failure_threshold
        self._recovery_timeout_s = recovery_timeout_s
        self._max_retries = max_retries
        self._base_backoff_s = base_backoff_s

        self._state = BreakerState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None

    def _record_success(self) -> None:
        self._failure_count = 0
        self._state = BreakerState.CLOSED

    def _record_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= self._failure_threshold:
            self._state = BreakerState.OPEN
            self._opened_at = time.monotonic()
            logger.warning("breaker=%s state=OPEN failures=%d", self.name, self._failure_count)

    def _can_attempt(self) -> bool:
        if self._state != BreakerState.OPEN:
            return True
        if time.monotonic() - (self._opened_at or 0) >= self._recovery_timeout_s:
            self._state = BreakerState.HALF_OPEN
            return True
        return False

    async def call(self, fn: Callable[..., Awaitable[T]], *args, **kwargs) -> T:
        if not self._can_attempt():
            raise CircuitOpenError(f"breaker={self.name} está OPEN")

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                result = await fn(*args, **kwargs)
                self._record_success()
                return result
            except Exception as exc:  # noqa: BLE001 — breaker precisa capturar qualquer falha de I/O
                last_exc = exc
                self._record_failure()
                if attempt < self._max_retries and self._state != BreakerState.OPEN:
                    backoff = self._base_backoff_s * (2**attempt) + random.uniform(0, 0.1)
                    await asyncio.sleep(backoff)
                    continue
                break
        assert last_exc is not None
        raise last_exc
