"""
Agente Mediador com Harness — Versão AgentCore Runtime
=======================================================
Adaptação do mediator_harness.py para AWS Bedrock AgentCore Runtime.

Diferenças em relação à versão standalone:
  1. `BedrockAgentCoreApp` como servidor (porta 8080, contrato do Runtime)
  2. `__warmup__` sentinel como fast-path no entrypoint (cold start)
  3. Handler de /ping correto: `time_of_last_update` só muda quando o
     STATUS muda — nunca a cada ping (senão o idle timeout nunca dispara
     e as sessões vivem até MaxLifetime, esgotando a cota)
  4. session_id do AgentCore como thread_id do LangGraph (correlação 1:1)
  5. Timeout budget do harness alinhado aos limites do Runtime
     (idle timeout default 15 min, max lifetime 8h)
  6. JWT M2M via AgentCore Identity nos headers do MCP client
     (especialistas atrás do AgentCore Gateway)
  7. StopRuntimeSession no shutdown para cleanup explícito de sessão

Camadas do harness que o AgentCore JÁ cobre (não reimplementar):
  - Isolamento por microVM (blast radius entre sessões)
  - Métricas ActiveSessionCount no CloudWatch (AWS/Bedrock-AgentCore)
  - Ciclo de vida da sessão (idle timeout, max lifetime)

Camadas que continuam sendo responsabilidade DESTE código:
  - Circuit breaker por especialista, retry + backoff + jitter
  - Timeout budget em cascata DENTRO da requisição
  - Guardrails (allowlist de rotas, threshold de confiança)
  - Limite de iterações do grafo
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any, Literal, Optional

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.models import PingStatus
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel, Field

logger = logging.getLogger("mediator.harness.agentcore")

WARMUP_SENTINEL = "__warmup__"

# =====================================================================
# 1. CONFIGURAÇÃO — alinhada aos limites do AgentCore Runtime
# =====================================================================

@dataclass(frozen=True)
class HarnessConfig:
    """
    Regra de ouro: total_timeout_s DEVE ser menor que o
    idle_runtime_session_timeout do Runtime (default 900s).
    Se o harness estourar o limite da plataforma, quem mata a
    sessão é o Runtime — e você perde o controle do erro retornado.
    """
    max_iterations: int = 8
    total_timeout_s: float = 120.0       # << 900s do idle timeout do Runtime
    specialist_timeout_s: float = 30.0
    max_retries: int = 2
    retry_base_delay_s: float = 0.5
    circuit_failure_threshold: int = 3
    circuit_reset_timeout_s: float = 30.0
    allowed_routes: tuple[str, ...] = ("billing", "technical", "rag", "fallback")


# =====================================================================
# 2. CIRCUIT BREAKER (idêntico à versão standalone)
# =====================================================================

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
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
# 3. ESTADO DO GRAFO (idêntico à versão standalone)
# =====================================================================

class IntentClassification(BaseModel):
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
# 4. IDENTITY: token M2M para o AgentCore Gateway
# =====================================================================

class GatewayTokenProvider:
    """
    Obtém e cacheia o bearer token (client credentials) usado nos
    headers do MCP client contra o AgentCore Gateway.
    Em produção, troque o stub por AgentCore Identity ou seu
    TokenManager (RS256) existente.
    """

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        async with self._lock:
            # margem de 60s antes de expirar
            if self._token and time.monotonic() < self._expires_at - 60:
                return self._token
            self._token, ttl = await self._fetch_token()
            self._expires_at = time.monotonic() + ttl
            return self._token

    async def _fetch_token(self) -> tuple[str, float]:
        # Stub: substituir por chamada real ao Identity / IdP (EntraID etc.)
        # Ex.: workload identity do AgentCore ou client_credentials OAuth2.
        token = os.environ.get("GATEWAY_BEARER_TOKEN", "dev-token")
        return token, 3600.0


# =====================================================================
# 5. O HARNESS (núcleo igual; bordas adaptadas ao Runtime)
# =====================================================================

class AgentHarness:
    def __init__(
        self,
        config: HarnessConfig,
        gateway_url: str,
        routing_model: str = "gpt-4o",
    ) -> None:
        self.config = config
        self._gateway_url = gateway_url
        self._token_provider = GatewayTokenProvider()
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
        self._warmed_up = False
        # Status reportado ao /ping do Runtime. Só muda em transições reais.
        self._busy_sessions = 0

    # ------------------------------------------------------------------
    # 5.1 Warmup (disparado pelo sentinel, não por requisição real)
    # ------------------------------------------------------------------

    async def warmup(self) -> None:
        """Idempotente: chamadas repetidas do sentinel são no-op."""
        if self._warmed_up:
            return
        token = await self._token_provider.get_token()
        # Um único endpoint (Gateway) na frente de todos os especialistas.
        # O Gateway mantém sessões MCP com estado (Mcp-Session-Id) e SSE,
        # eliminando reconexão manual por especialista.
        self._mcp_client = MultiServerMCPClient(
            {
                "gateway": {
                    "url": self._gateway_url,
                    "transport": "streamable_http",
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            }
        )
        all_tools = await self._mcp_client.get_tools()
        for tool in all_tools:
            route = tool.name.split("__", 1)[0]
            self._tools_by_route.setdefault(route, []).append(tool)
        self._warmed_up = True
        logger.info("Warmup ok. Rotas: %s", list(self._tools_by_route))

    # ------------------------------------------------------------------
    # 5.2 Ping status — contrato com o Runtime
    # ------------------------------------------------------------------

    def ping_status(self) -> PingStatus:
        """
        HealthyBusy enquanto há requisição em andamento; Healthy caso
        contrário. IMPORTANTE: o SDK deriva time_of_last_update das
        TRANSIÇÕES de status. Nunca force esse campo para "agora" a
        cada ping — isso impede o idle timeout de disparar e as
        sessões vivem até MaxLifetime, estourando a cota de sessões.
        """
        return (
            PingStatus.HEALTHY_BUSY if self._busy_sessions > 0 else PingStatus.HEALTHY
        )

    # ------------------------------------------------------------------
    # 5.3 Guardrails (idênticos)
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_input(text: str) -> str:
        if not text or not text.strip():
            raise ValueError("Entrada vazia")
        if len(text) > 8_000:
            raise ValueError("Entrada excede limite")
        return text.strip()

    def _validate_route(self, route: str) -> str:
        if route not in self.config.allowed_routes:
            logger.warning("Rota inválida do LLM: %r -> fallback", route)
            return "fallback"
        return route

    # ------------------------------------------------------------------
    # 5.4 Execução resiliente (idêntica)
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
                    delay += random.uniform(0, delay * 0.3)
                    self._metrics["retries"] += 1
                    logger.info(
                        "Retry %d rota=%s em %.2fs (%s)",
                        attempt + 1, route, delay, exc,
                    )
                    await asyncio.sleep(delay)
        raise RuntimeError(f"Especialista '{route}' esgotou retries") from last_exc

    # ------------------------------------------------------------------
    # 5.5 Nós do grafo (idênticos)
    # ------------------------------------------------------------------

    async def _classify_intent(self, state: MediatorState) -> dict:
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
        route = self._validate_route(intent.route)
        if intent.confidence < 0.5:
            route = "fallback"
        intent = intent.model_copy(update={"route": route})
        return {"intent": intent, "iterations": state.iterations + 1}

    async def _invoke_specialist(self, state: MediatorState) -> dict:
        assert state.intent is not None
        route = state.intent.route
        tools = self._tools_by_route.get(route, [])
        if not tools:
            return {"specialist_result": None, "errors": [f"Sem tools para '{route}'"]}

        query = str(state.messages[-1].content)

        async def _run() -> str:
            llm_with_tools = self._router_llm.bind_tools(tools)
            response = await llm_with_tools.ainvoke([HumanMessage(content=query)])
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
        response = await self._router_llm.ainvoke(state.messages)
        return {"specialist_result": str(response.content)}

    def _route_after_classify(self, state: MediatorState) -> str:
        if state.iterations >= self.config.max_iterations:
            logger.warning("max_iterations atingido -> fallback")
            return "fallback"
        assert state.intent is not None
        return "fallback" if state.intent.route == "fallback" else "specialist"

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
    # 5.6 Ponto de entrada — agora recebe o session_id do Runtime
    # ------------------------------------------------------------------

    async def run(self, user_input: str, session_id: str) -> dict[str, Any]:
        """
        session_id do AgentCore == thread_id do LangGraph.
        Correlação 1:1: o mesmo ID aparece nos logs do Runtime
        (/aws/bedrock-agentcore/runtimes/...), no checkpointer e
        nos seus spans OTel.
        """
        self._metrics["requests"] += 1
        text = self._validate_input(user_input)
        start = time.monotonic()
        self._busy_sessions += 1  # ping passa a reportar HealthyBusy

        try:
            final_state = await asyncio.wait_for(
                self._graph.ainvoke(
                    {"messages": [HumanMessage(content=text)]},
                    config={"configurable": {"thread_id": session_id}},
                ),
                timeout=self.config.total_timeout_s,
            )
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "error": "timeout_budget_exceeded",
                "latency_ms": int((time.monotonic() - start) * 1000),
                "session_id": session_id,
            }
        finally:
            self._busy_sessions -= 1  # transição de volta para Healthy

        return {
            "ok": final_state.get("specialist_result") is not None,
            "route": (
                final_state["intent"].route if final_state.get("intent") else None
            ),
            "result": final_state.get("specialist_result"),
            "errors": final_state.get("errors", []),
            "latency_ms": int((time.monotonic() - start) * 1000),
            "session_id": session_id,
        }


# =====================================================================
# 6. CLEANUP EXPLÍCITO DE SESSÃO (opcional)
# =====================================================================

def stop_runtime_session(session_id: str) -> None:
    """
    Termina a sessão no Runtime imediatamente em vez de esperar o
    idle timeout. Use em: fim explícito de conversa, erro fatal,
    ou gestão de cota (evitar acúmulo de sessões ociosas).
    Requer IAM: bedrock-agentcore:StopRuntimeSession.
    """
    client = boto3.client("bedrock-agentcore")
    try:
        client.stop_runtime_session(
            agentRuntimeArn=os.environ["AGENT_RUNTIME_ARN"],
            runtimeSessionId=session_id,
        )
        logger.info("Sessão %s terminada explicitamente", session_id)
    except client.exceptions.ResourceNotFoundException:
        pass  # já terminada — idempotente


# =====================================================================
# 7. APP AGENTCORE — porta 8080, contrato do Runtime
# =====================================================================

config = HarnessConfig()
harness = AgentHarness(
    config=config,
    gateway_url=os.environ.get(
        "GATEWAY_MCP_URL", "https://my-gateway.gateway.bedrock-agentcore.aws/mcp"
    ),
)

app = BedrockAgentCoreApp()


@app.ping
def ping() -> PingStatus:
    """O Runtime consulta este handler para decidir idle timeout."""
    return harness.ping_status()


@app.entrypoint
async def invoke(payload: dict, context) -> dict:
    """
    Contrato do AgentCore Runtime:
      payload: corpo do InvokeAgentRuntime
      context: metadados da invocação (session_id vem do header
               X-Amzn-Bedrock-AgentCore-Runtime-Session-Id)
    """
    user_input = str(payload.get("prompt", payload.get("input", "")))

    # ---- FAST-PATH: __warmup__ sentinel ----
    # Primeira coisa no handler, custo de uma comparação de string.
    # Dispara o custo de inicialização (MCP, tools, token) ANTES da
    # primeira requisição real. Idempotente.
    if user_input == WARMUP_SENTINEL:
        await harness.warmup()
        return {"status": "warmed"}

    # Garantia defensiva: se nenhum warmup chegou antes do tráfego
    # real (deploy sem invocação de aquecimento), aquece inline.
    if not harness._warmed_up:
        await harness.warmup()

    session_id = getattr(context, "session_id", None) or "no-session"
    result = await harness.run(user_input, session_id=session_id)

    # Fim explícito de conversa -> libera a cota de sessão do Runtime
    if payload.get("end_session") is True:
        stop_runtime_session(session_id)
        result["session_terminated"] = True

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run()  # porta 8080, conforme contrato do AgentCore Runtime
