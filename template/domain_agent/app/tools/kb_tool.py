"""
KB exposta como TOOL (function calling), não como RAG-as-context injetado
cegamente no prompt. A decisão de OFERECER a tool ao LLM (bind_tools) é
determinística e vem do classificador de intent (graph/nodes.py) — o LLM
só ganha a chance de chamar `retrieve_<kb_name>` quando o intent já
sinalizou relevância. A decisão de FATO CHAMAR (com qual query reescrita)
continua sendo do LLM via tool calling normal.

Por que esse desenho, e não (a) RAG cego sempre-on, nem (b) tool sempre
disponível pro LLM decidir do zero a partir do prompt cru:
  (a) desperdiça budget/latência em turnos onde a KB não é relevante, e
      historicamente piora precisão (contexto irrelevante compete por
      atenção com o contexto certo).
  (b) sem o gate de intent, o LLM decide só olhando o prompt cru, que é
      mais ruidoso — o classificador já fez o trabalho de entender a
      intenção real do turno (inclusive com contexto de conversa), então
      reaproveitar esse sinal pra restringir o tool-set é mais preciso
      e mais barato (menos tokens de schema oferecidos à toa).

Cada agente tem só 1-2 KBs (ver AppConfig.kbs) — não há necessidade de
uma tool "router" genérica: cada KB vira sua própria tool nomeada.
"""

from typing import Protocol

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from app.config import KBConfig
from app.harness.circuit_breaker import CircuitBreakerRegistry
from app.harness.retry import call_with_resilience
from app.state import IntentResult


class SimilaritySearchClient(Protocol):
    """Interface que o SDK próprio de RAG por similaridade (em
    desenvolvimento pelo time) precisa implementar. Este template só
    depende deste contrato — plugar a implementação real em main.py
    quando o SDK estiver pronto."""

    async def search(
        self, kb_name: str, query: str, top_k: int, similarity_threshold: float
    ) -> list[str]:
        """Retorna os `top_k` chunks mais similares acima do threshold,
        já formatados como texto pronto pra entrar no tool result."""
        ...


class _KBQueryInput(BaseModel):
    query: str = Field(description="Consulta reescrita e focada, não o prompt cru do usuário")


def build_kb_tool(
    kb_cfg: KBConfig,
    search_client: SimilaritySearchClient,
    breaker: CircuitBreakerRegistry,
    harness_cfg,
) -> BaseTool:
    resource_key = f"kb:{kb_cfg.name}"

    async def _retrieve(query: str) -> str:
        async def _call():
            return await search_client.search(
                kb_name=kb_cfg.name,
                query=query,
                top_k=kb_cfg.top_k,
                similarity_threshold=kb_cfg.similarity_threshold,
            )

        chunks = await call_with_resilience(
            resource_key=resource_key,
            fn=_call,
            breaker=breaker,
            max_attempts=harness_cfg.retry_max_attempts,
            base_delay_ms=harness_cfg.retry_base_delay_ms,
            max_delay_ms=harness_cfg.retry_max_delay_ms,
        )
        if not chunks:
            return "Nenhum resultado relevante encontrado nesta base."
        return "\n---\n".join(chunks)

    return StructuredTool.from_function(
        coroutine=_retrieve,
        name=f"retrieve_{kb_cfg.name}",
        description=(
            f"Busca por similaridade na base de conhecimento '{kb_cfg.name}'. "
            f"Use apenas quando a pergunta do usuário exigir informação "
            f"específica desta base — reescreva a query focando no que "
            f"falta saber, não repita o prompt inteiro do usuário."
        ),
        args_schema=_KBQueryInput,
    )


def select_bound_kb_tools(
    intent: IntentResult,
    all_kb_tools: dict[str, BaseTool],  # kb_name -> tool
) -> list[BaseTool]:
    """Ponto central do gate por intent: só as KBs cujo `intent_tags`
    batem com `intent["kb_targets"]` (populado pelo classificador) entram
    na lista de tools oferecida ao LLM neste turno. Se `needs_kb` for
    False, retorna lista vazia — o LLM nem vê a tool existir."""
    if not intent["needs_kb"]:
        return []
    return [
        all_kb_tools[kb_name]
        for kb_name in intent["kb_targets"]
        if kb_name in all_kb_tools
    ]
