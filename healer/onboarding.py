"""One-click "auto-onboarding PR": adds error capture to a connected app's repo.

Deliberately not an AI-agent task (no LLM call, no worktree fix-loop): the
integration is a fixed template picked by the app's detected language, so
this is fast, free, and 100% deterministic. Python gets a small
dependency-free helper wired to `/ingest/error`; every other language gets
the *official OpenTelemetry SDK* config pointing OTLP/HTTP at sentinel-pod
(`/v1/traces`) with the app's ingest token. Reliably wiring a *specific*
framework's exception hook for an unknown repo is not something a template
can guarantee, so the PR body says what to call instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import settings
from core.models import MonitoredApp
from mcp_server.github_client import GitHubClient

ONBOARDING_BRANCH_PREFIX = "selfheal-onboarding"


def _base_url() -> str:
    return (settings.public_url or "http://localhost:8002").rstrip("/")


def _ingest_url(app: MonitoredApp) -> str:
    return f"{_base_url()}/ingest/error"


_PYTHON_SNIPPET = '''"""Error reporting for the AI self-healing system.

Call `report_error(exc)` from an except block (or a framework-level error
handler) to send an unhandled exception here for automatic detection and
AI-assisted fixing. Never raises itself -- a reporting failure must never
break the app it's monitoring.
"""

from __future__ import annotations

import json
import traceback
import urllib.request

INGEST_URL = "{ingest_url}"
INGEST_TOKEN = "{ingest_token}"


def report_error(exc: BaseException) -> None:
    try:
        payload = {{
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "traceback": "".join(traceback.format_exception(exc)),
        }}
        request = urllib.request.Request(
            INGEST_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={{
                "Content-Type": "application/json",
                "Authorization": f"Bearer {{INGEST_TOKEN}}",
            }},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=2)
    except Exception:
        pass
'''

# Placeholders are __ENDPOINT__, __TOKEN__, __SERVICE__ (not str.format:
# these files are full of braces).
_OTEL_TEMPLATES: dict[str, tuple[str, str]] = {
    "javascript": (
        "selfheal_otel.js",
        """// OpenTelemetry setup for the AI self-healing system.
// Load first:  node -r ./selfheal_otel.js app.js
// npm i @opentelemetry/sdk-node @opentelemetry/exporter-trace-otlp-http @opentelemetry/api
const { NodeSDK } = require("@opentelemetry/sdk-node");
const { OTLPTraceExporter } = require("@opentelemetry/exporter-trace-otlp-http");
const { trace } = require("@opentelemetry/api");

const sdk = new NodeSDK({
  serviceName: "__SERVICE__",
  traceExporter: new OTLPTraceExporter({
    url: "__ENDPOINT__/v1/traces",
    headers: { Authorization: "Bearer __TOKEN__" },
  }),
});
sdk.start();

// Call from your error-handling middleware / catch blocks.
function recordError(err) {
  const span = trace.getTracer("selfheal").startSpan("unhandled-error");
  span.recordException(err);
  span.end();
}
module.exports = { recordError };
""",
    ),
    "go": (
        "selfheal_otel.go",
        """package main

// OpenTelemetry setup for the AI self-healing system.
// go get go.opentelemetry.io/otel go.opentelemetry.io/otel/sdk \\
//   go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp

import (
	"context"
	"runtime/debug"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// InitTelemetry wires the exporter; defer the returned func in main().
func InitTelemetry(ctx context.Context) (func(context.Context) error, error) {
	exp, err := otlptracehttp.New(ctx,
		otlptracehttp.WithEndpointURL("__ENDPOINT__/v1/traces"),
		otlptracehttp.WithHeaders(map[string]string{"Authorization": "Bearer __TOKEN__"}))
	if err != nil {
		return nil, err
	}
	res := resource.NewSchemaless(attribute.String("service.name", "__SERVICE__"))
	tp := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exp), sdktrace.WithResource(res))
	otel.SetTracerProvider(tp)
	return tp.Shutdown, nil
}

// RecordError reports err with its stack (Go errors carry none by default).
func RecordError(ctx context.Context, err error) {
	_, span := otel.Tracer("selfheal").Start(ctx, "unhandled-error")
	span.RecordError(err, trace.WithAttributes(
		attribute.String("exception.stacktrace", string(debug.Stack()))))
	span.End()
}
""",
    ),
    "java": (
        "selfheal-otel.properties",
        """# OpenTelemetry Java agent config for the AI self-healing system.
# Run:  java -javaagent:opentelemetry-javaagent.jar \\
#            -Dotel.javaagent.configuration-file=selfheal-otel.properties -jar app.jar
otel.service.name=__SERVICE__
otel.exporter.otlp.protocol=http/protobuf
otel.exporter.otlp.endpoint=__ENDPOINT__
otel.exporter.otlp.headers=Authorization=Bearer __TOKEN__
otel.metrics.exporter=none
""",
    ),
    "csharp": (
        "SelfHealOtel.cs",
        """// OpenTelemetry setup for the AI self-healing system.
// dotnet add package OpenTelemetry.Extensions.Hosting
// dotnet add package OpenTelemetry.Exporter.OpenTelemetryProtocol
// dotnet add package OpenTelemetry.Instrumentation.AspNetCore
using OpenTelemetry.Exporter;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

public static class SelfHealOtel
{
    // builder.Services.AddOpenTelemetry().WithTracing(SelfHealOtel.Configure);
    public static void Configure(TracerProviderBuilder b) => b
        .ConfigureResource(r => r.AddService("__SERVICE__"))
        .AddAspNetCoreInstrumentation(o => o.RecordException = true)
        .AddOtlpExporter(o =>
        {
            o.Endpoint = new System.Uri("__ENDPOINT__/v1/traces");
            o.Protocol = OtlpExportProtocol.HttpProtobuf;
            o.Headers = "Authorization=Bearer __TOKEN__";
        });
}
""",
    ),
    "php": (
        "selfheal_otel.php",
        """<?php
// OpenTelemetry setup for the AI self-healing system.
// composer require open-telemetry/sdk open-telemetry/exporter-otlp
// Require this file first, or export the same values as env vars.
putenv('OTEL_SERVICE_NAME=__SERVICE__');
putenv('OTEL_TRACES_EXPORTER=otlp');
putenv('OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf');
putenv('OTEL_EXPORTER_OTLP_ENDPOINT=__ENDPOINT__');
putenv('OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer __TOKEN__');
putenv('OTEL_PHP_AUTOLOAD_ENABLED=true');
""",
    ),
    "ruby": (
        "selfheal_otel.rb",
        """# OpenTelemetry setup for the AI self-healing system.
# bundle add opentelemetry-sdk opentelemetry-exporter-otlp opentelemetry-instrumentation-all
# require_relative "selfheal_otel" before your app boots.
require "opentelemetry/sdk"
require "opentelemetry/exporter/otlp"
require "opentelemetry/instrumentation/all"

ENV["OTEL_EXPORTER_OTLP_ENDPOINT"] = "__ENDPOINT__"
ENV["OTEL_EXPORTER_OTLP_HEADERS"] = "Authorization=Bearer __TOKEN__"
OpenTelemetry::SDK.configure do |c|
  c.service_name = "__SERVICE__"
  c.use_all
end
""",
    ),
}


@dataclass(frozen=True)
class OnboardingFile:
    path: str
    content: str


def build_onboarding_file(app: MonitoredApp) -> OnboardingFile:
    if app.language in _OTEL_TEMPLATES:
        path, template = _OTEL_TEMPLATES[app.language]
        content = (
            template.replace("__ENDPOINT__", _base_url())
            .replace("__TOKEN__", app.ingest_token)
            .replace("__SERVICE__", app.name)
        )
        return OnboardingFile(path=path, content=content)
    # Python (and "unknown") keeps the dependency-free urllib middleware snippet.
    content = _PYTHON_SNIPPET.format(ingest_url=_ingest_url(app), ingest_token=app.ingest_token)
    return OnboardingFile(path="selfheal_error_reporter.py", content=content)


def build_onboarding_pr_body(app: MonitoredApp, onboarding_file: OnboardingFile) -> str:
    footer = '_Opened automatically by the AI self-healing system\'s "connect a repo" flow._'
    if app.language in _OTEL_TEMPLATES:
        return (
            f"Adds `{onboarding_file.path}`, the official OpenTelemetry SDK setup for "
            f"{app.language}, exporting traces over OTLP/HTTP to the AI self-healing "
            "system with this app's ingest token.\n\n"
            "## Next step (not done automatically)\n"
            "Load/initialise it at app start (see the comments in the file) and make sure "
            "unhandled exceptions are recorded on a span (`recordException` / "
            "`RecordError`; the Java agent and ASP.NET Core instrumentation do this "
            f"automatically) so they reach the system.\n\n{footer}"
        )
    return (
        f"Adds `{onboarding_file.path}`, a small dependency-free helper that reports an "
        "unhandled exception to the AI self-healing system for automatic detection and "
        "AI-assisted fixing.\n\n"
        "## Next step (not done automatically)\n"
        "Call `report_error(exc)` from this app's top-level exception handler / error "
        "middleware (e.g. a `try`/`except` around the request handler, or your "
        f"framework's error hook) so runtime errors reach it.\n\n{footer}"
    )


async def open_onboarding_pull_request(app: MonitoredApp) -> dict[str, object]:
    """Commit `build_onboarding_file(app)` to a new branch and open a PR.

    Uses the GitHub Contents API directly (no worktree/clone needed for a
    single-file commit), against `app.github_repo`.
    """
    onboarding_file = build_onboarding_file(app)
    branch = f"{ONBOARDING_BRANCH_PREFIX}/{app.name}"

    async with GitHubClient(repo=app.github_repo) as client:
        repo_info = await client.get_repo()
        base_branch = str(repo_info.get("default_branch") or "main")
        base_ref = await client.get_branch_sha(base_branch)
        await client.create_branch(branch, from_sha=base_ref)
        await client.create_or_update_file(
            branch=branch,
            path=onboarding_file.path,
            content=onboarding_file.content,
            message=f"Add self-healing error reporting ({onboarding_file.path})",
        )
        pr = await client.create_pull_request(
            title="Add self-healing error reporting",
            body=build_onboarding_pr_body(app, onboarding_file),
            head=branch,
            base=base_branch,
        )
        return pr
