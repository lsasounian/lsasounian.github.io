"""
Cache do token do Gateway MCP — mesmo padrão do GatewayTokenProvider já usado
no projeto original. Evita chamar Secrets Manager a cada invocação.
"""
import time

import boto3

_cache: dict[str, tuple[str, float]] = {}
_client = boto3.client("secretsmanager")


def get_gateway_token(ttl_s: int = 300) -> str:
    cached = _cache.get("gateway_token")
    if cached and cached[1] > time.monotonic():
        return cached[0]

    from app.config import settings  # import local evita ciclo config <-> secrets

    secret = _client.get_secret_value(SecretId=settings.mcp_gateway_token_secret_arn)
    token = secret["SecretString"]
    _cache["gateway_token"] = (token, time.monotonic() + ttl_s)
    return token
