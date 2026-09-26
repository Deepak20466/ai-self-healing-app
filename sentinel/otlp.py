"""OTLP/HTTP ingest: turn OpenTelemetry exception spans/logs into `CapturedError`s.

Why one JSON code path: protobuf bodies are decoded with `opentelemetry-proto`
and converted with `json_format.MessageToDict`, which yields the same
camelCase shape as OTLP/JSON -- so parsing logic exists exactly once.
Exceptions arrive two ways in the wild: span *events* named `exception`
(what every SDK's `record_exception` emits) and log records carrying
`exception.*` attributes. Both are handled.

Location comes from the stack trace's top in-app frame (see
`sentinel.stacktrace`); `code.filepath`/`code.lineno` are only a fallback
because most SDKs don't set them on exception events.
"""

from __future__ import annotations

import gzip
import zlib
from datetime import UTC, datetime
from typing import Any

from google.protobuf import json_format
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

from sentinel.capture import CapturedError
from sentinel.stacktrace import (
    language_from_sdk,
    parse_stacktrace,
    relativize,
    select_in_app_frame,
)

_MAX_BODY_BYTES = 5 * 1024 * 1024


class OTLPDecodeError(ValueError):
    """Body couldn't be decoded as OTLP (bad gzip/protobuf/JSON shape)."""


def decompress(body: bytes, content_encoding: str | None) -> bytes:
    enc = (content_encoding or "").lower()
    try:
        if enc == "gzip":
            out = gzip.decompress(body)
        elif enc == "deflate":
            out = zlib.decompress(body)
        else:
            out = body
    except (OSError, zlib.error) as exc:
        raise OTLPDecodeError(f"bad {enc} body") from exc
    if len(out) > _MAX_BODY_BYTES:
        raise OTLPDecodeError("body too large")
    return out


def protobuf_to_dict(kind: str, body: bytes) -> dict[str, Any]:
    msg = (
        trace_service_pb2.ExportTraceServiceRequest()
        if kind == "traces"
        else logs_service_pb2.ExportLogsServiceRequest()
    )
    try:
        msg.ParseFromString(body)
    except Exception as exc:  # protobuf raises DecodeError; keep the boundary narrow to one type
        raise OTLPDecodeError("invalid protobuf") from exc
    result: dict[str, Any] = json_format.MessageToDict(msg)
    return result


def empty_protobuf_response(kind: str) -> bytes:
    resp = (
        trace_service_pb2.ExportTraceServiceResponse()
        if kind == "traces"
        else logs_service_pb2.ExportLogsServiceResponse()
    )
    data: bytes = resp.SerializeToString()
    return data


def _attr_value(value: dict[str, Any]) -> Any:
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in value:
            return value[key]
    if "intValue" in value:
        return int(value["intValue"])
    return None


def _attrs(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {a["key"]: _attr_value(a.get("value", {})) for a in items or [] if "key" in a}


def _occurred_at(nanos: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(nanos) / 1e9, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return datetime.now(UTC)


def _build(
    exc_attrs: dict[str, Any],
    extra_attrs: dict[str, Any],
    resource: dict[str, Any],
    nanos: Any,
    in_app_hint: str | None,
) -> CapturedError | None:
    exc_type = exc_attrs.get("exception.type")
    message = exc_attrs.get("exception.message") or ""
    stack = str(exc_attrs.get("exception.stacktrace") or "")
    if not exc_type and not stack and not message:
        return None

    language = language_from_sdk(str(resource.get("telemetry.sdk.language") or ""))
    lang, frames = parse_stacktrace(stack, language)
    file_path, line, function = "<unknown>", 0, "<unknown>"
    frame = select_in_app_frame(lang, frames, in_app_hint) if lang else None
    if frame is not None:
        file_path, line, function = relativize(frame.file, in_app_hint), frame.line, frame.function
    else:
        code_file = extra_attrs.get("code.filepath") or extra_attrs.get("code.file.path")
        if code_file:
            file_path = relativize(str(code_file), in_app_hint)
            line = int(extra_attrs.get("code.lineno") or extra_attrs.get("code.line.number") or 0)
            function = str(
                extra_attrs.get("code.function")
                or extra_attrs.get("code.function.name")
                or "<unknown>"
            )

    context: dict[str, Any] = {"source": "otlp"}
    if resource.get("service.name"):
        context["service.name"] = resource["service.name"]
    if lang:
        context["language"] = lang
    return CapturedError(
        exception_type=str(exc_type or "Error"),
        message=str(message),
        traceback=stack or f"{exc_type}: {message}",
        file_path=file_path,
        line_number=line,
        function_name=function,
        request_context=context,
        git_sha=str(resource.get("vcs.revision") or "") or None,
        occurred_at=_occurred_at(nanos),
    )


def extract_errors(
    kind: str, payload: dict[str, Any], in_app_hint: str | None = None
) -> list[CapturedError]:
    """`kind` is "traces" or "logs". Returns one CapturedError per exception found."""
    out: list[CapturedError] = []
    if kind == "traces":
        for rs in payload.get("resourceSpans", []):
            resource = _attrs(rs.get("resource", {}).get("attributes"))
            for ss in rs.get("scopeSpans", []):
                for span in ss.get("spans", []):
                    span_attrs = _attrs(span.get("attributes"))
                    for event in span.get("events", []):
                        if event.get("name") != "exception":
                            continue
                        attrs = _attrs(event.get("attributes"))
                        err = _build(
                            attrs, {**span_attrs, **attrs}, resource,
                            event.get("timeUnixNano"), in_app_hint,
                        )  # fmt: skip
                        if err:
                            out.append(err)
    else:
        for rl in payload.get("resourceLogs", []):
            resource = _attrs(rl.get("resource", {}).get("attributes"))
            for sl in rl.get("scopeLogs", []):
                for record in sl.get("logRecords", []):
                    attrs = _attrs(record.get("attributes"))
                    if not any(k.startswith("exception.") for k in attrs):
                        continue
                    err = _build(attrs, attrs, resource, record.get("timeUnixNano"), in_app_hint)
                    if err:
                        out.append(err)
    return out
