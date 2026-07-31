"""
Parse do prompt no formato OpenAI-style: [{"role": "system", ...}, {"role": "user", ...}].
O SDK fixo só expõe agentId/sessionId/prompt como string — o array vem serializado
dentro desse campo. Contrato: content do systemMessage é JSON com id_cliente.
"""
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def parse_prompt(raw_prompt: str) -> tuple[str, Optional[dict]]:
    """Retorna (user_content, system_payload). Nunca lança — prompt malformado
    vira fallback de string pura, o grafo não pode cair por causa de parsing."""
    try:
        parsed = json.loads(raw_prompt)
    except json.JSONDecodeError:
        return raw_prompt, None

    if not isinstance(parsed, list):
        return raw_prompt, None

    system_msg = next((m for m in parsed if m.get("role") == "system"), None)
    user_msg = next((m for m in parsed if m.get("role") == "user"), None)
    user_content = user_msg["content"] if user_msg else raw_prompt

    system_payload: Optional[dict] = None
    if system_msg is not None:
        try:
            system_payload = json.loads(system_msg["content"])
        except (json.JSONDecodeError, TypeError):
            logger.warning("systemMessage.content não é JSON válido — ignorando id_cliente")
            system_payload = None

    return user_content, system_payload
