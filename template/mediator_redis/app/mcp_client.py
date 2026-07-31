"""
Client MCP (Streamable HTTP) via langchain-mcp-adapters. Headers dinâmicos
carregam session_id/agent_id/id_cliente do harness a cada chamada — a LLM
nunca vê esses campos, eles não fazem parte do schema de nenhum tool.
"""
from langchain_mcp_adapters.client import MultiServerMCPClient

from app.config import settings
from app.harness.context import mcp_headers
from app.harness.secrets import get_gateway_token


def build_mcp_client() -> MultiServerMCPClient:
    return MultiServerMCPClient(
        {
            "specialists": {
                "transport": "streamable_http",
                "url": settings.mcp_gateway_url,
                "headers": lambda: mcp_headers(get_gateway_token()),
            }
        }
    )
