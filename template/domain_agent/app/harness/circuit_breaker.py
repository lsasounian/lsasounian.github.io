"""
Circuit breaker por resource key (ex.: "mcp:pricing-service", "kb:vendas",
"llm:gpt-5.4"). Estados: CLOSED -> OPEN -> HALF_OPEN -> CLOSED.

LIMITAÇÃO CONHECIDA (documentada também no harness do mediador): o estado
vive em memória do processo, ou seja, é por microVM/instância do AgentCore
Runtime, não distribuído. Se isso virar problema real (muitas instâncias
abrindo/fechando de forma inconsistente), o próximo passo é externalizar
para Redis/DynamoDB — igual ficou registrado como TODO no mediador.
"""

import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(Exception):
    def __init__(self, resource_key: str, retry_after_s: float):
        self.resource_key = resource_key
        self.retry_after_s = retry_after_s
        super().__init__(
            f"circuit breaker OPEN para '{resource_key}', retry em {retry_after_s:.1f}s"
        )


@dataclass
class _BreakerEntry:
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    opened_at: float = 0.0
    half_open_calls_in_flight: int = 0
    lock: Lock = field(default_factory=Lock)


class CircuitBreakerRegistry:
    """Um registry por processo — todas as chamadas externas (MCP, KB, LLM)
    passam por aqui via `guard()`."""

    def __init__(self, failure_threshold: int, open_duration_s: int, half_open_max_calls: int):
        self._entries: dict[str, _BreakerEntry] = {}
        self._registry_lock = Lock()
        self.failure_threshold = failure_threshold
        self.open_duration_s = open_duration_s
        self.half_open_max_calls = half_open_max_calls

    def _get_entry(self, resource_key: str) -> _BreakerEntry:
        with self._registry_lock:
            if resource_key not in self._entries:
                self._entries[resource_key] = _BreakerEntry()
            return self._entries[resource_key]

    def before_call(self, resource_key: str) -> None:
        entry = self._get_entry(resource_key)
        with entry.lock:
            if entry.state is CircuitState.OPEN:
                elapsed = time.monotonic() - entry.opened_at
                if elapsed < self.open_duration_s:
                    raise CircuitBreakerOpenError(
                        resource_key, self.open_duration_s - elapsed
                    )
                entry.state = CircuitState.HALF_OPEN
                entry.half_open_calls_in_flight = 0

            if entry.state is CircuitState.HALF_OPEN:
                if entry.half_open_calls_in_flight >= self.half_open_max_calls:
                    raise CircuitBreakerOpenError(resource_key, self.open_duration_s)
                entry.half_open_calls_in_flight += 1

    def on_success(self, resource_key: str) -> None:
        entry = self._get_entry(resource_key)
        with entry.lock:
            entry.state = CircuitState.CLOSED
            entry.failure_count = 0
            entry.half_open_calls_in_flight = 0

    def on_failure(self, resource_key: str) -> None:
        entry = self._get_entry(resource_key)
        with entry.lock:
            entry.failure_count += 1
            if entry.state is CircuitState.HALF_OPEN:
                # falhou em teste de recuperação -> reabre imediatamente
                entry.state = CircuitState.OPEN
                entry.opened_at = time.monotonic()
                return
            if entry.failure_count >= self.failure_threshold:
                entry.state = CircuitState.OPEN
                entry.opened_at = time.monotonic()

    def state_of(self, resource_key: str) -> CircuitState:
        return self._get_entry(resource_key).state


# Instância única compartilhada pelo processo do agente. Importar esta
# variável nos módulos de tools (mcp_client.py, kb_tool.py).
breaker_registry: CircuitBreakerRegistry | None = None


def init_breaker_registry(
    failure_threshold: int, open_duration_s: int, half_open_max_calls: int
) -> CircuitBreakerRegistry:
    global breaker_registry
    breaker_registry = CircuitBreakerRegistry(
        failure_threshold, open_duration_s, half_open_max_calls
    )
    return breaker_registry
