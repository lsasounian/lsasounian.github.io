"""
Skill Registry self-hospedado, exposto via MCP, backed por DynamoDB.

Modelo de dados (single-table):
    PK: skill_id            (ex: "cartoes")
    SK: "v{n}"               -- cada versão publicada é um item próprio
    SK: "LATEST"             -- ponteiro pra versão ativa atual (mesmo shape do item de versão)

Busca é sempre uma chamada de API por trás do MCP server -- sem índice de
embedding, sem cache local, sem infra de vetor. Pra um catálogo pequeno/médio
(dezenas a poucas centenas de skills), scan + filtro é suficiente; se crescer
muito, trocar por um GSI com keywords antes de considerar algo mais pesado.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import boto3
from boto3.dynamodb.conditions import Attr
from mcp.server.fastmcp import FastMCP

TABLE_NAME = "skill-registry"
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
s3 = boto3.client("s3")

mcp = FastMCP("skill-registry")


# ---------------------------------------------------------------------------
# Modelo
# ---------------------------------------------------------------------------

@dataclass
class SkillRecord:
    skill_id: str
    version: int
    description: str
    intent_examples: list[str]
    mcp_servers: list[str]
    allowed_tools: list[str]
    instructions: str | None      # cabe direto no item se pequeno
    s3_uri: str | None            # ou ponteiro S3 se o conteúdo for grande
    status: Literal["draft", "active", "deprecated"]
    owner: str


# ---------------------------------------------------------------------------
# Publicação -- grava a versão nova e move o ponteiro LATEST atomicamente
# ---------------------------------------------------------------------------

def publish_skill(record: SkillRecord) -> None:
    base_item = {
        "skill_id": record.skill_id,
        "description": record.description,
        "intent_examples": record.intent_examples,
        "mcp_servers": record.mcp_servers,
        "allowed_tools": record.allowed_tools,
        "instructions": record.instructions,
        "s3_uri": record.s3_uri,
        "status": record.status,
        "owner": record.owner,
        "updated_at": int(time.time()),
    }
    version_item = {**base_item, "sk": f"v{record.version}"}
    latest_item = {**base_item, "sk": "LATEST"}

    # transação evita ficar com o ponteiro LATEST apontando pra versão errada
    # se o processo cair entre os dois writes
    dynamodb.meta.client.transact_write_items(
        TransactItems=[
            {"Put": {"TableName": TABLE_NAME, "Item": version_item}},
            {"Put": {"TableName": TABLE_NAME, "Item": latest_item}},
        ]
    )


# ---------------------------------------------------------------------------
# Tools MCP -- o que o Main Agent (ou qualquer client MCP) chama
# ---------------------------------------------------------------------------

@mcp.tool()
def search_skills(query: str, top_k: int = 3) -> list[dict]:
    """Busca skills por palavra-chave na descrição e nas intent_examples.
    Sempre uma chamada de API -- sem embedding, sem cache local."""
    query_words = query.lower().split()

    response = table.scan(
        FilterExpression=Attr("sk").eq("LATEST") & Attr("status").eq("active")
    )
    scored = []
    for item in response["Items"]:
        haystack = " ".join([item["description"], *item["intent_examples"]]).lower()
        score = sum(1 for word in query_words if word in haystack)
        if score > 0:
            scored.append((score, item))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [
        {"skill_id": item["skill_id"], "description": item["description"], "score": score}
        for score, item in scored[:top_k]
    ]


@mcp.tool()
def get_skill(skill_id: str, version: str | None = None) -> dict:
    """Retorna a skill completa (instruções + tools + mcp_servers).
    Sem version informado, retorna a LATEST ativa."""
    sk = f"v{version}" if version else "LATEST"
    response = table.get_item(Key={"skill_id": skill_id, "sk": sk})
    item = response.get("Item")
    if not item:
        raise ValueError(f"Skill '{skill_id}' versão '{sk}' não encontrada")

    if item.get("s3_uri") and not item.get("instructions"):
        bucket, key = item["s3_uri"].replace("s3://", "").split("/", 1)
        item["instructions"] = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
    return item


@mcp.tool()
def list_skills(status: str = "active") -> list[dict]:
    """Lista todas as skills na versão LATEST com o status informado."""
    response = table.scan(
        FilterExpression=Attr("sk").eq("LATEST") & Attr("status").eq(status)
    )
    return [
        {"skill_id": i["skill_id"], "description": i["description"]}
        for i in response["Items"]
    ]


if __name__ == "__main__":
    # streamable-http pra rodar atrás de ALB + ECS Fargate (não Lambda + API
    # Gateway, pelo mesmo motivo da sua migração WebSocket -> SSE)
    mcp.run(transport="streamable-http")
