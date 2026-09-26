"""OTLP ingest: per-language stack-trace parsing, JSON + protobuf bodies, auth."""

from __future__ import annotations

import gzip
import uuid

import pytest
from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
from sqlalchemy import select

from core.models import Error, MonitoredApp
from sentinel import otlp
from sentinel.stacktrace import parse_stacktrace, select_in_app_frame

BT = chr(96)  # backtick, kept out of the literals below

# sdk language -> (our language, stacktrace, expected file, expected line)
SAMPLES = {
    "python": (
        "python",
        'Traceback (most recent call last):\n  File "/srv/app/main.py", line 10, in handler\n'
        '    run()\n  File "/srv/venv/lib/python3.11/site-packages/lib/x.py", line 3, in run\n'
        "    1/0\nZeroDivisionError: division by zero",
        "/srv/app/main.py",
        10,
    ),
    "nodejs": (
        "javascript",
        "TypeError: x\n    at divide (/app/src/math.js:12:9)\n"
        "    at Layer.handle (/app/node_modules/express/lib/router/layer.js:95:5)\n",
        "/app/src/math.js",
        12,
    ),
    "java": (
        "java",
        "java.lang.IllegalStateException: boom\n"
        "\tat java.base/java.util.Objects.requireNonNull(Objects.java:233)\n"
        "\tat com.acme.shop.Cart.total(Cart.java:42)\n",
        "com/acme/shop/Cart.java",
        42,
    ),
    "go": (
        "go",
        "goroutine 1 [running]:\nruntime/debug.Stack()\n"
        "\t/usr/local/go/src/runtime/debug/stack.go:24 +0x5e\n"
        "main.divide(0x1, 0x0)\n\t/app/main.go:12 +0x1d\n",
        "/app/main.go",
        12,
    ),
    "dotnet": (
        "csharp",
        "System.InvalidOperationException: boom\n"
        f"   at System.Linq.Enumerable.First[T](IEnumerable{BT}1 s)\n"
        "   at Shop.Cart.Total(Int32 n) in C:\\src\\Shop\\Cart.cs:line 27\n",
        "C:/src/Shop/Cart.cs",
        27,
    ),
    "php": (
        "php",
        "Exception: boom in /var/www/app/src/Cart.php:9\nStack trace:\n"
        "#0 /var/www/app/vendor/laravel/x.php(5): Foo->bar()\n"
        "#1 /var/www/app/src/Cart.php(9): Cart->total()\n#2 {main}",
        "/var/www/app/src/Cart.php",
        9,
    ),
    "ruby": (
        "ruby",
        f"cart.rb:7:in {BT}total': divided by 0 (ZeroDivisionError)\n"
        f"\tfrom /usr/lib/ruby/gems/3.2/gems/rack-3/lib/rack.rb:10:in {BT}call'\n",
        "cart.rb",
        7,
    ),
}


@pytest.mark.parametrize("sdk", list(SAMPLES))
def test_top_in_app_frame_per_language(sdk: str) -> None:
    lang, trace, path, line = SAMPLES[sdk]
    parsed_lang, frames = parse_stacktrace(trace, None)  # auto-detect
    assert parsed_lang == lang
    frame = select_in_app_frame(lang, frames)
    assert frame is not None
    assert frame.file.replace("\\", "/") == path
    assert frame.line == line


def _sattr(key: str, value: str) -> dict:
    return {"key": key, "value": {"stringValue": value}}


def _trace_payload(sdk: str, trace: str) -> dict:
    event = {
        "name": "exception",
        "timeUnixNano": "1700000000000000000",
        "attributes": [
            _sattr("exception.type", "Boom"),
            _sattr("exception.message", "bad"),
            _sattr("exception.stacktrace", trace),
        ],
    }
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        _sattr("service.name", "svc"),
                        _sattr("telemetry.sdk.language", sdk),
                    ]
                },
                "scopeSpans": [{"spans": [{"name": "GET /x", "events": [event, {"name": "x"}]}]}],
            }
        ]
    }


@pytest.mark.parametrize("sdk", list(SAMPLES))
def test_extract_errors_from_json_spans(sdk: str) -> None:
    _, trace, path, line = SAMPLES[sdk]
    [err] = otlp.extract_errors("traces", _trace_payload(sdk, trace))
    assert err.file_path == path
    assert err.line_number == line
    assert err.request_context["service.name"] == "svc"


def test_code_attributes_are_fallback_when_no_stacktrace() -> None:
    payload = _trace_payload("go", "")
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    span["attributes"] = [
        _sattr("code.filepath", "/w/examples/go_app/main.go"),
        {"key": "code.lineno", "value": {"intValue": "31"}},
    ]
    [err] = otlp.extract_errors("traces", payload, in_app_hint="examples/go_app")
    assert (err.file_path, err.line_number) == ("examples/go_app/main.go", 31)


def test_log_record_with_exception_attributes() -> None:
    records = [
        {
            "attributes": [
                _sattr("exception.type", "E"),
                _sattr("exception.stacktrace", SAMPLES["ruby"][1]),
            ]
        },
        {"attributes": [_sattr("other", "x")]},
    ]
    payload = {"resourceLogs": [{"resource": {}, "scopeLogs": [{"logRecords": records}]}]}
    errs = otlp.extract_errors("logs", payload)
    assert len(errs) == 1 and errs[0].line_number == 7


async def _make_app(db_session, hint: str = "examples/node_app") -> MonitoredApp:
    app = MonitoredApp(
        name=f"otlp-{uuid.uuid4().hex[:8]}",
        language="javascript",
        local_repo_path=hint,
        github_repo="o/r",
        allowed_write_paths=[hint + "/"],
        test_command="npm test",
        ingest_token=uuid.uuid4().hex,
    )
    db_session.add(app)
    await db_session.flush()
    return app


async def test_otlp_requires_token(sentinel_http_client) -> None:
    resp = await sentinel_http_client.post("/v1/traces", json={})
    assert resp.status_code == 401


async def test_otlp_json_stores_error_for_app(sentinel_http_client, db_session) -> None:
    app = await _make_app(db_session)
    fn = f"boom{uuid.uuid4().hex[:6]}"
    trace = f"Error: x\n    at {fn} (/w/examples/node_app/src/a.js:5:3)\n"
    resp = await sentinel_http_client.post(
        "/v1/traces",
        json=_trace_payload("nodejs", trace),
        headers={"Authorization": f"Bearer {app.ingest_token}"},
    )
    assert resp.status_code == 200
    err = (await db_session.execute(select(Error).where(Error.function_name == fn))).scalar_one()
    assert (err.file_path, err.line_number) == ("examples/node_app/src/a.js", 5)
    assert err.app_id == app.id


async def test_otlp_protobuf_gzip(sentinel_http_client, db_session) -> None:
    app = await _make_app(db_session, "examples/go_app")
    fn = f"main.f{uuid.uuid4().hex[:6]}"
    trace = f"goroutine 1 [running]:\n{fn}()\n\t/w/examples/go_app/main.go:12 +0x1d\n"
    msg = json_format.ParseDict(
        _trace_payload("go", trace), trace_service_pb2.ExportTraceServiceRequest()
    )
    resp = await sentinel_http_client.post(
        "/v1/traces",
        content=gzip.compress(msg.SerializeToString()),
        headers={
            "Authorization": f"Bearer {app.ingest_token}",
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "gzip",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-protobuf"
    err = (await db_session.execute(select(Error).where(Error.function_name == fn))).scalar_one()
    assert (err.file_path, err.line_number) == ("examples/go_app/main.go", 12)


async def test_otlp_bad_body_is_400(sentinel_http_client, db_session) -> None:
    app = await _make_app(db_session)
    resp = await sentinel_http_client.post(
        "/v1/logs",
        content=b"\xff\xfe not proto",
        headers={
            "Authorization": f"Bearer {app.ingest_token}",
            "Content-Type": "application/x-protobuf",
        },
    )
    assert resp.status_code == 400


def test_go_recovered_panic_skips_the_recovery_machinery() -> None:
    trace = (
        "goroutine 6 [running]:\nruntime/debug.Stack()\n"
        "\t/usr/local/go/src/runtime/debug/stack.go:26 +0x5e\n"
        "main.recoverMiddleware.func1.1()\n\t/app/main.go:35 +0x10\n"
        "panic({0x1, 0x2})\n\t/usr/local/go/src/runtime/panic.go:783 +0x132\n"
        "runtime.panicdivide(...)\n\t/usr/local/go/src/runtime/panic.go:100\n"
        "main.average(...)\n\t/app/calc.go:11\n"
    )
    lang, frames = parse_stacktrace(trace, "go")
    frame = select_in_app_frame(lang or "go", frames)
    assert frame is not None
    assert (frame.file, frame.line) == ("/app/calc.go", 11)
