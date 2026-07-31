"""
Nós do grafo do agente filho. Fluxo:

  validate_input (fora do grafo, harness/guardrails.py)
        |
        v
  classify_intent  --------> aplica allowlist/threshold (guardrails.validate_intent)
        |
        v
  bind_tools_and_reason  ---> ReAct loop: MCP tools sempre disponíveis +
        |                     KB tool(s) só se o intent liberou (kb_tool.select_bound_kb_tools)
        v
  ToolNode (execução)  <-----+
        |  (volta pro reasoning se ainda há tool_calls, até max_tool_iterations)
        v
  final_guardrail  ---------> valida a resposta antes de devolver pro mediador
        |
        v
       END
"""

import logging

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt import ToolNode

from app.config import config
from app.harness.guardrails import GuardrailViolation, check_tool_iteration_limit, validate_intent
from app.prompts.system_prompt import SYSTEM_PROMPT_TEMPLATE
from app.state import AgentState, IntentResult
from app.tools.kb_tool import select_bound_kb_tools

logger = logging.getLogger("child_agent.graph")


async def classify_intent(state: AgentState, config_: RunnableConfig, *, llm) -> dict:
    """Classificação estruturada de intent — separada do reasoning
    principal, propositalmente, pra ser um sinal limpo (não contaminado
    por ferramentas já bindadas) que o resto do grafo usa pra decidir
    o que oferecer ao LLM. Mesmo padrão do roteamento GPT já usado no
    mediador, adaptado pra escopo de um agente filho.

    TODO(time): trocar o parsing abaixo por `llm.with_structured_output`
    usando o schema de IntentResult, e definir a taxonomia real de labels
    e `kb_targets` (deve bater com os `intent_tags` de app/config.py::KBConfig).
    """
    last_user_msg = state["messages"][-1].content

    # Placeholder — substituir por chamada estruturada real ao LLM de
    # classificação (pode ser um modelo mais barato que o de reasoning).
    raw: IntentResult = {
        "label": "unclassified",
        "confidence": 0.0,
        "needs_kb": False,
        "kb_targets": [],
    }

    validated = validate_intent(raw, config.harness)
    return {"intent": validated}


async def bind_tools_and_reason(state: AgentState, config_: RunnableConfig, *, llm, mcp_tools, kb_tools) -> dict:
    """Monta o tool-set do turno (MCP tools + KB tools gated por intent)
    e faz UMA chamada de LLM. O ToolNode subsequente executa as tool
    calls, se houver."""
    check_tool_iteration_limit(state["tool_call_count"], config.harness)

    bound_kb_tools = select_bound_kb_tools(state["intent"], kb_tools)
    tool_set = [*mcp_tools, *bound_kb_tools]

    llm_with_tools = llm.bind_tools(tool_set) if tool_set else llm

    system = SystemMessage(content=SYSTEM_PROMPT_TEMPLATE.format(
        agent_name=config.agent_name,
        available_kbs=", ".join(t.name for t in bound_kb_tools) or "nenhuma neste turno",
    ))
    response: AIMessage = await llm_with_tools.ainvoke([system, *state["messages"]])

    increment = 1 if response.tool_calls else 0
    return {"messages": [response], "tool_call_count": state["tool_call_count"] + increment}


def route_after_reasoning(state: AgentState) -> str:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "execute_tools"
    return "final_guardrail"


async def final_guardrail(state: AgentState) -> dict:
    """TODO(time): validações específicas do domínio do agente antes de
    devolver a resposta ao mediador (ex.: não vazar PII, não afirmar coisas
    fora do allowlist de intent, formato esperado pelo contrato do mediador)."""
    try:
        # placeholder de validação de output
        approved = True
        return {"final_response_approved": approved}
    except GuardrailViolation as exc:
        logger.warning("final_guardrail bloqueou a resposta: %s", exc)
        return {"final_response_approved": False, "guardrail_violations": [str(exc)]}


def build_tool_node(mcp_tools, kb_tools_flat) -> ToolNode:
    return ToolNode([*mcp_tools, *kb_tools_flat])
