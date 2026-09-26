package main

import (
	"context"
	"os"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// initTelemetry wires the official OpenTelemetry Go SDK to sentinel-pod's
// OTLP/HTTP endpoint. Config via env, so no secret is committed:
//
//	SELFHEAL_OTLP_ENDPOINT (default http://localhost:8002)
//	SELFHEAL_INGEST_TOKEN  (this app's ingest token from monitored_apps)
func initTelemetry(ctx context.Context) (func(context.Context) error, error) {
	endpoint := os.Getenv("SELFHEAL_OTLP_ENDPOINT")
	if endpoint == "" {
		endpoint = "http://localhost:8002"
	}
	opts := []otlptracehttp.Option{otlptracehttp.WithEndpointURL(endpoint + "/v1/traces")}
	if token := os.Getenv("SELFHEAL_INGEST_TOKEN"); token != "" {
		opts = append(opts, otlptracehttp.WithHeaders(map[string]string{"Authorization": "Bearer " + token}))
	}
	exp, err := otlptracehttp.New(ctx, opts...)
	if err != nil {
		return nil, err
	}
	res := resource.NewSchemaless(attribute.String("service.name", "go_app"))
	// Synchronous processor: an error span is exported immediately.
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exp), sdktrace.WithResource(res))
	otel.SetTracerProvider(tp)
	return tp.Shutdown, nil
}

// recordPanic reports a recovered panic as an OTel `exception` span event.
// Go errors carry no stack of their own, so the goroutine stack is attached
// explicitly as exception.stacktrace.
func recordPanic(ctx context.Context, recovered any) {
	_, span := otel.Tracer("go_app").Start(ctx, "unhandled-panic")
	defer span.End()
	span.AddEvent("exception", traceEventAttrs(recovered))
}
