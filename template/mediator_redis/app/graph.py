"""
Grafo supervisor: resolve "o que fazer". Política operacional (timeout, retry,
circuit breaker, warmup) fica inteiramente no harness (main.py + app/harness/*),
nunca dentro de um node.

prepare_context e route são esqueletos — plugue aqui a pipeline de RAG
(embed_query -> retrieve -> rerank -> get_context) e o roteador GPT com saída
estruturada que vocês já têm implementados no projeto original; a interface
abaixo (contrato de entrada/saída de cada node) é o que importa para o harness.
"""
from typing import Optional

from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from app.state import AgentState


async def prepare_context(state: AgentState) -> dict:
    # Placeholder: pipeline de RAG real entra aqui.
    return {}


async def route(state: AgentState) -> dict:
    # Placeholder: roteador GPT com saída estruturada entra aqui.
    # Precisa devolver {"route": <nome_do_especialista_ou_None>, "messages": [...]}.
    raise NotImplementedError("plugue o roteador GPT-5.4 existente aqui")


def _has_route(state: AgentState) -> str:
    return "specialists" if state.get("route") else END


def build_graph(checkpointer, tools: Optional[list] = None):
    """tools é injetado a partir do _prewarm() em main.py, já descoberto via
    MCP antes do primeiro tráfego real — não descobre tools dentro do grafo."""
    graph = StateGraph(AgentState)
    graph.add_node("prepare_context", prepare_context)
    graph.add_node("route", route)
    graph.add_node("specialists", ToolNode(tools or []))

    graph.set_entry_point("prepare_context")
    graph.add_edge("prepare_context", "route")
    graph.add_conditional_edges("route", _has_route)
    graph.add_edge("specialists", END)

    return graph.compile(checkpointer=checkpointer)
