"""
Agente Mediador com Harness
============================
Arquitetura: Supervisor (mediador) captura intenção -> roteia para
especialistas expostos via MCP (Streamable HTTP).

Conceito de HARNESS aplicado:
O "harness" é a camada de infraestrutura que envolve o agente — não é o
grafo em si, mas tudo que controla sua execução:
  1. Ciclo de vida (init, warmup, shutdown)
  2. Controle de execução (timeout budget, limite de iterações, retry)
  3. Resiliência (circuit breaker por especialista, fallback)
  4. Observabilidade (tracing, métricas, correlação por thread_id)
  5. Guardrails (validação de entrada/saída, allowlist de rotas)

O LLM decide "o quê"; o harness decide "como, quando e sob quais limites".
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Any, Literal, Optional

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel, Field

logger = logging.getLogger("mediator.harness")

# =====================================================================
# 1. CONFIGURAÇÃO DO HARNESS
# =====================================================================

@dataclass(frozen=True)
class HarnessConfig:
    """Limites operacionais — o contrato de execução do harness."""
    max_iterations: int = 8              # evita loops infinitos do grafo
    total_timeout_s: float = 60.0        # budget total da requisição
    specialist_timeout_s: float = 25.0   # budget por chamada a especialista
    max_retries: int = 2
    retry_base_delay_s: float = 0.5      # backoff exponencial c/ jitter
    circuit_failure_threshold: int = 3
    circuit_reset_timeout_s: float = 30.0
    allowed_routes: tuple[str, ...] = ("billing", "technical", "rag", "fallback")


# =====================================================================
# 2. CIRCUIT BREAKER (por especialista)
# =====================================================================

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Isola especialistas degradados. Um breaker por rota."""

    def __init__(self, threshold: int, reset_timeout_s: float) -> None:
        self._threshold = threshold
        self._reset_timeout_s = reset_timeout_s
        self._failures = 0
        self._state = CircuitState.CLOSED
        self._opened_at: float = 0.0

    @property
    def state(self) -> CircuitState:
        if (
            self._state is CircuitState.OPEN
            and time.monotonic() - self._opened_at >= self._reset_timeout_s
        ):
            self._state = CircuitState.HALF_OPEN
        return self._state

    def allow(self) -> bool:
        return self.state is not CircuitState.OPEN

    def record_success(self) -> None:
        self._failures = 0
        self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()
            logger.warning("Circuit OPEN (failures=%d)", self._failures)


# =====================================================================
# 3. ESTADO DO GRAFO
# =====================================================================

class IntentClassification(BaseModel):
    """Saída estruturada do roteador — validada pelo harness."""
    route: Literal["billing", "technical", "rag", "fallback"] = Field(
        description="Especialista de destino"
    )
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(description="Justificativa curta do roteamento")


class MediatorState(BaseModel):
    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)
    intent: Optional[IntentClassification] = None
    specialist_result: Optional[str] = None
    iterations: int = 0
    errors: list[str] = Field(default_factory=list)

    class Config:
        arbitrary_types_allowed = True


# =====================================================================
# 4. O HARNESS
# =====================================================================

class AgentHarness:
    """
    Envelope de execução do agente mediador.

    Responsabilidades (o grafo NÃO conhece nada disso):
      - gerenciar conexões MCP (warmup, reconexão)
      - aplicar timeout budget em cascata
      - retry com backoff exponencial + jitter
      - circuit breaker por especialista
      - guardrails de entrada/saída
      - métricas e correlação por thread_id
    """

    def __init__(
        self,
        config: HarnessConfig,
        mcp_servers: dict[str, dict[str, Any]],
        routing_model: str = "gpt-4o",
    ) -> None:
        self.config = config
        self._mcp_servers = mcp_servers
        self._mcp_client: Optional[MultiServerMCPClient] = None
        self._tools_by_route: dict[str, list[Any]] = {}
        self._breakers: dict[str, CircuitBreaker] = {
            route: CircuitBreaker(
                config.circuit_failure_threshold,
                config.circuit_reset_timeout_s,
            )
            for route in config.allowed_routes
        }
        self._router_llm = ChatOpenAI(model=routing_model, temperature=0)
        self._checkpointer = MemorySaver()
        self._graph = self._build_graph()
        self._metrics: dict[str, int] = {"requests": 0, "retries": 0, "fallbacks": 0}

    # ------------------------------------------------------------------
    # 4.1 Ciclo de vida
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Warmup: conecta MCP e pré-carrega tools ANTES da 1ª requisição."""
        self._mcp_client = MultiServerMCPClient(self._mcp_servers)
        all_tools = await self._mcp_client.get_tools()
        for tool in all_tools:
            # Convenção: nome da tool prefixado com a rota (ex.: billing__get_invoice)
            route = tool.name.split("__", 1)[0]
            self._tools_by_route.setdefault(route, []).append(tool)
        logger.info(
            "Harness pronto. Rotas com tools: %s", list(self._tools_by_route)
        )

    async def shutdown(self) -> None:
        self._mcp_client = None
        logger.info("Harness finalizado. Métricas: %s", self._metrics)

    @asynccontextmanager
    async def lifespan(self):
        """Para integrar com FastAPI: app = FastAPI(lifespan=harness.lifespan)"""
        await self.startup()
        try:
            yield
        finally:
            await self.shutdown()

    # ------------------------------------------------------------------
    # 4.2 Guardrails
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_input(text: str) -> str:
        if not text or not text.strip():
            raise ValueError("Entrada vazia")
        if len(text) > 8_000:
            raise ValueError("Entrada excede limite")
        return text.strip()

    def _validate_route(self, route: str) -> str:
        """Allowlist: mesmo que o LLM alucine uma rota, o harness bloqueia."""
        if route not in self.config.allowed_routes:
            logger.warning("Rota inválida do LLM: %r -> fallback", route)
            return "fallback"
        return route

    # ------------------------------------------------------------------
    # 4.3 Execução resiliente (retry + circuit breaker + timeout)
    # ------------------------------------------------------------------

    async def _call_with_resilience(self, route: str, coro_factory) -> str:
        breaker = self._breakers[route]
        if not breaker.allow():
            self._metrics["fallbacks"] += 1
            raise RuntimeError(f"Circuit OPEN para '{route}'")

        last_exc: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                result = await asyncio.wait_for(
                    coro_factory(),
                    timeout=self.config.specialist_timeout_s,
                )
                breaker.record_success()
                return result
            except (asyncio.TimeoutError, ConnectionError, RuntimeError) as exc:
                last_exc = exc
                breaker.record_failure()
                if attempt < self.config.max_retries:
                    import random
                    delay = self.config.retry_base_delay_s * (2 ** attempt)
                    delay += random.uniform(0, delay * 0.3)  # jitter
                    self._metrics["retries"] += 1
                    logger.info(
                        "Retry %d rota=%s em %.2fs (%s)",
                        attempt + 1, route, delay, exc,
                    )
                    await asyncio.sleep(delay)
        raise RuntimeError(f"Especialista '{route}' esgotou retries") from last_exc

    # ------------------------------------------------------------------
    # 4.4 Nós do grafo
    # ------------------------------------------------------------------

    async def _classify_intent(self, state: MediatorState) -> dict:
        """Nó roteador: LLM com saída estruturada + validação do harness."""
        user_msg = state.messages[-1].content
        structured = self._router_llm.with_structured_output(IntentClassification)
        intent: IntentClassification = await structured.ainvoke(
            [
                SystemMessage(
                    content=(
                        "Você é um roteador. Classifique a intenção do usuário "
                        f"em uma das rotas: {self.config.allowed_routes}. "
                        "Use 'fallback' se nenhuma se aplicar."
                    )
                ),
                HumanMessage(content=str(user_msg)),
            ]
        )
        # Guardrail: allowlist + threshold de confiança
        route = self._validate_route(intent.route)
        if intent.confidence < 0.5:
            route = "fallback"
        intent = intent.model_copy(update={"route": route})
        return {"intent": intent, "iterations": state.iterations + 1}

    async def _invoke_specialist(self, state: MediatorState) -> dict:
        """Nó de execução: chama o especialista via MCP sob o harness."""
        assert state.intent is not None
        route = state.intent.route
        tools = self._tools_by_route.get(route, [])
        if not tools:
            return {"specialist_result": None, "errors": [f"Sem tools para '{route}'"]}

        query = str(state.messages[-1].content)

        async def _run() -> str:
            # Especialista = LLM com as tools MCP daquela rota
            llm_with_tools = self._router_llm.bind_tools(tools)
            response = await llm_with_tools.ainvoke(
                [HumanMessage(content=query)]
            )
            if response.tool_calls:
                tc = response.tool_calls[0]
                tool = next(t for t in tools if t.name == tc["name"])
                result = await tool.ainvoke(tc["args"])
                return str(result)
            return str(response.content)

        try:
            result = await self._call_with_resilience(route, _run)
            return {"specialist_result": result}
        except RuntimeError as exc:
            return {"specialist_result": None, "errors": [str(exc)]}

    async def _fallback(self, state: MediatorState) -> dict:
        """Degradação graciosa quando não há rota viável."""
        response = await self._router_llm.ainvoke(state.messages)
        return {"specialist_result": str(response.content)}

    def _route_after_classify(self, state: MediatorState) -> str:
        # Guardrail do harness: limite de iterações independe do LLM
        if state.iterations >= self.config.max_iterations:
            logger.warning("max_iterations atingido -> fallback")
            return "fallback"
        assert state.intent is not None
        return "fallback" if state.intent.route == "fallback" else "specialist"

    # ------------------------------------------------------------------
    # 4.5 Construção do grafo (puro — sem lógica de infra)
    # ------------------------------------------------------------------

    def _build_graph(self):
        builder = StateGraph(MediatorState)
        builder.add_node("classify", self._classify_intent)
        builder.add_node("specialist", self._invoke_specialist)
        builder.add_node("fallback", self._fallback)

        builder.add_edge(START, "classify")
        builder.add_conditional_edges(
            "classify",
            self._route_after_classify,
            {"specialist": "specialist", "fallback": "fallback"},
        )
        builder.add_edge("specialist", END)
        builder.add_edge("fallback", END)
        return builder.compile(checkpointer=self._checkpointer)

    # ------------------------------------------------------------------
    # 4.6 Ponto de entrada público
    # ------------------------------------------------------------------

    async def run(self, user_input: str, thread_id: str) -> dict[str, Any]:
        """
        Única API pública. O chamador nunca toca o grafo diretamente —
        toda execução passa pelo harness.
        """
        self._metrics["requests"] += 1
        text = self._validate_input(user_input)
        start = time.monotonic()

        try:
            final_state = await asyncio.wait_for(
                self._graph.ainvoke(
                    {"messages": [HumanMessage(content=text)]},
                    config={"configurable": {"thread_id": thread_id}},
                ),
                timeout=self.config.total_timeout_s,
            )
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "error": "timeout_budget_exceeded",
                "latency_ms": int((time.monotonic() - start) * 1000),
            }

        return {
            "ok": final_state.get("specialist_result") is not None,
            "route": (
                final_state["intent"].route if final_state.get("intent") else None
            ),
            "result": final_state.get("specialist_result"),
            "errors": final_state.get("errors", []),
            "latency_ms": int((time.monotonic() - start) * 1000),
            "thread_id": thread_id,
        }


# =====================================================================
# 5. USO
# =====================================================================

MCP_SERVERS = {
    "billing": {
        "url": "https://billing-agent.internal/mcp",
        "transport": "streamable_http",
    },
    "technical": {
        "url": "https://tech-agent.internal/mcp",
        "transport": "streamable_http",
    },
    "rag": {
        "url": "https://rag-agent.internal/mcp",
        "transport": "streamable_http",
    },
}


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    harness = AgentHarness(config=HarnessConfig(), mcp_servers=MCP_SERVERS)

    async with harness.lifespan():
        result = await harness.run(
            "Minha fatura veio com valor errado este mês",
            thread_id="user-123-session-456",
        )
        print(result)


if __name__ == "__main__":
    asyncio.run(main())
