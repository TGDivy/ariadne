"""Low-cardinality OpenTelemetry signals for Ariadne's Codex runtime."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote

from openai_codex.generated.v2_all import (
    CommandExecutionThreadItem,
    FileChangeThreadItem,
    McpToolCallThreadItem,
    ThreadTokenUsage,
    TokenUsageBreakdown,
    WebSearchThreadItem,
)
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Counter, Histogram, Meter, UpDownCounter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer

if TYPE_CHECKING:
    from .config import TelemetryConfig

LOGGER = logging.getLogger(__name__)

TurnStatus = Literal["success", "failure", "cancelled"]

_INSTRUMENTATION_NAME = "ariadne"
_GEN_AI_OPERATION = "invoke_agent"
_GEN_AI_PROVIDER = "openai"
_TOKEN_BUCKETS = (
    100,
    500,
    1_000,
    5_000,
    10_000,
    50_000,
    100_000,
    250_000,
    500_000,
    1_000_000,
    2_000_000,
    5_000_000,
    10_000_000,
)


def _package_version() -> str:
    try:
        return version("ariadne")
    except PackageNotFoundError:
        return "unknown"


def _resource(service_name: str) -> Resource:
    return Resource.create(
        {
            "service.name": service_name,
            "service.version": _package_version(),
        }
    )


def _tool_name(item: object) -> str | None:
    if isinstance(item, WebSearchThreadItem):
        return "web_search"
    if isinstance(item, CommandExecutionThreadItem):
        return "shell"
    if isinstance(item, FileChangeThreadItem):
        return "file_change"
    if isinstance(item, McpToolCallThreadItem):
        return f"mcp.{item.tool}"
    return None


def _tool_status(item: object) -> TurnStatus:
    if isinstance(item, CommandExecutionThreadItem) and item.exit_code not in (None, 0):
        return "failure"
    if isinstance(item, McpToolCallThreadItem) and item.error is not None:
        return "failure"
    raw_status = getattr(getattr(item, "status", None), "value", "")
    if raw_status in {"failed", "declined", "cancelled", "canceled"}:
        return "cancelled" if raw_status in {"cancelled", "canceled"} else "failure"
    return "success"


def _reported_duration(item: object) -> float | None:
    duration_ms = getattr(item, "duration_ms", None)
    if isinstance(duration_ms, int):
        return duration_ms / 1000
    return None


@dataclass(slots=True)
class _ToolObservation:
    name: str
    started_at: float
    span: Span


class Telemetry:
    """Own Ariadne's instruments and, when configured, its OTLP providers."""

    def __init__(
        self,
        *,
        meter_provider: MeterProvider | None = None,
        tracer_provider: TracerProvider | None = None,
        owns_providers: bool = False,
    ) -> None:
        self._meter_provider = meter_provider
        self._tracer_provider = tracer_provider
        self._owns_providers = owns_providers
        self.enabled = meter_provider is not None or tracer_provider is not None
        self._meter: Meter = (
            meter_provider.get_meter(_INSTRUMENTATION_NAME, _package_version())
            if meter_provider is not None
            else metrics.get_meter(_INSTRUMENTATION_NAME, _package_version())
        )
        self._tracer: Tracer = (
            tracer_provider.get_tracer(_INSTRUMENTATION_NAME, _package_version())
            if tracer_provider is not None
            else trace.get_tracer(_INSTRUMENTATION_NAME, _package_version())
        )

        self._process_starts = self._meter.create_counter(
            "ariadne.process.starts",
            description="Ariadne process starts.",
            unit="{start}",
        )
        self._turns: Counter = self._meter.create_counter(
            "ariadne.codex.turns",
            description="Completed Codex turns.",
            unit="{turn}",
        )
        self._active_turns: UpDownCounter = self._meter.create_up_down_counter(
            "ariadne.codex.active_turns",
            description="Codex turns currently in progress.",
            unit="{turn}",
        )
        self._threads: Counter = self._meter.create_counter(
            "ariadne.codex.threads",
            description="Codex threads started.",
            unit="{thread}",
        )
        self._usage_reports: Counter = self._meter.create_counter(
            "ariadne.codex.usage_reports",
            description="Completed turns with a final token-usage snapshot.",
            unit="{report}",
        )
        self._input_tokens = self._token_counter(
            "input_tokens", "Input tokens reported by Codex."
        )
        self._cached_input_tokens = self._token_counter(
            "cached_input_tokens", "Cached input tokens reported by Codex."
        )
        self._uncached_input_tokens = self._token_counter(
            "uncached_input_tokens", "Input tokens not served from cache."
        )
        self._cache_write_input_tokens = self._token_counter(
            "cache_write_input_tokens", "Cache-write input tokens reported by Codex."
        )
        self._output_tokens = self._token_counter(
            "output_tokens", "Output tokens reported by Codex."
        )
        self._reasoning_tokens = self._token_counter(
            "reasoning_tokens", "Reasoning output tokens reported by Codex."
        )
        self._turn_duration: Histogram = self._meter.create_histogram(
            "ariadne.codex.turn.duration",
            description="End-to-end Codex turn duration.",
            unit="s",
        )
        self._time_to_first_response: Histogram = self._meter.create_histogram(
            "ariadne.codex.turn.time_to_first_response",
            description="Time from turn request to the first response delta.",
            unit="s",
        )
        self._thread_total_tokens: Histogram = self._meter.create_histogram(
            "ariadne.codex.thread.total_tokens",
            description=(
                "Cumulative thread token total in the final Codex usage snapshot; "
                "this is not the current context size."
            ),
            unit="{token}",
        )
        self._compactions: Counter = self._meter.create_counter(
            "ariadne.codex.compactions",
            description="Codex context compactions.",
            unit="{compaction}",
        )
        self._tool_calls: Counter = self._meter.create_counter(
            "ariadne.codex.tool.calls",
            description="Completed Codex tool calls.",
            unit="{call}",
        )
        self._tool_duration: Histogram = self._meter.create_histogram(
            "ariadne.codex.tool.duration",
            description="Codex tool-call duration.",
            unit="s",
        )

        # These two instruments follow the OpenTelemetry GenAI conventions.
        self._gen_ai_duration: Histogram = self._meter.create_histogram(
            "gen_ai.client.operation.duration",
            description="GenAI operation duration.",
            unit="s",
        )
        self._gen_ai_token_usage: Histogram = self._meter.create_histogram(
            "gen_ai.client.token.usage",
            description="Number of input and output tokens used per GenAI operation.",
            unit="{token}",
        )

        if meter_provider is not None:
            self._process_starts.add(1)

    def _token_counter(self, suffix: str, description: str) -> Counter:
        return self._meter.create_counter(
            f"ariadne.codex.{suffix}", description=description, unit="{token}"
        )

    def start_turn(
        self, *, source: str, model: str, reasoning_effort: str
    ) -> TurnObservation:
        return TurnObservation(
            self,
            source=source,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    def thread_started(self, *, source: str, model: str, reasoning_effort: str) -> None:
        self._threads.add(
            1,
            {
                "source": source,
                "model": model,
                "reasoning_effort": reasoning_effort,
            },
        )

    def shutdown(self) -> None:
        """Flush owned providers. Export failures are logged by the OTel SDK."""
        if not self._owns_providers:
            return
        if self._tracer_provider is not None:
            self._tracer_provider.shutdown()
        if self._meter_provider is not None:
            self._meter_provider.shutdown()


class TurnObservation:
    """Accumulate one turn and emit its final values exactly once."""

    def __init__(
        self,
        telemetry: Telemetry,
        *,
        source: str,
        model: str,
        reasoning_effort: str,
    ) -> None:
        self._telemetry = telemetry
        self._attributes = {
            "source": source,
            "model": model,
            "reasoning_effort": reasoning_effort,
        }
        self._gen_ai_attributes = {
            "gen_ai.operation.name": _GEN_AI_OPERATION,
            "gen_ai.provider.name": _GEN_AI_PROVIDER,
            "gen_ai.request.model": model,
            "ariadne.source": source,
            "ariadne.reasoning_effort": reasoning_effort,
        }
        self._started_at = time.monotonic()
        self._first_response_at: float | None = None
        self._usage: ThreadTokenUsage | None = None
        self._usage_baseline: TokenUsageBreakdown | None = None
        self._tools: dict[str, _ToolObservation] = {}
        self._finished = False
        self._span = telemetry._tracer.start_span(
            f"{_GEN_AI_OPERATION} {model}",
            kind=SpanKind.CLIENT,
            attributes=self._gen_ai_attributes,
        )
        telemetry._active_turns.add(1, self._attributes)

    def first_response(self) -> None:
        if self._first_response_at is None:
            self._first_response_at = time.monotonic()

    def usage(
        self,
        usage: ThreadTokenUsage,
        baseline: TokenUsageBreakdown | None = None,
    ) -> None:
        """Retain cumulative usage and its pre-turn baseline.

        Codex's ``last`` field is one model request, while a turn can make
        several requests around tool calls. The cumulative ``total`` delta is
        therefore the complete turn usage.
        """
        self._usage_baseline = baseline
        self._usage = usage

    def compacted(self) -> None:
        self._telemetry._compactions.add(1, self._attributes)
        self._span.add_event("gen_ai.context.compacted")

    def tool_started(self, item: object) -> None:
        name = _tool_name(item)
        item_id = getattr(item, "id", None)
        if name is None or not isinstance(item_id, str) or item_id in self._tools:
            return
        span = self._telemetry._tracer.start_span(
            f"execute_tool {name}",
            context=trace.set_span_in_context(self._span),
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.provider.name": _GEN_AI_PROVIDER,
                "gen_ai.request.model": self._attributes["model"],
                "gen_ai.tool.name": name,
                "ariadne.source": self._attributes["source"],
                "ariadne.reasoning_effort": self._attributes["reasoning_effort"],
            },
        )
        self._tools[item_id] = _ToolObservation(name, time.monotonic(), span)

    def tool_completed(self, item: object) -> None:
        name = _tool_name(item)
        item_id = getattr(item, "id", None)
        if name is None or not isinstance(item_id, str):
            return
        observation = self._tools.pop(item_id, None)
        status = _tool_status(item)
        attributes = {**self._attributes, "tool": name, "status": status}
        self._telemetry._tool_calls.add(1, attributes)
        reported_duration = _reported_duration(item)
        duration = (
            reported_duration
            if reported_duration is not None
            else time.monotonic() - observation.started_at
            if observation is not None
            else None
        )
        if duration is not None:
            self._telemetry._tool_duration.record(duration, attributes)
        if observation is not None:
            observation.span.set_attribute("ariadne.status", status)
            if status != "success":
                observation.span.set_status(Status(StatusCode.ERROR))
            observation.span.end()

    def finish(self, status: TurnStatus, error: BaseException | None = None) -> None:
        if self._finished:
            return
        self._finished = True
        finished_at = time.monotonic()
        duration = finished_at - self._started_at
        attributes = {**self._attributes, "status": status}

        for observation in self._tools.values():
            tool_attributes = {
                **self._attributes,
                "tool": observation.name,
                "status": status,
            }
            self._telemetry._tool_calls.add(1, tool_attributes)
            self._telemetry._tool_duration.record(
                finished_at - observation.started_at, tool_attributes
            )
            observation.span.set_attribute("ariadne.status", status)
            if status != "success":
                observation.span.set_status(Status(StatusCode.ERROR))
            observation.span.end()
        self._tools.clear()

        self._telemetry._active_turns.add(-1, self._attributes)
        self._telemetry._turns.add(1, attributes)
        self._telemetry._turn_duration.record(duration, attributes)
        if self._first_response_at is not None:
            self._telemetry._time_to_first_response.record(
                self._first_response_at - self._started_at, attributes
            )

        gen_ai_attributes = {**self._gen_ai_attributes, "ariadne.status": status}
        self._telemetry._gen_ai_duration.record(duration, gen_ai_attributes)
        if self._usage is not None:
            self._emit_usage(attributes, gen_ai_attributes)

        self._span.set_attribute("ariadne.status", status)
        self._span.set_attribute("ariadne.turn.duration", duration)
        if error is not None:
            error_type = type(error).__qualname__
            self._span.set_attribute("error.type", error_type)
        if status != "success":
            self._span.set_status(Status(StatusCode.ERROR))
        self._span.end()

    def _emit_usage(
        self, attributes: dict[str, str], gen_ai_attributes: dict[str, str]
    ) -> None:
        assert self._usage is not None
        usage = _usage_delta(self._usage.total, self._usage_baseline)
        self._telemetry._usage_reports.add(1, attributes)
        self._telemetry._input_tokens.add(usage.input_tokens, attributes)
        self._telemetry._cached_input_tokens.add(usage.cached_input_tokens, attributes)
        self._telemetry._uncached_input_tokens.add(
            max(usage.input_tokens - usage.cached_input_tokens, 0), attributes
        )
        self._telemetry._cache_write_input_tokens.add(
            usage.cache_write_input_tokens or 0, attributes
        )
        self._telemetry._output_tokens.add(usage.output_tokens, attributes)
        self._telemetry._reasoning_tokens.add(usage.reasoning_output_tokens, attributes)
        self._telemetry._thread_total_tokens.record(
            self._usage.total.total_tokens, attributes
        )
        self._telemetry._gen_ai_token_usage.record(
            usage.input_tokens,
            {**gen_ai_attributes, "gen_ai.token.type": "input"},
        )
        self._telemetry._gen_ai_token_usage.record(
            usage.output_tokens,
            {**gen_ai_attributes, "gen_ai.token.type": "output"},
        )

        self._span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
        self._span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
        self._span.set_attribute(
            "gen_ai.usage.cache_read.input_tokens", usage.cached_input_tokens
        )
        self._span.set_attribute(
            "gen_ai.usage.cache_creation.input_tokens",
            usage.cache_write_input_tokens or 0,
        )
        self._span.set_attribute(
            "gen_ai.usage.reasoning.output_tokens", usage.reasoning_output_tokens
        )
        self._span.set_attribute(
            "ariadne.codex.thread.total_tokens", self._usage.total.total_tokens
        )
        if self._usage.model_context_window is not None:
            self._span.set_attribute(
                "ariadne.codex.model_context_window",
                self._usage.model_context_window,
            )


def _usage_delta(
    current: TokenUsageBreakdown,
    baseline: TokenUsageBreakdown | None,
) -> TokenUsageBreakdown:
    if baseline is None or current.total_tokens < baseline.total_tokens:
        baseline = TokenUsageBreakdown(
            input_tokens=0,
            cached_input_tokens=0,
            cache_write_input_tokens=0,
            output_tokens=0,
            reasoning_output_tokens=0,
            total_tokens=0,
        )

    def delta(current_value: int | None, baseline_value: int | None) -> int:
        return max((current_value or 0) - (baseline_value or 0), 0)

    return TokenUsageBreakdown(
        input_tokens=delta(current.input_tokens, baseline.input_tokens),
        cached_input_tokens=delta(
            current.cached_input_tokens, baseline.cached_input_tokens
        ),
        cache_write_input_tokens=delta(
            current.cache_write_input_tokens, baseline.cache_write_input_tokens
        ),
        output_tokens=delta(current.output_tokens, baseline.output_tokens),
        reasoning_output_tokens=delta(
            current.reasoning_output_tokens, baseline.reasoning_output_tokens
        ),
        total_tokens=delta(current.total_tokens, baseline.total_tokens),
    )


def configure_telemetry(config: TelemetryConfig | None = None) -> Telemetry:
    """Configure direct OTLP/HTTP export exclusively from Ariadne's TOML."""
    if config is None or not config.enabled:
        LOGGER.info("OpenTelemetry export is disabled in Ariadne configuration")
        return Telemetry()

    assert config.endpoint is not None
    assert config.authorization is not None
    base_endpoint = str(config.endpoint).rstrip("/")
    headers = {"Authorization": unquote(config.authorization.get_secret_value())}
    resource = _resource(config.service_name)
    meter_provider: MeterProvider | None = None
    tracer_provider: TracerProvider | None = None
    try:
        if config.metrics:
            reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=f"{base_endpoint}/v1/metrics",
                    headers=headers,
                ),
                export_interval_millis=config.export_interval_seconds * 1000,
            )
            token_buckets = ExplicitBucketHistogramAggregation(
                boundaries=_TOKEN_BUCKETS
            )
            meter_provider = MeterProvider(
                metric_readers=[reader],
                resource=resource,
                views=[
                    View(
                        instrument_name="ariadne.codex.thread.total_tokens",
                        aggregation=token_buckets,
                    ),
                    View(
                        instrument_name="gen_ai.client.token.usage",
                        aggregation=token_buckets,
                    ),
                ],
            )
        if config.traces:
            tracer_provider = TracerProvider(resource=resource)
            tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=f"{base_endpoint}/v1/traces",
                        headers=headers,
                    )
                )
            )
    except Exception:
        LOGGER.exception("OpenTelemetry setup failed; continuing without export")
        if tracer_provider is not None:
            tracer_provider.shutdown()
        if meter_provider is not None:
            meter_provider.shutdown()
        return Telemetry()

    LOGGER.info(
        "OpenTelemetry OTLP/HTTP export enabled (metrics=%s, traces=%s)",
        config.metrics,
        config.traces,
    )
    return Telemetry(
        meter_provider=meter_provider,
        tracer_provider=tracer_provider,
        owns_providers=True,
    )
