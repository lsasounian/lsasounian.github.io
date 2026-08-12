"""
Tradução do harness (Microsoft Copilot Studio) para LangGraph.

Mapeamento conceitual:
    Harness (Copilot Studio)                 -> router_node + agent_node
    Topic / Skill                            -> dataclass Skill no dict SKILLS
    Frases de intenção do topic              -> intent_examples (usadas no prompt do router)
    Knowledge source vinculada ao topic      -> tool de KB, bindada só na skill ativa
    "Questione o usuário antes de seguir"    -> interrupt() + checkpointer

Com só 3 domínios, roteamento por LLM direto é suficiente. Embeddings/vetor só
compensam quando o catálogo de skills cresce bem além disso.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, TypedDict

from langchain_aws import ChatBedrock
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver  # troque por RedisSaver em produção
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.types import interrupt

# ajuste pro model ID que você tem provisionado no Bedrock
MODEL_ID = "anthropic.claude-sonnet-4-5-20250929-v1:0"


# ---------------------------------------------------------------------------
# 1. Skill = a mesma unidade que hoje é um "Topic" no Copilot Studio
# ---------------------------------------------------------------------------

@dataclass
class Skill:
    skill_id: str
    intent_examples: list[str]   # frases-gatilho, equivalente ao trigger do topic
    instructions: str            # regras específicas dessa skill
    kb_tool_name: str            # nome da tool de KB vinculada a essa skill


SKILLS: dict[str, Skill] = {
    "cartoes": Skill(
        skill_id="cartoes",
        intent_examples=["cartão", "cartões", "fatura", "limite do cartão", "anuidade"],
        instructions=(
            "1. Use o KB XPTO como tool antes de responder a pergunta do usuário\n"
            "2. Pesquise mesmo com pedidos incompletos, não assuma status de não informado\n"
            "3. Não misture regras, apresente cenários separados\n"
            "4. Se faltarem elementos para a resposta completa, questione o usuário antes de seguir"
        ),
        kb_tool_name="kb_xpto_search",
    ),
    "pf": Skill(
        skill_id="pf",
        intent_examples=["conta corrente", "saldo", "extrato", "transferência", "pix"],
        instructions="# preencha com as regras reais dessa skill",
        kb_tool_name="kb_pf_search",
    ),
    "seguros": Skill(
        skill_id="seguros",
        intent_examples=["seguro", "apólice", "sinistro", "cobertura"],
        instructions="# preencha com as regras reais dessa skill",
        kb_tool_name="kb_seguros_search",
    ),
}

# A parte do prompt do harness que NÃO muda entre skills
BASE_HARNESS_PROMPT = (
    "Você é um agente de atendimento que interpreta a intenção do usuário, "
    "ativa a habilidade de domínio pertinente, consulta fontes de conhecimento "
    "como tool e entrega uma resposta final clara e assertiva."
)


# ---------------------------------------------------------------------------
# 2. Tools de KB -- uma por skill, equivalente ao "knowledge source" do topic
# ---------------------------------------------------------------------------

@tool
def kb_xpto_search(query: str) -> str:
    """Busca na base de conhecimento XPTO sobre cartões."""
    # troque pelo client real -- Bedrock Knowledge Bases, OpenSearch, ou MCP
    # response = bedrock_agent_runtime.retrieve(
    #     knowledgeBaseId="KB_XPTO_ID", retrievalQuery={"text": query}
    # )
    # return "\n".join(r["content"]["text"] for r in response["retrievalResults"])
    raise NotImplementedError


@tool
def kb_pf_search(query: str) -> str:
    """Busca na base de conhecimento de conta corrente / PF."""
    raise NotImplementedError


@tool
def kb_seguros_search(query: str) -> str:
    """Busca na base de conhecimento de seguros."""
    raise NotImplementedError


TOOL_REGISTRY = {
    "kb_xpto_search": kb_xpto_search,
    "kb_pf_search": kb_pf_search,
    "kb_seguros_search": kb_seguros_search,
}


# ---------------------------------------------------------------------------
# 3. State
# ---------------------------------------------------------------------------

class HarnessState(TypedDict):
    messages: Annotated[list, add_messages]
    active_skill: str | None


# ---------------------------------------------------------------------------
# 4. Router node -- decide qual skill ativar pela intenção.
#    Com só 3 domínios, LLM direto é suficiente -- sem embeddings, sem índice.
# ---------------------------------------------------------------------------

ROUTER_PROMPT_TEMPLATE = (
    "Você decide qual habilidade de domínio deve ser ativada pra responder "
    "o usuário. Habilidades disponíveis:\n{skill_list}\n\n"
    "Responda apenas com o skill_id da habilidade mais adequada, nada mais."
)

router_llm = ChatBedrock(model_id=MODEL_ID, temperature=0)


def router_node(state: HarnessState) -> dict:
    skill_list = "\n".join(
        f"- {s.skill_id}: {', '.join(s.intent_examples)}" for s in SKILLS.values()
    )
    prompt = ROUTER_PROMPT_TEMPLATE.format(skill_list=skill_list)
    decision = router_llm.invoke([SystemMessage(content=prompt), state["messages"][-1]])
    skill_id = decision.content.strip().lower()
    return {"active_skill": skill_id if skill_id in SKILLS else "cartoes"}


# ---------------------------------------------------------------------------
# 5. Agent node -- monta o prompt (base + skill) e faz bind só da KB da skill
# ---------------------------------------------------------------------------

agent_llm = ChatBedrock(model_id=MODEL_ID, temperature=0)


def agent_node(state: HarnessState) -> dict:
    skill = SKILLS[state["active_skill"]]
    system_prompt = (
        f"{BASE_HARNESS_PROMPT}\n\n"
        f"Regras específicas do domínio '{skill.skill_id}':\n{skill.instructions}"
    )
    tools = [TOOL_REGISTRY[skill.kb_tool_name]]
    llm_with_tools = agent_llm.bind_tools(tools)

    response = llm_with_tools.invoke([SystemMessage(content=system_prompt), *state["messages"]])
    return {"messages": [response]}


# ---------------------------------------------------------------------------
# 6. Clarificação (regra 4 da skill) -- pausa o grafo até o usuário responder.
#    Exige checkpointer: é ele que sustenta o estado entre a pausa e a retomada.
# ---------------------------------------------------------------------------

def clarify_node(state: HarnessState) -> dict:
    question = interrupt({"reason": "faltam elementos para responder"})
    return {"messages": [question]}


# ---------------------------------------------------------------------------
# 7. Grafo
# ---------------------------------------------------------------------------

tool_node = ToolNode(list(TOOL_REGISTRY.values()))

graph = StateGraph(HarnessState)
graph.add_node("router", router_node)
graph.add_node("agent", agent_node)
graph.add_node("tools", tool_node)

graph.set_entry_point("router")
graph.add_edge("router", "agent")
graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
graph.add_edge("tools", "agent")

# Em produção, troque MemorySaver pelo seu RedisSaver -- o mesmo checkpointer
# que sustenta a regra 4 (pausar/retomar) sustenta o histórico entre turnos.
app = graph.compile(checkpointer=MemorySaver())


if __name__ == "__main__":
    config = {"configurable": {"thread_id": "exemplo-1"}}
    result = app.invoke(
        {"messages": [HumanMessage(content="quero contestar uma cobrança no cartão")]},
        config=config,
    )
    print(result["messages"][-1].content)
