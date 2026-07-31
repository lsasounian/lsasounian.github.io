"""
Compilação do StateGraph. Chamado uma vez no prewarm (harness/lifecycle.py)
e reusado — nunca recompilar por request.
"""

from functools import partial

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from app.graph.nodes import (
    bind_tools_and_reason,
    build_tool_node,
    classify_intent,
    final_guardrail,
    route_after_reasoning,
)
from app.state import AgentState


def build_graph(llm, mcp_tools, kb_tools: dict, checkpointer: BaseCheckpointSaver):
    kb_tools_flat = list(kb_tools.values())

    graph = StateGraph(AgentState)

    graph.add_node("classify_intent", classify_intent)
    graph.add_node(
        "reason",
        partial(bind_tools_and_reason, llm=llm, mcp_tools=mcp_tools, kb_tools=kb_tools),
    )
    graph.add_node("execute_tools", build_tool_node(mcp_tools, kb_tools_flat))
    graph.add_node("final_guardrail", final_guardrail)

    graph.set_entry_point("classify_intent")
    graph.add_edge("classify_intent", "reason")
    graph.add_conditional_edges(
        "reason",
        route_after_reasoning,
        {"execute_tools": "execute_tools", "final_guardrail": "final_guardrail"},
    )
    # volta pro reasoning depois de executar tools — o guardrail de
    # max_tool_iterations (harness/guardrails.py) evita loop infinito
    graph.add_edge("execute_tools", "reason")
    graph.add_edge("final_guardrail", END)

    return graph.compile(checkpointer=checkpointer)
