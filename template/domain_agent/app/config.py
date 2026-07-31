"""
Configuração central do agente filho.

Cada time deve customizar os valores abaixo (via env vars, não hardcode)
ao criar um novo agente a partir deste template. Os campos marcados com
TODO são obrigatórios para o agente funcionar em produção.
"""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HarnessConfig:
    # ---- Timeout em cascata (budget total dividido entre as fases) ----
    total_invocation_budget_ms: int = int(os.getenv("TOTAL_BUDGET_MS", "25000"))
    intent_classification_budget_ms: int = int(os.getenv("INTENT_BUDGET_MS", "2000"))
    kb_retrieval_budget_ms: int = int(os.getenv("KB_BUDGET_MS", "4000"))
    tool_call_budget_ms: int = int(os.getenv("TOOL_CALL_BUDGET_MS", "12000"))
    final_generation_budget_ms: int = int(os.getenv("FINAL_GEN_BUDGET_MS", "7000"))

    # ---- Circuit breaker (por resource key: mcp:<server>, kb:<nome>, llm) ----
    cb_failure_threshold: int = int(os.getenv("CB_FAILURE_THRESHOLD", "5"))
    cb_open_duration_s: int = int(os.getenv("CB_OPEN_DURATION_S", "30"))
    cb_half_open_max_calls: int = int(os.getenv("CB_HALF_OPEN_MAX_CALLS", "1"))

    # ---- Retry (exponential backoff + full jitter) ----
    retry_max_attempts: int = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))
    retry_base_delay_ms: int = int(os.getenv("RETRY_BASE_DELAY_MS", "200"))
    retry_max_delay_ms: int = int(os.getenv("RETRY_MAX_DELAY_MS", "4000"))

    # ---- Guardrails ----
    max_input_chars: int = int(os.getenv("MAX_INPUT_CHARS", "8000"))
    max_tool_iterations: int = int(os.getenv("MAX_TOOL_ITERATIONS", "6"))
    intent_confidence_threshold: float = float(os.getenv("INTENT_CONFIDENCE_THRESHOLD", "0.55"))
    # TODO(time): lista de intents que este agente está autorizado a atender.
    # Qualquer intent fora desse allowlist cai no fallback do guardrail.
    allowed_intents: tuple[str, ...] = tuple(
        os.getenv("ALLOWED_INTENTS", "").split(",")
    ) if os.getenv("ALLOWED_INTENTS") else ()


@dataclass(frozen=True)
class KBConfig:
    """
    Cada agente filho tem no máximo 1-2 KBs (banco vetorial via SDK próprio
    de RAG por similaridade — NÃO é Bedrock Knowledge Bases API).

    TODO(time): preencher `name` e `intent_tags` de cada KB habilitada
    para este agente. `intent_tags` é o vocabulário usado pelo classificador
    de intent (app/graph/nodes.py::classify_intent) para decidir se a KB
    deve ser oferecida como tool naquele turno.
    """
    name: str
    intent_tags: tuple[str, ...]
    top_k: int = 5
    similarity_threshold: float = 0.7


@dataclass(frozen=True)
class AppConfig:
    agent_name: str = os.getenv("AGENT_NAME", "child-agent-template")
    port: int = int(os.getenv("PORT", "8080"))
    llm_model: str = os.getenv("LLM_MODEL", "gpt-5.4")  # TODO(time): ajustar
    prime_llm_on_boot: bool = os.getenv("PRIME_LLM_ON_BOOT", "true").lower() == "true"

    # TODO(time): endpoints dos MCP servers que este agente CONSOME como
    # cliente (ferramentas externas). Isso é diferente de como este agente
    # é exposto como tool para o mediador — essa exposição é gerenciada
    # fora deste template.
    mcp_servers: dict = field(default_factory=lambda: {
        # "nome_da_ferramenta": {"transport": "streamable_http", "url": "http://..."},
    })

    # TODO(time): declarar as 1-2 KBs deste agente
    kbs: tuple[KBConfig, ...] = ()

    harness: HarnessConfig = field(default_factory=HarnessConfig)


config = AppConfig()
