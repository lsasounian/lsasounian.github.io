"""
Mock de KB pra esse POC. Documentos in-memory, busca por palavra-chave --
mesma filosofia de search_skills: sempre uma chamada de função simples, sem
embedding, sem vetor.

Pra evoluir pra produção: trocar `_DOCUMENTS`/`search_kb` por uma chamada real
(Bedrock Knowledge Bases, OpenSearch) mantendo a mesma assinatura de função,
pra não precisar mexer no server.py nem no agent.
"""

_DOCUMENTS: dict[str, list[dict]] = {
    "kb_cartoes_xpto": [
        {"id": "doc1", "text": "Para contestar uma cobrança indevida no cartão, o cliente deve abrir uma disputa em até 90 dias da fatura."},
        {"id": "doc2", "text": "O limite do cartão pode ser aumentado mediante análise de crédito, disponível no app em Cartões > Limite."},
        {"id": "doc3", "text": "A anuidade pode ser isentada para clientes com gasto mínimo mensal de R$ 1.000."},
    ],
    "kb_pf": [
        {"id": "doc1", "text": "O extrato da conta corrente fica disponível pelos últimos 12 meses no app."},
        {"id": "doc2", "text": "Transferências PIX acima de R$ 5.000 realizadas à noite passam por análise de segurança."},
    ],
    "kb_seguros": [
        {"id": "doc1", "text": "Para abrir um sinistro, o segurado deve acionar a central em até 30 dias do evento."},
    ],
}


def search_kb(kb_id: str, query: str, top_k: int = 3) -> list[dict]:
    documents = _DOCUMENTS.get(kb_id, [])
    query_words = query.lower().split()

    scored = []
    for doc in documents:
        score = sum(1 for w in query_words if w in doc["text"].lower())
        if score > 0:
            scored.append((score, doc))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [{"id": d["id"], "text": d["text"], "score": s} for s, d in scored[:top_k]]
