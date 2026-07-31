"""
Setup de OpenTelemetry (traces + metrics) consistente com o que já foi
definido pro mediador: OTLP exporter, correlação trace_id (X-Ray via ADOT)
com thread_id/session_id do AgentCore para permitir juntar os spans do
mediador com os spans deste agente filho numa mesma trace distribuída.
"""

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

logger = logging.getLogger("child_agent")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

_tracer: trace.Tracer | None = None
_meter: metrics.Meter | None = None


def init_observability(service_name: str) -> None:
    global _tracer, _meter

    resource = Resource.create({SERVICE_NAME: service_name})

    provider = TracerProvider(resource=resource)
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if otlp_endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint)))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)

    meter_provider = MeterProvider(resource=resource)
    metrics.set_meter_provider(meter_provider)
    _meter = metrics.get_meter(service_name)


def get_tracer() -> trace.Tracer:
    assert _tracer is not None, "chamar init_observability() antes"
    return _tracer


def get_meter() -> metrics.Meter:
    assert _meter is not None, "chamar init_observability() antes"
    return _meter


def bind_correlation(session_id: str) -> None:
    """Anexa session_id (== thread_id do LangGraph) como atributo do span
    corrente. Chamar logo no início do handler de /invocations, dentro do
    span raiz criado pelo ADOT/X-Ray — assim trace_id e thread_id ficam
    correlacionáveis nos logs e no X-Ray para debugar uma conversa
    específica atravessando mediador -> agente filho."""
    span = trace.get_current_span()
    span.set_attribute("session_id", session_id)
    span.set_attribute("thread_id", session_id)
