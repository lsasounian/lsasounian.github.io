"""
Setup básico de OpenTelemetry para exportar traces pro Langfuse via OTLP/HTTP.

Langfuse expõe um endpoint OTLP nativo em {LANGFUSE_HOST}/api/public/otel/v1/traces,
autenticado com Basic Auth (public_key:secret_key em base64). Só HTTP é suportado
(JSON ou protobuf) -- gRPC não.
"""

import base64

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config import settings

_initialized = False


def setup_telemetry() -> None:
    """Chamar uma vez no startup da API (ver lifespan em app/api/main.py)."""
    global _initialized
    if _initialized:
        return

    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        # sem credenciais, segue sem exportar (útil pra rodar o POC sem Langfuse configurado)
        _initialized = True
        return

    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    ).decode()

    exporter = OTLPSpanExporter(
        endpoint=f"{settings.langfuse_host}/api/public/otel/v1/traces",
        headers={"Authorization": f"Basic {auth}"},
    )

    provider = TracerProvider(
        resource=Resource.create({"service.name": settings.otel_service_name})
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    _initialized = True


def get_tracer(name: str = "mediator-agent"):
    return trace.get_tracer(name)
