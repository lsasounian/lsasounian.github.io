"""
Lifecycle do agente no AgentCore Runtime — replica as decisões já validadas
no harness do mediador:

1. `_prewarm()`: roda ANTES de `app.run()`, faz descoberta eager das tools
   MCP, compila o grafo, e opcionalmente "esquenta" a conexão TLS/HTTP do
   LLM (PRIME_LLM_ON_BOOT). Reduz o primeiro request real de "cold" pra "warm".

2. Sentinel `__warmup__`: valor que nunca aparece em input real de usuário.
   Verificado como primeira instrução no handler de /invocations — se bater,
   executa warmup idempotente e retorna sem tocar no grafo real. Usado pelo
   frontend/orquestrador pra pre-aquecer microVMs novas antes de rotear
   tráfego real pra elas.

3. Contrato /ping — ARMADILHA DOCUMENTADA: NÃO atualizar
   `time_of_last_update` a cada chamada de /ping. Fazer isso impede o idle
   timeout de disparar, e a sessão fica viva até o MaxLifetime de 8h,
   estourando silenciosamente a quota de sessões. O /ping deste template
   só reporta o health status, sem tocar em nenhum timestamp de atividade
   da sessão.
"""

import logging

logger = logging.getLogger("child_agent.lifecycle")

WARMUP_SENTINEL = "__warmup__"

_warmup_done = False


async def prewarm(mcp_client_factory, graph_builder_factory, prime_llm_on_boot: bool) -> None:
    """Chamar uma vez antes de `app.run()`."""
    global _warmup_done
    if _warmup_done:
        return

    logger.info("prewarm: iniciando descoberta eager de tools MCP")
    mcp_client = await mcp_client_factory()
    await mcp_client.get_tools()  # força a descoberta agora, não no 1o request

    logger.info("prewarm: compilando grafo")
    graph_builder_factory()  # constrói e compila o StateGraph uma vez

    if prime_llm_on_boot:
        logger.info("prewarm: priming de conexão TLS/HTTP do LLM")
        # TODO(time): 1 turno mínimo pro LLM configurado, só pra abrir
        # a conexão antes do primeiro request real (mesmo padrão do mediador).

    _warmup_done = True
    logger.info("prewarm: concluído")


def is_warmup_request(prompt: str) -> bool:
    return prompt == WARMUP_SENTINEL


def ping_response() -> dict:
    """NÃO atualiza timestamp de atividade da sessão — só reporta status."""
    return {"status": "Healthy"}
