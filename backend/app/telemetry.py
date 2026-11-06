"""OpenTelemetry setup. Point OTEL_EXPORTER_OTLP_ENDPOINT at your Collector
(e.g. http://perceptor:4318) and traces flow on to Phoenix."""

from __future__ import annotations

import logging
import os

log = logging.getLogger("agent_hq.telemetry")


def setup_telemetry() -> None:
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        log.info("OTEL_EXPORTER_OTLP_ENDPOINT not set; tracing disabled")
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from pydantic_ai import Agent

    service = os.environ.get("OTEL_SERVICE_NAME", "agent-hq")
    provider = TracerProvider(resource=Resource.create({"service.name": service}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    # Every PydanticAI agent run, model call and tool call becomes a span.
    Agent.instrument_all()
    log.info("tracing enabled -> %s", os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"])
