const express = require("express");
const { trace } = require("@opentelemetry/api");
const { displayName } = require("./users");

function createApp() {
  const app = express();

  app.get("/healthz", (_req, res) => res.json({ status: "ok" }));

  app.get("/users/:id/display-name", (req, res) => {
    res.json({ name: displayName(Number(req.params.id)) });
  });

  // Error middleware: record the exception (with its stack) on a span so the
  // OTel SDK exports it as an `exception` span event.
  // eslint-disable-next-line no-unused-vars
  app.use((err, _req, res, _next) => {
    const span = trace.getTracer("node_app").startSpan("unhandled-error");
    span.recordException(err);
    span.end();
    res.status(500).json({ error: err.message });
  });

  return app;
}

module.exports = { createApp };
