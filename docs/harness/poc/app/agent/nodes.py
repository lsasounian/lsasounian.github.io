"""
Nodes do harness. As tools MCP (registry + rag) chegam já resolvidas via
`tools_by_name`, montado uma vez no startup por app/agent/graph.py -- não há
round-trip MCP repetido a cada mensagem só pra descobrir as tools de novo.
"""

from langchain_aws import ChatBedrock
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient

from app.agent.state import MediatorState
from app.config import settings

llm = ChatBedrock(model_id=settings.bedrock_model_id, temperature=0)

_mcp_client = MultiServerMCPClient(
    {"registry": {"url": settings.mcp_server_url, "transport": "streamable_http"}}
)


async def get_mcp_tools() -> dict:
    """Chamado uma vez no startup (ver graph.py). Descobre via MCP as tools de
    catálogo (search_skills, get_skill, list_skills) e a de RAG (rag_search) --
    nenhuma delas está hardcoded no código do agente."""
    tools = await _mcp_client.get_tools()
    return {t.name: t for t in tools}


REFINE_PROMPT = (
    "Reescreva a pergunta do usuário de forma clara e completa, explicitando "
    "o que está sendo pedido, sem adicionar nenhuma informação que o usuário "
    "não deu. Responda só com a pergunta reescrita, nada mais."
)


def make_refine_query_node():
    async def refine_query_node(state: MediatorState) -> dict:
        raw = state["messages"][-1].content
        result = await llm.ainvoke(
            [SystemMessage(content=REFINE_PROMPT), HumanMessage(content=raw)]
        )
        return {"raw_query": raw, "refined_query": result.content.strip()}

    return refine_query_node


ROUTER_PROMPT_TEMPLATE = (
    "Você decide qual habilidade de domínio deve ser ativada pra responder "
    "o usuário. Habilidades disponíveis:\n{skill_list}\n\n"
    "Responda apenas com o skill_id da habilidade mais adequada, nada mais."
)


def make_router_node(tools_by_name: dict):
    async def router_node(state: MediatorState) -> dict:
        # roteamento usa a pergunta REFINADA, não a bruta
        candidates = await tools_by_name["search_skills"].ainvoke(
            {"query": state["refined_query"], "top_k": 3}
        )
        if candidates:
            return {"active_skill": candidates[0]["skill_id"]}

        # fallback: nenhuma skill bateu por palavra-chave, LLM decide entre todas
        all_skills = await tools_by_name["list_skills"].ainvoke({})
        skill_list = "\n".join(f"- {s['skill_id']}: {s['description']}" for s in all_skills)
        prompt = ROUTER_PROMPT_TEMPLATE.format(skill_list=skill_list)
        decision = await llm.ainvoke(
            [SystemMessage(content=prompt), HumanMessage(content=state["refined_query"])]
        )
        return {"active_skill": decision.content.strip().lower()}

    return router_node


def make_skill_loader_node(tools_by_name: dict):
    async def skill_loader_node(state: MediatorState) -> dict:
        skill = await tools_by_name["get_skill"].ainvoke({"skill_id": state["active_skill"]})
        return {"skill_instructions": skill["instructions"], "skill_kb_ids": skill["kb_ids"]}

    return skill_loader_node


BASE_HARNESS_PROMPT = (
    "Você é um agente de atendimento que interpreta a intenção do usuário, "
    "ativa a habilidade de domínio pertinente, consulta fontes de conhecimento "
    "como tool e entrega uma resposta final clara e assertiva."
)


def make_agent_node(tools_by_name: dict):
    # bind_tools acontece uma vez aqui (startup), não a cada mensagem --
    # nesse POC o rag_search é o único tool genérico compartilhado por todas
    # as skills, então não há bind dinâmico por skill como no design anterior
    llm_with_rag = llm.bind_tools([tools_by_name["rag_search"]])

    async def agent_node(state: MediatorState) -> dict:
        system_prompt = (
            f"{BASE_HARNESS_PROMPT}\n\n"
            f"Regras específicas do domínio '{state['active_skill']}':\n"
            f"{state['skill_instructions']}\n\n"
            f"Use a tool rag_search com kb_id em {state['skill_kb_ids']} pra consultar "
            f"a base de conhecimento dessa skill. Query sugerida (pergunta refinada): "
            f"\"{state['refined_query']}\""
        )
        response = await llm_with_rag.ainvoke(
            [SystemMessage(content=system_prompt), *state["messages"]]
        )
        return {"messages": [response], "bound_tool_names": ["rag_search"]}

    return agent_node
