"""Unit tests for the X-Trace-Id derivation logic.

We exercise the static method ``_derived_trace_id_hex`` (and its
``derive_trace_id_from_uuid`` helper) directly rather than booting a full
ASGI app — that's where the only non-trivial branching lives. The
send-wrapper side (X-Trace-Id / request-id response headers) is covered by
the integration tests against the API routes.
"""

from __future__ import annotations

from src.trace_id_middleware import TraceIdHeaderMiddleware, derive_trace_id_from_uuid


def test_no_x_trace_id_derives_nothing():
    headers = [(b"content-type", b"application/json")]
    assert TraceIdHeaderMiddleware._derived_trace_id_hex(headers) == ""


def test_empty_x_trace_id_derives_nothing():
    headers = [(b"x-trace-id", b"")]
    assert TraceIdHeaderMiddleware._derived_trace_id_hex(headers) == ""


def test_x_trace_id_derives_32_hex():
    headers = [(b"x-trace-id", b"9e108e0e-3a5b-4d8c-9f10-1234567890ab")]
    tid = TraceIdHeaderMiddleware._derived_trace_id_hex(headers)
    assert len(tid) == 32
    int(tid, 16)  # valid hex


def test_x_trace_id_is_deterministic():
    # Two requests with the same client trace ID must derive the same
    # trace ID.
    headers = [(b"x-trace-id", b"session-42")]
    tid_a = TraceIdHeaderMiddleware._derived_trace_id_hex(list(headers))
    tid_b = TraceIdHeaderMiddleware._derived_trace_id_hex(list(headers))
    assert tid_a == tid_b != ""


def test_distinct_inputs_derive_distinct_ids():
    tid_a = TraceIdHeaderMiddleware._derived_trace_id_hex([(b"x-trace-id", b"session-1")])
    tid_b = TraceIdHeaderMiddleware._derived_trace_id_hex([(b"x-trace-id", b"session-2")])
    assert tid_a != tid_b


def test_plain_32_hex_is_used_as_is():
    raw = "aabbccddeeff0011aabbccddeeff0011"
    assert derive_trace_id_from_uuid(raw) == int(raw, 16)


def test_uuid_input_is_hashed_not_taken_verbatim():
    # A UUID's hex is hashed so it can't collide with a raw trace ID that
    # shares the same 32-char form.
    uuid_str = "9e108e0e-3a5b-4d8c-9f10-1234567890ab"
    derived = derive_trace_id_from_uuid(uuid_str)
    assert derived is not None
    assert derived != int(uuid_str.replace("-", ""), 16)


def test_opaque_string_still_correlates():
    a = derive_trace_id_from_uuid("my-session")
    b = derive_trace_id_from_uuid("my-session")
    assert a == b is not None


def test_empty_and_zero_inputs_are_none():
    assert derive_trace_id_from_uuid("") is None
    assert derive_trace_id_from_uuid("0" * 32) is None
