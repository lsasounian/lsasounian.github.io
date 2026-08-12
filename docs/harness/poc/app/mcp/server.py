"""
MCP server único que expõe:
  - Catálogo de skills: search_skills, get_skill, list_skills
  - RAG genérico por kb_id: rag_search

Roda como processo separado (ver docker-compose.yml), exposto via HTTP
(streamable-http) pra ser acessado pelo agent-api dentro da rede do compose.
"""

from mcp.server.fastmcp import FastMCP

from app.mcp.catalog import SKILLS_CATALOG
from app.mcp.kb_store import search_kb

# host/port explícitos pra funcionar dentro do container -- confira os kwargs
# aceitos na versão do pacote `mcp` instalada, essa API muda entre versões
mcp = FastMCP("mediator-registry", host="0.0.0.0", port=8001)


@mcp.tool()
def search_skills(query: str, top_k: int = 3) -> list[dict]:
    """Busca skills por palavra-chave na descrição e nas intent_examples.
    Sempre uma chamada de função simples -- sem embedding, sem cache local."""
    query_words = query.lower().split()

    scored = []
    for skill in SKILLS_CATALOG.values():
        haystack = " ".join([skill["description"], *skill["intent_examples"]]).lower()
        score = sum(1 for w in query_words if w in haystack)
        if score > 0:
            scored.append((score, skill))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {"skill_id": s["skill_id"], "description": s["description"], "score": score}
        for score, s in scored[:top_k]
    ]


@mcp.tool()
def get_skill(skill_id: str) -> dict:
    """Retorna a skill completa: instruções, kb_ids e allowed_tools."""
    skill = SKILLS_CATALOG.get(skill_id)
    if not skill:
        raise ValueError(f"Skill '{skill_id}' não encontrada")
    return skill


@mcp.tool()
def list_skills() -> list[dict]:
    """Lista todas as skills do catálogo (fallback quando search_skills não acha nada)."""
    return [
        {"skill_id": s["skill_id"], "description": s["description"]}
        for s in SKILLS_CATALOG.values()
    ]


@mcp.tool()
def rag_search(kb_id: str, query: str, top_k: int = 3) -> list[dict]:
    """Busca genérica em qualquer KB do catálogo, dado o kb_id.
    Único método de RAG, reusado por todas as skills -- o kb_id vem do
    array `kb_ids` da skill ativa."""
    return search_kb(kb_id, query, top_k)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
