"""
Configuração centralizada do harness. Único lugar que lê variáveis de ambiente,
evitando os.environ espalhado pelo código.
"""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # --- Identidade do runtime ---
    agent_llm_model: str = os.getenv("AGENT_LLM_MODEL", "gpt-5.4")

    # --- Redis via fachada HTTP (cross-account proibido -> API Gateway + Lambda) ---
    redis_facade_base_url: str = os.getenv("REDIS_FACADE_BASE_URL", "")
    redis_facade_mtls_cert: str = os.getenv("REDIS_FACADE_MTLS_CERT_PATH", "/opt/certs/client.pem")
    redis_facade_mtls_key: str = os.getenv("REDIS_FACADE_MTLS_KEY_PATH", "/opt/certs/client.key")
    redis_facade_timeout_s: float = float(os.getenv("REDIS_FACADE_TIMEOUT_S", "2.0"))

    # --- MCP Gateway (especialistas) ---
    mcp_gateway_url: str = os.getenv("MCP_GATEWAY_URL", "")
    mcp_gateway_token_secret_arn: str = os.getenv("MCP_GATEWAY_TOKEN_SECRET_ARN", "")

    # --- Lifecycle ---
    idle_timeout_s: int = int(os.getenv("IDLE_TIMEOUT_S", str(15 * 60)))    # espelha default AgentCore
    hard_ceiling_hours: int = int(os.getenv("HARD_CEILING_HOURS", "8"))     # espelha MaxLifetime
    prime_llm_on_boot: bool = os.getenv("PRIME_LLM_ON_BOOT", "false").lower() == "true"

    # --- Execução ---
    graph_timeout_budget_s: float = float(os.getenv("GRAPH_TIMEOUT_BUDGET_S", "25.0"))
    max_iterations: int = int(os.getenv("MAX_ITERATIONS", "8"))


settings = Settings()
