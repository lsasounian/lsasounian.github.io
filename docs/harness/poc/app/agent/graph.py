"""
Monta e compila o grafo do harness mediador.

Fluxo: refine_query -> router -> skill_loader -> agent <-> tools -> fim
As tools MCP são resolvidas uma vez em build_graph() e reusadas em todas as
invocações -- chamar isso no lifespan da API (app/api/main.py), não por request.
"""

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from app.agent.nodes import (
    get_mcp_tools,
    make_agent_node,
    make_refine_query_node,
    make_router_node,
    make_skill_loader_node,
)
from app.agent.state import MediatorState


async def build_graph():
    tools_by_name = await get_mcp_tools()

    graph = StateGraph(MediatorState)
    graph.add_node("refine_query", make_refine_query_node())
    graph.add_node("router", make_router_node(tools_by_name))
    graph.add_node("skill_loader", make_skill_loader_node(tools_by_name))
    graph.add_node("agent", make_agent_node(tools_by_name))
    graph.add_node("tools", ToolNode([tools_by_name["rag_search"]]))

    graph.set_entry_point("refine_query")
    graph.add_edge("refine_query", "router")
    graph.add_edge("router", "skill_loader")
    graph.add_edge("skill_loader", "agent")
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    # MemorySaver pra esse POC -- trocar por RedisSaver em produção
    return graph.compile(checkpointer=MemorySaver())
