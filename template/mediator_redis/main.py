"""
Entrypoint AgentCore. Único lugar que conhece o contrato do SDK fixo
(agentId, sessionId, prompt) — tudo abaixo daqui trabalha com as abstrações
do harness (AgentState, contextvars, checkpointer HTTP).
"""
import logging

from bedrock_agentcore import BedrockAgentCoreApp
from langchain_core.messages import HumanMessage

from app.config import settings
from app.graph import build_graph
from app.harness.circuit_breaker import CircuitBreaker
from app.harness.context import bind as bind_context
from app.harness.redis_checkpointer import HTTPRedisSaver
from app.mcp_client import build_mcp_client
from app.prompt_parser import parse_prompt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = BedrockAgentCoreApp()  # expõe /invocations e /ping automaticamente na porta 8080

_redis_breaker = CircuitBreaker(name="redis-facade", failure_threshold=5, recovery_timeout_s=15.0)
_checkpointer = HTTPRedisSaver(
    base_url=settings.redis_facade_base_url,
    breaker=_redis_breaker,
    mtls_cert=settings.redis_facade_mtls_cert,
    mtls_key=settings.redis_facade_mtls_key,
    timeout_s=settings.redis_facade_timeout_s,
)

_graph = None  # compilado em _prewarm(), nunca no primeiro invocation real


async def _prewarm() -> None:
    """Disparado pelo sentinel __warmup__: compila o grafo e descobre tools MCP
    antes do primeiro tráfego real, eliminando cold start no caminho quente."""
    global _graph
    mcp_client = build_mcp_client()
    tools = await mcp_client.get_tools()
    _graph = build_graph(_checkpointer, tools=tools)

    if settings.prime_llm_on_boot:
        await _prime_llm_connection()

    logger.info("prewarm concluído — %d tools MCP descobertas", len(tools))


async def _prime_llm_connection() -> None:
    # Placeholder: turno mínimo contra o provider da LLM para aquecer conexão TLS/HTTP.
    pass


@app.entrypoint
async def handler(payload: dict, context) -> dict:
    session_id = payload["sessionId"]
    agent_id = payload["agentId"]
    raw_prompt = payload["prompt"]

    # Fast-path de warmup: nunca toca no grafo real nem na fachada Redis.
    if raw_prompt == "__warmup__":
        if _graph is None:
            await _prewarm()
        return {"status": "warmed"}

    if _graph is None:
        # Defesa: se por algum motivo o warmup explícito não foi disparado antes
        # do primeiro tráfego real, prewarm acontece aqui — com o custo de cold start.
        await _prewarm()

    user_content, system_payload = parse_prompt(raw_prompt)
    id_cliente = system_payload.get("id_cliente") if system_payload else None

    # Metadado operacional -> contextvars, nunca para dentro do AgentState/mensagens.
    bind_context(session_id=session_id, agent_id=agent_id, id_cliente=id_cliente)

    graph_input = {
        "messages": [HumanMessage(content=user_content)],
        "id_cliente": id_cliente,
        "iteration_count": 0,
    }
    config = {
        "configurable": {"thread_id": session_id},
        "recursion_limit": settings.max_iterations,
    }

    result = await _graph.ainvoke(graph_input, config=config)

    if payload.get("end_session"):
        # Espelha StopRuntimeSession: cleanup explícito em vez de esperar TTL.
        await _checkpointer.cleanup(session_id)

    return {"messages": [m.content for m in result["messages"]]}


if __name__ == "__main__":
    app.run()
