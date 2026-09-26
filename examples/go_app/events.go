package main

import (
	"fmt"
	"runtime/debug"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

// traceEventAttrs builds the semantic-convention exception.* attributes.
func traceEventAttrs(recovered any) trace.EventOption {
	return trace.WithAttributes(
		attribute.String("exception.type", fmt.Sprintf("%T", recovered)),
		attribute.String("exception.message", fmt.Sprint(recovered)),
		attribute.String("exception.stacktrace", string(debug.Stack())),
	)
}
