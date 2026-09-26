# Connect any app in 5 steps

Works for any language that has an official OpenTelemetry SDK. Python apps
can alternatively use the dependency-free helper. See the support table in
the README for what is verified live vs. only unit-tested per language.

1. **Connect the repo.** Dashboard → **Apps** → *Add app* → paste the GitHub
   URL. Needs `GITHUB_TOKEN` with push access (and, optionally,
   `ALLOWED_REPO_OWNERS`). The language, test and lint commands are
   auto-detected from the manifest (`package.json`, `go.mod`, `pom.xml` /
   `build.gradle*`, `*.csproj`, `composer.json`, `Gemfile`,
   `pyproject.toml`/`requirements.txt`).
2. **Read the health report.** The instant scan runs the app's own tests,
   linter and dependency audit. A check whose tool isn't installed on the
   healer host is reported as `skipped: tool not installed (<tool>)`, never
   as a failure of your repo.
3. **Add error capture.** Click **Onboard PR**. Python gets a small helper
   (`selfheal_error_reporter.py`); every other language gets the official
   OpenTelemetry SDK setup (`selfheal_otel.js` / `.go` / `.rb` / `.php`,
   `SelfHealOtel.cs`, `selfheal-otel.properties` for the Java agent) already
   pointing at `<PUBLIC_URL>/v1/traces` with this app's ingest token.
4. **Wire it in and merge.** Load the file at startup (each file's header
   comment says how) and make sure unhandled exceptions are recorded on a
   span — `span.recordException(err)` (JS), `span.RecordError` + stack (Go),
   automatic with the Java agent and ASP.NET Core instrumentation. Or set the
   standard env vars yourself:

   ```
   OTEL_EXPORTER_OTLP_ENDPOINT=<PUBLIC_URL>
   OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer <ingest token>
   ```

   The endpoint accepts OTLP/HTTP as JSON or protobuf, optionally gzipped, on
   `/v1/traces` and `/v1/logs`. The bearer token is required: it is the only
   thing that links an exporter to your app.
5. **Watch it heal.** Errors appear in the dashboard with the top *in-app*
   file and line (library/vendor frames skipped), deduplicated by the same
   fingerprint as every other error. **Fix** opens a heal job that ends in a
   PR against your repo, guarded by the same limits — plus per-language
   checks that reject a patch which skips, disables or deletes tests
   (`it.skip`, `@Disabled`, `t.Skip`, `[Ignore]`, `markTestSkipped`,
   `skip`/`pending`, ...).

Working examples: `examples/node_app` (Express) and `examples/go_app`; run
`python scripts/demo_examples.py` to see each one's seeded bug captured with
the correct file and line, then scanned.
