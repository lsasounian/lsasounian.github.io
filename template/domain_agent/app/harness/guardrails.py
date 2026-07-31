"""
Guardrails determinísticos (não dependem do LLM "se comportar bem").
Rodam ANTES do grafo (input) e DEPOIS do grafo (output/intent).
"""

from app.config import HarnessConfig
from app.state import IntentResult


class GuardrailViolation(Exception):
    def __init__(self, rule: str, detail: str):
        self.rule = rule
        self.detail = detail
        super().__init__(f"guardrail '{rule}' violado: {detail}")


def validate_input(raw_input: str, cfg: HarnessConfig) -> None:
    if not raw_input or not raw_input.strip():
        raise GuardrailViolation("empty_input", "input vazio ou só whitespace")
    if len(raw_input) > cfg.max_input_chars:
        raise GuardrailViolation(
            "input_size",
            f"{len(raw_input)} chars excede o limite de {cfg.max_input_chars}",
        )


def validate_intent(intent: IntentResult, cfg: HarnessConfig) -> IntentResult:
    """Aplica allowlist + threshold de confiança. Retorna o intent
    original OU um intent de fallback determinístico ('unclassified')
    quando a confiança é baixa ou o label não está no allowlist deste
    agente — nesse caso o grafo NÃO oferece nenhuma tool de KB e segue
    para uma resposta conservadora (ver graph/nodes.py)."""
    if intent["confidence"] < cfg.intent_confidence_threshold:
        return {**intent, "label": "unclassified", "needs_kb": False, "kb_targets": []}

    if cfg.allowed_intents and intent["label"] not in cfg.allowed_intents:
        return {**intent, "label": "out_of_scope", "needs_kb": False, "kb_targets": []}

    return intent


def check_tool_iteration_limit(current_count: int, cfg: HarnessConfig) -> None:
    if current_count >= cfg.max_tool_iterations:
        raise GuardrailViolation(
            "tool_iteration_limit",
            f"{current_count} chamadas de tool >= limite de {cfg.max_tool_iterations}",
        )
