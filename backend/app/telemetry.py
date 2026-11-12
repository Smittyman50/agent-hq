"""OpenTelemetry for Arize Phoenix (directly or through an OpenTelemetry Collector).

Set OTEL_EXPORTER_OTLP_ENDPOINT (e.g. http://perceptor:4318, OTLP over HTTP) and:

* Every agent run, model call and tool call becomes a span. The OpenInference span
  processor reshapes PydanticAI's spans into the conventions Phoenix renders natively
  (agent / LLM / tool spans with inputs, outputs and token counts).
* Each job runs inside a "job" span whose parent is the span that queued it. The W3C
  traceparent travels with the job through Valkey, so a conversation turn, its
  delegations on other machines and any background report all form one trace.
* Every span inside a job carries session.id = the conversation ID (Phoenix groups
  them as a session) plus agent, job, kind, pool and worker attributes.
* Images (screenshots sent to vision models) are never put into spans.
  AHQ_TRACE_CONTENT=0 also leaves out prompts and replies.
* The MCP libraries (the MCP Python SDK and FastMCP) emit their own protocol spans for
  every tool listing and call ("MCP send tools/call ...", "server/discover", ...). They
  carry no OpenInference kind, so Phoenix shows them as "unknown", and they repeat what
  PydanticAI's execute_tool span already shows. They're left out unless AHQ_TRACE_MCP=1,
  which keeps them, labelled as tool or chain spans.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from contextvars import ContextVar
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.propagate import extract, inject

log = logging.getLogger("agent_hq.telemetry")
_enabled = False

TRACER = "agent_hq"

# Attributes stamped onto every span started while a job runs.
_job_attrs: ContextVar[dict[str, Any] | None] = ContextVar("ahq_job_attrs", default=None)


def _job_attributes_processor():
    from opentelemetry.sdk.trace import SpanProcessor

    class JobAttributesProcessor(SpanProcessor):
        """Copies the current job's attributes onto every new span."""

        def on_start(self, span, parent_context=None) -> None:
            attrs = _job_attrs.get()
            if attrs:
                span.set_attributes(attrs)

    return JobAttributesProcessor()


# Instrumentation scopes of the MCP libraries' own protocol spans.
MCP_SCOPES = {"mcp-python-sdk", "fastmcp"}


def _scope(span) -> str:
    scope = getattr(span, "instrumentation_scope", None)
    return getattr(scope, "name", "") or ""


def _mcp_filter(inner, keep: bool):
    """Wraps the exporting processor: drops MCP protocol spans, or (keep) labels them."""
    from opentelemetry.sdk.trace import SpanProcessor

    class McpSpanFilter(SpanProcessor):
        def on_start(self, span, parent_context=None) -> None:
            if keep and _scope(span) in MCP_SCOPES:
                kind = "TOOL" if "tools/call" in (span.name or "") else "CHAIN"
                span.set_attribute("openinference.span.kind", kind)
            inner.on_start(span, parent_context)

        def on_end(self, span) -> None:
            if not keep and _scope(span) in MCP_SCOPES:
                return
            inner.on_end(span)

        def shutdown(self) -> None:
            inner.shutdown()

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return inner.force_flush(timeout_millis)

    return McpSpanFilter()


def setup_telemetry(service_suffix: str = "", *, exporter=None, force: bool = False) -> bool:
    """Configure tracing. `exporter` is for tests (e.g. an InMemorySpanExporter)."""
    global _enabled
    if _enabled and not force:
        return True
    if exporter is None and not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    from openinference.instrumentation.pydantic_ai import OpenInferenceSpanProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    from pydantic_ai import Agent
    from pydantic_ai.models.instrumented import InstrumentationSettings

    base = os.environ.get("OTEL_SERVICE_NAME", "agent-hq")
    service = f"{base}-{service_suffix}" if service_suffix else base
    attrs = {"service.name": service}
    # Phoenix files traces into the project named by this resource attribute ("default"
    # without it). The API and every worker share one project, whatever their service name.
    # OTEL_RESOURCE_ATTRIBUTES=openinference.project.name=... still wins if you set it.
    if "openinference.project.name" not in os.environ.get("OTEL_RESOURCE_ATTRIBUTES", ""):
        attrs["openinference.project.name"] = os.environ.get("PHOENIX_PROJECT_NAME") or base
    provider = TracerProvider(resource=Resource.create(attrs))
    # Order matters: stamp job attributes, reshape for Phoenix, then export.
    provider.add_span_processor(_job_attributes_processor())
    provider.add_span_processor(OpenInferenceSpanProcessor())
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        export = BatchSpanProcessor(OTLPSpanExporter())
    else:
        export = SimpleSpanProcessor(exporter)
    keep_mcp = os.environ.get("AHQ_TRACE_MCP", "0") in ("1", "true", "yes")
    provider.add_span_processor(_mcp_filter(export, keep_mcp))
    trace.set_tracer_provider(provider)
    include_content = os.environ.get("AHQ_TRACE_CONTENT", "1") not in ("0", "false", "no")
    Agent.instrument_all(InstrumentationSettings(
        tracer_provider=provider, include_binary_content=False, include_content=include_content))
    _enabled = True
    log.info("tracing enabled (%s)%s", service,
             f" -> {os.environ['OTEL_EXPORTER_OTLP_ENDPOINT']}" if exporter is None else "")
    return True


def current_traceparent() -> str | None:
    """W3C traceparent of the active span, to store on a job being queued."""
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier.get("traceparent")


@contextlib.contextmanager
def job_span(job, agent_name: str, worker: str):
    """Run a job inside a span continuing the trace that queued it."""
    attrs = {
        "session.id": job.origin_thread_id,
        "agent_hq.agent": agent_name,
        "agent_hq.job_id": job.id,
        "agent_hq.job_kind": str(getattr(job.kind, "value", job.kind)),
        "agent_hq.pool": job.pool,
        "agent_hq.worker": worker,
        "agent_hq.attempt": job.attempts,
        "metadata": json.dumps({"agent": agent_name, "job_id": job.id, "pool": job.pool,
                                "worker": worker, "depth": job.depth}),
    }
    parent = extract({"traceparent": job.trace_parent}) if job.trace_parent else otel_context.Context()
    tracer = trace.get_tracer(TRACER)
    kind = attrs["agent_hq.job_kind"]
    token = _job_attrs.set(attrs)
    try:
        with tracer.start_as_current_span(
            f"{kind} {agent_name}", context=parent, kind=trace.SpanKind.CONSUMER,
            attributes={**attrs, "openinference.span.kind": "CHAIN" if kind == "turn" else "AGENT",
                        "input.value": job.task[:4000]},
        ) as span:
            yield span
    finally:
        _job_attrs.reset(token)


@contextlib.contextmanager
def delivery_span(hook: str, ip: str):
    """A span for one webhook delivery. Jobs it queues continue this trace (they store the
    current traceparent), so Phoenix shows delivery -> turn -> delegations as one trace."""
    tracer = trace.get_tracer(TRACER)
    with tracer.start_as_current_span(
        f"webhook {hook}", kind=trace.SpanKind.SERVER,
        attributes={"openinference.span.kind": "CHAIN", "agent_hq.webhook": hook,
                    "agent_hq.webhook.ip": ip},
    ) as span:
        yield span


def set_output(span, value: Any) -> None:
    if span is not None and span.is_recording():
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        span.set_attribute("output.value", text[:4000])
