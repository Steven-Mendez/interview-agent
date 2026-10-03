"""Anonymous metrics over OTLP, for Grafana.

Every measurement keeps its (component, name). A known value is a sample of the
exponential histogram `interview_agent.<component>.<name>`; a missing one (None,
NaN or infinite) adds one to the counter `interview_agent.<component>.<name>.unknown`,
so absent data is never averaged in as zero. Attributes are categories our own
code produces (DIMENSIONS): never a trace, interview, turn or user ID, nor content.

A state rather than an event (how many users there are) is a gauge: the latest
values set_snapshot() published, reported again at every export.

Without an endpoint nothing is configured and recording is a no-op. The provider
is private to this module: livekit.agents.telemetry installs the global one for
LiveKit Cloud (delta temporality) and turns its own metrics off when another
global exists, so ours must neither block it nor leak into it.
"""

from __future__ import annotations

import functools
import logging
import math
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import AlwaysOffExemplarFilter, Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExponentialBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.util.re import parse_env_headers

from interview_agent.config import Settings

logger = logging.getLogger(__name__)

# One export attempt, retries included; the exporter's own default is 10 s.
_EXPORT_TIMEOUT_SECONDS = 3

DIMENSIONS = frozenset(
    {
        "graph_version",
        "config_version",
        "language",
        "seniority",
        "length",
        "model",
        "resolved_model",
        "reasoning_effort",
        "price_version",
        "farewell_status",
        "confirmation_source",
        "stt_model",
        "tts_model",
        "provider",
        "status_code",
        "error_type",
        "source",
        "action",
        "audio_output",
        # Accounts and sign-ins: fixed categories, never who.
        "role",
        "limit",
        "window",
        "reason",
        "result",
        "auth_provider",
    }
)


def safe_dimensions(dimensions: dict) -> dict:
    """Short categorical values only."""
    return {
        key: value
        for key, value in dimensions.items()
        if key in DIMENSIONS and isinstance(value, (str, int, bool)) and len(str(value)) <= 128
    }


# Each gauge's latest values, by metric name: one (attributes, value) pair per
# label set. set_snapshot replaces a gauge's tuple whole, so a collection reads
# either the old values or the new ones, never a mix. Kept across configure()
# and shutdown(): the values describe the database, not the provider.
_snapshots: dict[str, tuple[tuple[dict, float], ...]] = {}


def _observe(metric: str, options: CallbackOptions) -> Iterable[Observation]:
    return [Observation(value, attributes) for attributes, value in _snapshots.get(metric, ())]


class _Process:
    """This process's provider and its instruments, created once per name."""

    def __init__(self, provider: MeterProvider) -> None:
        meter = provider.get_meter("interview_agent")
        self.provider = provider
        self.histogram = functools.cache(meter.create_histogram)
        self.counter = functools.cache(meter.create_counter)
        # Asynchronous: a synchronous gauge exports a value once, after which
        # the series would go stale until the next change.
        self.gauge = functools.cache(
            lambda metric: meter.create_observable_gauge(
                metric, callbacks=[functools.partial(_observe, metric)]
            )
        )


_process: _Process | None = None


def configure(settings: Settings, service_name: str, *, reader: MetricReader | None = None) -> bool:
    """Start recording for this process, replacing any earlier configuration.
    Returns False (recording stays a no-op) without a reader or an endpoint.

    The endpoint and headers come from Settings: .env values never reach
    os.environ, where the exporter would otherwise look for them."""
    global _process
    shutdown()
    endpoint = settings.otel_exporter_otlp_endpoint
    if reader is None:
        if not endpoint:
            return False
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(
                endpoint=endpoint.rstrip("/") + "/v1/metrics",
                # The OTel env format: comma-separated, URL-encoded key=value.
                headers=dict(parse_env_headers(settings.otel_exporter_otlp_headers, liberal=True)),
                timeout=_EXPORT_TIMEOUT_SECONDS,
            )
        )
    provider = MeterProvider(
        metric_readers=[reader],
        resource=Resource.create(
            {"service.name": service_name, "service.instance.id": str(uuid.uuid4())}
        ),
        # Exemplars would attach the current span's trace and span IDs.
        exemplar_filter=AlwaysOffExemplarFilter(),
        # Native histograms in Prometheus, so Grafana can compute percentiles;
        # test readers see the same aggregation as the exporter.
        views=[
            View(instrument_type=Histogram, aggregation=ExponentialBucketHistogramAggregation())
        ],
        # Shutdown and flushes are explicit and bounded; an atexit export would
        # hold the exit once more for an unresponsive collector.
        shutdown_on_exit=False,
    )
    _process = _Process(provider)
    # Snapshots published before this configuration keep being reported.
    for metric in list(_snapshots):
        _process.gauge(metric)
    return True


def record(component: str, name: str, value: float | None, dimensions: dict | None = None) -> None:
    """One sample; cheap and never raises."""
    process = _process
    if process is None:
        return
    try:
        metric = f"interview_agent.{component}.{name}"
        attributes = safe_dimensions(dimensions or {})
        if value is None or not math.isfinite(value):
            process.counter(metric + ".unknown").add(1, attributes)
        else:
            process.histogram(metric).record(value, attributes)
    except Exception as exc:
        logger.warning("Metric not recorded: %s", type(exc).__name__)


def set_snapshot(
    component: str, name: str, values: Mapping[tuple[tuple[str, str], ...], float]
) -> None:
    """Replace what the gauge `interview_agent.<component>.<name>` reports, by
    label set: {(("role", "guest"),): 3, (("role", "admin"),): 1}, or {(): 5}
    without labels. A label set left out is no longer reported, so callers
    list every one, zeros included. Labels outside DIMENSIONS are dropped.
    Kept while recording is unconfigured; never raises."""
    try:
        metric = f"interview_agent.{component}.{name}"
        _snapshots[metric] = tuple(
            (safe_dimensions(dict(labels)), value)
            for labels, value in values.items()
            if math.isfinite(value)
        )
        process = _process
        if process is not None:
            process.gauge(metric)
    except Exception as exc:
        logger.warning("Metric not recorded: %s", type(exc).__name__)


def _bounded(call: Callable[[], object], timeout_millis: float) -> bool:
    """Runs `call` on a daemon thread and waits at most `timeout_millis`. The
    exporter ignores the SDK's timeouts, and a thread still exporting must not
    hold the process's exit (the caller's thread, e.g. asyncio.to_thread, would)."""
    done = threading.Event()
    succeeded = False

    def run() -> None:
        nonlocal succeeded
        try:
            succeeded = call() is not False
        except Exception as exc:
            logger.warning("Metrics export failed: %s", type(exc).__name__)
        finally:
            done.set()

    threading.Thread(target=run, name="interview-agent-metrics-export", daemon=True).start()
    if not done.wait(timeout_millis / 1000):
        logger.warning("Metrics export did not complete")
        return False
    return succeeded


def force_flush(timeout_millis: float = 5_000) -> bool:
    """Exports what was recorded so far, waiting at most `timeout_millis`.
    Blocking: async callers run it in a thread. Never raises; nothing to export
    counts as success."""
    process = _process
    if process is None:
        return True
    return _bounded(lambda: process.provider.force_flush(timeout_millis), timeout_millis)


def shutdown(timeout_millis: float = 5_000) -> None:
    """A last export, waiting at most `timeout_millis`, then recording is a
    no-op until configure(). Never raises."""
    global _process
    process, _process = _process, None
    if process is None:
        return
    _bounded(lambda: process.provider.shutdown(timeout_millis), timeout_millis)
