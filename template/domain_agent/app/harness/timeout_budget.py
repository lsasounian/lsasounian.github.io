"""
Timeout em cascata: o budget total da invocação é fatiado entre as fases
(classificação de intent, retrieval de KB, tool calls, geração final).
Cada fase consome do budget do TOTAL, não tem timeout independente —
isso evita que uma fase lenta "roube" tempo de outra sem ninguém perceber,
e garante que a invocação nunca estoura o limite duro do caller
(o SDK do mediador, no seu caso, que não é evoluível do seu lado).
"""

import time
from contextlib import contextmanager
from dataclasses import dataclass


class BudgetExhaustedError(Exception):
    def __init__(self, phase: str, remaining_ms: float):
        self.phase = phase
        self.remaining_ms = remaining_ms
        super().__init__(
            f"budget esgotado na fase '{phase}' (restavam {remaining_ms:.0f}ms)"
        )


@dataclass
class InvocationBudget:
    total_ms: int
    _start: float

    @classmethod
    def start(cls, total_ms: int) -> "InvocationBudget":
        return cls(total_ms=total_ms, _start=time.monotonic())

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._start) * 1000

    @property
    def remaining_ms(self) -> float:
        return max(0.0, self.total_ms - self.elapsed_ms)

    def check(self, phase: str) -> None:
        if self.remaining_ms <= 0:
            raise BudgetExhaustedError(phase, self.remaining_ms)

    @contextmanager
    def phase(self, phase_name: str, phase_budget_ms: int):
        """Aloca no máximo `phase_budget_ms` OU o restante do budget total,
        o que for menor. Não estende o total — só limita a fase."""
        self.check(phase_name)
        allotted = min(phase_budget_ms, self.remaining_ms)
        phase_start = time.monotonic()
        try:
            yield allotted
        finally:
            spent = (time.monotonic() - phase_start) * 1000
            # log/metric hook: registrar `spent` vs `allotted` por fase
            # (ver harness/observability.py) para detectar fases que
            # consistentemente estouram o próprio orçamento.
            _ = spent
