"""
AgentState do agente filho.

IMPORTANTE (decisão já validada em conversa anterior sobre o mediador):
`session_id` / `thread_id` / `agent_id` são metadados de invocação/harness,
NUNCA entram no AgentState nem viram argumento de tool call — eles trafegam
via `RunnableConfig["configurable"]`, atravessando o grafo (incluindo
dentro do ToolNode) sem nunca serem visíveis para o LLM.
"""

from operator import add
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class IntentResult(TypedDict):
    label: str
    confidence: float
    needs_kb: bool
    kb_targets: list[str]  # subset de KBConfig.name relevantes pro turno


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]

    intent: IntentResult | None

    # Contexto recuperado das KBs habilitadas via tool call (não via
    # injeção cega no prompt) — populado pelo ToolNode quando o LLM
    # de fato decide chamar a tool de KB.
    kb_context: Annotated[list[str], add]

    tool_call_count: int
    guardrail_violations: Annotated[list[str], add]

    # Preenchido pelo nó de guardrail final; usado para decidir se a
    # resposta segue para o usuário ou cai em fallback determinístico.
    final_response_approved: bool
