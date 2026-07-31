"""
AgentState — separação estrita: só entra aqui o que o grafo/LLM precisa enxergar
ou o que precisa sobreviver entre turnos via checkpoint. Metadado de harness puro
(agent_id, session_id) fica em RunnableConfig/contextvars, nunca aqui.
"""
from typing import Annotated, Optional, TypedDict

from langgraph.graph.message import add_messages


def keep_existing(old: Optional[str], new: Optional[str]) -> Optional[str]:
    """Primeiro valor não-nulo vence. Evita que um turno sem systemMessage
    sobrescreva id_cliente já capturado no primeiro turno."""
    return old if old is not None else new


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    id_cliente: Annotated[Optional[str], keep_existing]
    route: Optional[str]        # especialista escolhido pelo roteador, útil para observabilidade
    iteration_count: int
