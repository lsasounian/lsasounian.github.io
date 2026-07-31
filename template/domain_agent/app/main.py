"""
Entrypoint do agente filho no AgentCore Runtime — mesmo esqueleto
(BedrockAgentCoreApp, porta 8080) usado no mediador.

Como este agente é EXPOSTO como tool MCP para o mediador é responsabilidade
de outra camada (vocês já resolveram isso com um MCP server próprio) — este
arquivo cobre só o handler `/invocations` que essa camada acaba invocando.
"""

import logging

from bedrock_agentcore import BedrockAgentCoreApp
from langchain_openai import ChatOpenAI  # TODO(time): trocar pelo provider real

from app.config import config
from app.graph.builder import build_graph
from app.harness.circuit_breaker import init_breaker_registry
from app.harness.guardrails import GuardrailViolation, validate_input
from app.harness.lifecycle import WARMUP_SENTINEL, is_warmup_request, ping_response, prewarm
from app.harness.observability import bind_correlation, init_observability
from app.harness.timeout_budget import BudgetExhaustedError, InvocationBudget
from app.memory.checkpointer import build_checkpointer
from app.tools.kb_tool import build_kb_tool
from app.tools.mcp_client import ResilientMCPToolProvider

logger = logging.getLogger("child_agent.main")
init_observability(config.agent_name)
_breaker_registry = init_breaker_registry(
    config.harness.cb_failure_threshold,
    config.harness.cb_open_duration_s,
    config.harness.cb_half_open_max_calls,
)

app = BedrockAgentCoreApp()

_llm = ChatOpenAI(model=config.llm_model)  # TODO(time): provider/model real
_mcp_provider = ResilientMCPToolProvider(config.mcp_servers, _breaker_registry, config.harness)
_checkpointer = build_checkpointer()

# TODO(time): plugar a implementação real de SimilaritySearchClient quando
# o SDK de RAG por similaridade estiver pronto. Até lá, `_kb_tools` fica
# vazio e o agente simplesmente nunca oferece tool de KB.
_kb_search_client = None
_kb_tools: dict = {}
if _kb_search_client is not None:
    _kb_tools = {
        kb.name: build_kb_tool(kb, _kb_search_client, _breaker_registry, config.harness)
        for kb in config.kbs
    }

_graph = None  # compilado no prewarm


async def _ensure_prewarmed():
    global _graph

    async def _mcp_client_factory():
        return _mcp_provider

    def _graph_builder_factory():
        global _graph
        # a essa altura _mcp_provider.get_tools() já rodou dentro de
        # prewarm() e populou o cache — reusa sem nova chamada de rede.
        raw_tools = _mcp_provider._tools_cache or []
        # TODO(time): mapear tool -> server_name real (MultiServerMCPClient
        # não expõe isso diretamente hoje) para o wrap_tool poder usar a
        # resource_key correta por server no circuit breaker.
        mcp_tools = [_mcp_provider.wrap_tool(t, server_name="mcp") for t in raw_tools]
        _graph = build_graph(_llm, mcp_tools, _kb_tools, _checkpointer)
        return _graph

    await prewarm(_mcp_client_factory, _graph_builder_factory, config.prime_llm_on_boot)


@app.entrypoint
async def invoke(payload: dict, context) -> dict:
    session_id = context.session_id  # vira thread_id no checkpointer
    prompt = payload.get("prompt", "")

    if is_warmup_request(prompt):
        await _ensure_prewarmed()
        return {"status": "warmed"}

    bind_correlation(session_id)
    budget = InvocationBudget.start(config.harness.total_invocation_budget_ms)

    try:
        validate_input(prompt, config.harness)
    except GuardrailViolation as exc:
        logger.warning("input rejeitado pelo guardrail: %s", exc)
        return {"error": str(exc), "type": "guardrail_violation"}

    await _ensure_prewarmed()

    try:
        with budget.phase("total", config.harness.total_invocation_budget_ms):
            result = await _graph.ainvoke(
                {
                    "messages": [{"role": "user", "content": prompt}],
                    "intent": None,
                    "kb_context": [],
                    "tool_call_count": 0,
                    "guardrail_violations": [],
                    "final_response_approved": False,
                },
                config={"configurable": {"thread_id": session_id}},
            )
    except BudgetExhaustedError as exc:
        logger.error("budget estourado: %s", exc)
        return {"error": str(exc), "type": "budget_exhausted"}

    if not result["final_response_approved"]:
        return {"error": "resposta bloqueada pelo guardrail final", "type": "guardrail_violation"}

    return {"response": result["messages"][-1].content}


@app.ping
def ping() -> dict:
    return ping_response()


if __name__ == "__main__":
    app.run(port=config.port)
