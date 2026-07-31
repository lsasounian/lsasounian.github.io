"""
Retry com exponential backoff + full jitter (AWS builders' library pattern).
Usado em conjunto com o circuit breaker: cada tentativa passa por
`breaker.before_call()` / `on_success()` / `on_failure()`.
"""

import asyncio
import random
from typing import Awaitable, Callable, TypeVar

from app.harness.circuit_breaker import CircuitBreakerOpenError, CircuitBreakerRegistry

T = TypeVar("T")


class RetryExhaustedError(Exception):
    def __init__(self, resource_key: str, attempts: int, last_error: Exception):
        self.resource_key = resource_key
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"'{resource_key}' falhou após {attempts} tentativas: {last_error}"
        )


def _full_jitter_delay(attempt: int, base_ms: int, max_ms: int) -> float:
    cap = min(max_ms, base_ms * (2 ** attempt))
    return random.uniform(0, cap) / 1000


async def call_with_resilience(
    resource_key: str,
    fn: Callable[[], Awaitable[T]],
    breaker: CircuitBreakerRegistry,
    max_attempts: int,
    base_delay_ms: int,
    max_delay_ms: int,
    retriable_exceptions: tuple[type[Exception], ...] = (Exception,),
) -> T:
    """Wrapper único para toda chamada externa (MCP tool, KB SDK, LLM).

    TODO(time): ajustar `retriable_exceptions` por integração — erros de
    validação/4xx normalmente NÃO devem ser retriados, só 5xx/timeout/conexão.
    """
    last_error: Exception | None = None

    for attempt in range(max_attempts):
        try:
            breaker.before_call(resource_key)
        except CircuitBreakerOpenError:
            raise  # não retria breaker aberto — falha rápido, por design

        try:
            result = await fn()
            breaker.on_success(resource_key)
            return result
        except retriable_exceptions as exc:
            breaker.on_failure(resource_key)
            last_error = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(_full_jitter_delay(attempt, base_delay_ms, max_delay_ms))

    raise RetryExhaustedError(resource_key, max_attempts, last_error)
