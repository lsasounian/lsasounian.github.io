"""
Mesmo padrão de memória dupla usado no mediador: MemorySaver (curto prazo,
keyed por thread_id=session_id) + AgentCore Memory (longo prazo, opcional
via AGENTCORE_MEMORY_ID). Mock em memória quando a env var não está setada,
pra permitir teste local sem depender do recurso AWS.
"""

import os

from langgraph.checkpoint.memory import MemorySaver


def build_checkpointer() -> MemorySaver:
    # TODO(time): se este agente também precisar de memória de longo prazo
    # (cross-session), plugar o client do AgentCore Memory aqui, seguindo
    # o mesmo wrapper já implementado no mediador (fallback pra mock
    # in-memory quando AGENTCORE_MEMORY_ID não está setado).
    _ = os.getenv("AGENTCORE_MEMORY_ID")
    return MemorySaver()
