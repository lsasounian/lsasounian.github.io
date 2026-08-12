"""
Catálogo de skills -- seed estático pra esse POC.

Cada skill carrega `kb_ids`: array de bases de conhecimento que o rag_search
pode consultar pra essa skill. É isso que permite um único método de RAG
genérico em vez de uma tool de KB dedicada por domínio (kb_xpto_search,
kb_pf_search etc., como no desenho anterior).

Pra evoluir pra produção: trocar esse dict por leitura do DynamoDB, seguindo
o mesmo shape (skill_registry_mcp_server.py de uma sessão anterior já cobre
esse caminho).
"""

SKILLS_CATALOG: dict[str, dict] = {
    "cartoes": {
        "skill_id": "cartoes",
        "description": "Cartões: fatura, limite, anuidade, contestação de cobrança",
        "intent_examples": ["cartão", "cartões", "fatura", "limite do cartão", "anuidade"],
        "instructions": (
            "1. Use a tool rag_search (com os kb_ids dessa skill) antes de responder a pergunta do usuário\n"
            "2. Pesquise mesmo com pedidos incompletos, não assuma status de não informado\n"
            "3. Não misture regras, apresente cenários separados\n"
            "4. Se faltarem elementos para a resposta completa, questione o usuário antes de seguir"
        ),
        "kb_ids": ["kb_cartoes_xpto"],
        "allowed_tools": [],
    },
    "pf": {
        "skill_id": "pf",
        "description": "Conta corrente: saldo, extrato, transferência, pix",
        "intent_examples": ["conta corrente", "saldo", "extrato", "transferência", "pix"],
        "instructions": "# preencha com as regras reais dessa skill",
        "kb_ids": ["kb_pf"],
        "allowed_tools": [],
    },
    "seguros": {
        "skill_id": "seguros",
        "description": "Seguros: apólice, sinistro, cobertura",
        "intent_examples": ["seguro", "apólice", "sinistro", "cobertura"],
        "instructions": "# preencha com as regras reais dessa skill",
        "kb_ids": ["kb_seguros"],
        "allowed_tools": [],
    },
}
