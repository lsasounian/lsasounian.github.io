"""
Canal de propagação de metadado operacional (session_id, agent_id, id_cliente)
até o último salto: a chamada MCP real. Nunca passa pelo AgentState/schema de tool —
a LLM não decide, não vê e não precisa saber que esses campos existem.

contextvars é seguro aqui porque cada invocação do AgentCore roda em sua própria
task asyncio; não vaza entre invocações concorrentes desde que nenhum código
dispare create_task() desancorado do escopo da invocação atual.
"""
import contextvars
from typing import Optional

_session_id: contextvars.ContextVar[str] = contextvars.ContextVar("session_id")
_agent_id: contextvars.ContextVar[str] = contextvars.ContextVar("agent_id")
_id_cliente: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("id_cliente", default=None)


def bind(session_id: str, agent_id: str, id_cliente: Optional[str]) -> None:
    _session_id.set(session_id)
    _agent_id.set(agent_id)
    _id_cliente.set(id_cliente)


def mcp_headers(gateway_token: str) -> dict:
    """Headers injetados em cada chamada MCP. Este é o ponto que resolve o
    problema original: dado que a LLM não deveria saber que existe."""
    headers = {
        "Authorization": f"Bearer {gateway_token}",
        "X-Session-Id": _session_id.get(),
        "X-Agent-Id": _agent_id.get(),
    }
    id_cliente = _id_cliente.get()
    if id_cliente:
        headers["X-Id-Cliente"] = id_cliente
    return headers
