from openai_codex.generated.v2_all import (
    McpToolCallStatus,
    McpToolCallThreadItem,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from ariadne.telemetry import Telemetry, configure_telemetry


def test_telemetry_is_disabled_without_an_otlp_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)

    telemetry = configure_telemetry()

    assert telemetry.enabled is False


def test_tool_traces_contain_metadata_but_not_arguments() -> None:
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    telemetry = Telemetry(
        meter_provider=meter_provider,
        tracer_provider=tracer_provider,
    )
    observation = telemetry.start_turn(
        source="mail",
        model="gpt-test",
        reasoning_effort="medium",
    )
    secret = "private mailbox search terms"
    started = McpToolCallThreadItem(
        arguments={"query": secret},
        id="tool-1",
        server="ariadne",
        status=McpToolCallStatus.in_progress,
        tool="search_mail",
        type="mcpToolCall",
    )
    completed = started.model_copy(
        update={"status": McpToolCallStatus.completed, "duration_ms": 125}
    )

    observation.tool_started(started)
    observation.tool_completed(completed)
    observation.finish("success")

    spans = span_exporter.get_finished_spans()
    assert [span.name for span in spans] == [
        "execute_tool mcp.search_mail",
        "invoke_agent gpt-test",
    ]
    assert spans[0].attributes["gen_ai.tool.name"] == "mcp.search_mail"
    assert secret not in repr(spans)

    metrics = {
        metric.name: metric
        for resource in reader.get_metrics_data().resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    tool_point = metrics["ariadne.codex.tool.calls"].data.data_points[0]
    assert tool_point.attributes == {
        "source": "mail",
        "model": "gpt-test",
        "reasoning_effort": "medium",
        "tool": "mcp.search_mail",
        "status": "success",
    }
    tracer_provider.shutdown()
    meter_provider.shutdown()
