"""Native LiveKit records keep their code location and never their message."""

import logging
import pickle
import socket

import pytest
from livekit.agents.ipc.log_queue import LogQueueHandler
from livekit.agents.utils.aio import duplex_unix

from interview_agent.logging_config import (
    FfiLocationFilter,
    MetadataFormatter,
    install_ffi_location_filter,
)

FFI_TEMPLATE = "%s:%s:%s - %s"
NATIVE_MESSAGE = "failed to send: room interview-abc participant candidate"


def ffi_record(target, line, module_path, message=NATIVE_MESSAGE, name="livekit"):
    # Built the way livekit/rtc/_ffi_client.py forwards a native log record.
    return logging.LogRecord(
        name,
        logging.ERROR,
        "/site-packages/livekit/rtc/_ffi_client.py",
        187,
        FFI_TEMPLATE,
        (target, line, module_path, message),
        None,
    )


def test_native_record_shows_location_and_omits_message():
    item = ffi_record("livekit_ffi::server::data_stream", 123, "livekit_ffi::server")
    output = MetadataFormatter().format(item)
    assert "ffi_target=livekit_ffi::server::data_stream ffi_line=123" in output
    assert "ffi_module=livekit_ffi::server" in output
    assert "source_line=187" in output
    assert "content omitted" in output
    for leaked in ("interview-abc", "candidate", "failed", "room"):
        assert leaked not in output
    assert item.args[3] == NATIVE_MESSAGE  # The record is not mutated.


@pytest.mark.parametrize(
    "target",
    [
        "livekit_ffi server",
        "candidate@example.com",
        "https://example.livekit.cloud/rtc",
        "wss://interview-abc.livekit.cloud",
        "livekit_ffi::" + "a" * 120,
        "host:7880",
        "interview-3f2a9c1e-77b4-4d0e-9a51-0c6b2f8e1d23",
        "room/1234567",
        "",
        None,
        123,
    ],
)
def test_unsafe_target_yields_no_target_field(target):
    output = MetadataFormatter().format(ffi_record(target, 123, "livekit_ffi::server"))
    assert "ffi_target=" not in output
    if isinstance(target, str) and target:
        assert target not in output


@pytest.mark.parametrize(
    "module_path",
    ["livekit_ffi::server room", "user@host", "https://x.y", "a" * 121, "src/server.rs", 7],
)
def test_unsafe_module_path_yields_no_module_field(module_path):
    output = MetadataFormatter().format(ffi_record("libwebrtc", 9, module_path))
    assert "ffi_target=libwebrtc ffi_line=9" in output
    assert "ffi_module=" not in output


@pytest.mark.parametrize("line", ["123", 1.5, True, -1, 0, 10**9, None])
def test_non_integer_or_out_of_range_line_is_dropped(line):
    output = MetadataFormatter().format(ffi_record("libwebrtc", line, "livekit_ffi::server"))
    assert "ffi_line=" not in output
    assert "ffi_target=libwebrtc" in output


def test_fully_invalid_native_record_has_no_ffi_fields():
    item = ffi_record("a b@c", "x", "https://host", "secret")
    output = MetadataFormatter().format(item)
    assert "ffi_" not in output and "secret" not in output


@pytest.mark.parametrize("name", ["livekit.agents", "livekit.rtc", "interview_agent.worker", "x"])
def test_other_loggers_with_the_same_template_get_no_ffi_fields(name):
    item = ffi_record("livekit_ffi::server", 123, "livekit_ffi::server", name=name)
    output = MetadataFormatter().format(item)
    assert "ffi_" not in output
    assert "interview-abc" not in output


@pytest.mark.parametrize(
    ("msg", "args"),
    [
        ("%s:%s - %s", ("livekit_ffi::server", 123, NATIVE_MESSAGE)),
        (FFI_TEMPLATE, ("livekit_ffi::server", 123, "livekit_ffi::server")),
        (FFI_TEMPLATE, ["livekit_ffi::server", 123, "livekit_ffi::server", NATIVE_MESSAGE]),
        (FFI_TEMPLATE, {"target": "livekit_ffi::server"}),
    ],
)
def test_other_livekit_records_are_formatted_as_before(msg, args):
    item = logging.LogRecord("livekit", logging.ERROR, "/x.py", 42, msg, (), None)
    item.args = args
    output = MetadataFormatter().format(item)
    assert "ffi_" not in output
    assert output.endswith("| source_line=42")


def test_filter_is_installed_on_the_livekit_logger_once():
    install_ffi_location_filter()
    install_ffi_location_filter()
    filters = logging.getLogger("livekit").filters
    assert sum(isinstance(f, FfiLocationFilter) for f in filters) == 1


def test_filter_keeps_location_and_replaces_message():
    item = ffi_record("livekit_ffi::server::data_stream", 123, "a b@c")
    assert FfiLocationFilter().filter(item) is True
    assert item.args is None and NATIVE_MESSAGE not in item.getMessage()
    assert (item.ffi_target, item.ffi_line) == ("livekit_ffi::server::data_stream", 123)
    assert not hasattr(item, "ffi_module")  # Failed validation: no attribute.
    output = MetadataFormatter().format(item)
    assert output.endswith(
        "| source_line=187 ffi_target=livekit_ffi::server::data_stream ffi_line=123"
    )


def test_attributes_are_validated_again_by_the_formatter():
    item = logging.LogRecord("livekit", logging.ERROR, "/x.py", 1, "expanded", None, None)
    item.ffi_target = "wss://interview-abc.livekit.cloud"
    item.ffi_line = "123"
    item.ffi_module = "livekit_ffi::server"
    output = MetadataFormatter().format(item)
    assert "ffi_target=" not in output and "ffi_line=" not in output
    assert "interview-abc" not in output
    assert "ffi_module=livekit_ffi::server" in output


def test_attributes_on_other_loggers_are_ignored():
    item = logging.LogRecord("livekit.agents", logging.ERROR, "/x.py", 1, "m", None, None)
    item.ffi_target = "livekit_ffi::server"
    assert "ffi_" not in MetadataFormatter().format(item)


@pytest.fixture
def job_process_log_channel():
    """The worker/job log channel of livekit-agents: a LogQueueHandler on the
    job side of a socket pair, as job_proc_lazy_main.proc_main installs it."""
    job_sock, worker_sock = socket.socketpair()
    handler = LogQueueHandler(duplex_unix._Duplex.open(job_sock))
    worker = duplex_unix._Duplex.open(worker_sock)
    livekit_logger = logging.getLogger("livekit")
    saved = (livekit_logger.level, livekit_logger.propagate)
    livekit_logger.addHandler(handler)
    livekit_logger.setLevel(logging.DEBUG)
    livekit_logger.propagate = False
    try:
        yield worker
    finally:
        livekit_logger.removeHandler(handler)
        livekit_logger.level, livekit_logger.propagate = saved
        handler.close()
        handler.thread.join(timeout=5)
        worker.close()


def test_location_survives_the_job_process_log_channel(job_process_log_channel):
    # Logged exactly as livekit/rtc/_ffi_client.py does, through the real
    # "livekit" logger, so its installed filter runs before the queue handler.
    logging.getLogger("livekit").log(
        logging.ERROR,
        FFI_TEMPLATE,
        "livekit_ffi::server::data_stream",
        123,
        "livekit_ffi::server",
        NATIVE_MESSAGE,
    )
    data = job_process_log_channel.recv_bytes()
    # The native message never crosses the socket to the worker process.
    for leaked in (b"interview-abc", b"candidate", b"failed"):
        assert leaked not in data
    received = pickle.loads(data)
    assert received.args is None  # Expanded by the queue handler.
    output = MetadataFormatter().format(received)
    assert "ffi_target=livekit_ffi::server::data_stream ffi_line=123" in output
    assert "ffi_module=livekit_ffi::server" in output
    for leaked in ("interview-abc", "candidate", "failed"):
        assert leaked not in output
