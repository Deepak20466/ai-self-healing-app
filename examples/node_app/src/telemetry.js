// OpenTelemetry setup (official Node SDK) -> sentinel-pod's OTLP/HTTP endpoint.
// Loaded first:  node -r ./src/telemetry.js src/server.js
// Config via env so no secret is committed:
//   SELFHEAL_OTLP_ENDPOINT (default http://localhost:8002)
//   SELFHEAL_INGEST_TOKEN  (this app's ingest token from monitored_apps)
const { NodeSDK } = require("@opentelemetry/sdk-node");
const { OTLPTraceExporter } = require("@opentelemetry/exporter-trace-otlp-http");
const { SimpleSpanProcessor } = require("@opentelemetry/sdk-trace-base");

const endpoint = process.env.SELFHEAL_OTLP_ENDPOINT || "http://localhost:8002";
const token = process.env.SELFHEAL_INGEST_TOKEN || "";

const sdk = new NodeSDK({
  serviceName: "node_app",
  // Simple (unbatched) processor: an error span is exported immediately.
  spanProcessors: [
    new SimpleSpanProcessor(
      new OTLPTraceExporter({
        url: `${endpoint}/v1/traces`,
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      })
    ),
  ],
});
sdk.start();

process.on("SIGTERM", () => sdk.shutdown().finally(() => process.exit(0)));
