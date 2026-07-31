"""
Este agente age como MCP CLIENT: durante o próprio loop de raciocínio ele
pode precisar chamar ferramentas externas expostas via MCP (Streamable
HTTP), da mesma forma que o mediador chama agentes especialistas.

Isso é ortogonal a como este agente é EXPOSTO como tool para o mediador
(isso é responsabilidade de outra camada, fora deste template — vocês
disseram que já têm o MCP server/wrapper de exposição resolvido).

`MultiServerMCPClient` é montado uma vez no prewarm (lifecycle.py) e
reusado — cada chamada de tool passa pelo circuit breaker + retry do
harness, keyed por "mcp:<nome_do_server>".
"""

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from app.harness.circuit_breaker import CircuitBreakerRegistry
from app.harness.retry import call_with_resilience


class ResilientMCPToolProvider:
    def __init__(self, servers_config: dict, breaker: CircuitBreakerRegistry, harness_cfg):
        # servers_config: {"nome": {"transport": "streamable_http", "url": "..."}}
        # TODO(time): preencher em app/config.py::AppConfig.mcp_servers
        self._client = MultiServerMCPClient(servers_config)
        self._breaker = breaker
        self._harness_cfg = harness_cfg
        self._tools_cache: list[BaseTool] | None = None

    async def get_tools(self) -> list[BaseTool]:
        """Descoberta eager — chamado no prewarm, cacheado depois."""
        if self._tools_cache is None:
            self._tools_cache = await self._client.get_tools()
        return self._tools_cache

    def wrap_tool(self, tool: BaseTool, server_name: str) -> BaseTool:
        """Envolve a tool original com circuit breaker + retry, mantendo
        nome/schema originais (o LLM não percebe a diferença)."""
        resource_key = f"mcp:{server_name}"
        original_coroutine = tool.coroutine

        async def _resilient_call(*args, **kwargs):
            return await call_with_resilience(
                resource_key=resource_key,
                fn=lambda: original_coroutine(*args, **kwargs),
                breaker=self._breaker,
                max_attempts=self._harness_cfg.retry_max_attempts,
                base_delay_ms=self._harness_cfg.retry_base_delay_ms,
                max_delay_ms=self._harness_cfg.retry_max_delay_ms,
            )

        tool.coroutine = _resilient_call
        return tool
