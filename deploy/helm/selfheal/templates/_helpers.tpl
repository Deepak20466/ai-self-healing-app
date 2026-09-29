{{- define "selfheal.fullname" -}}
selfheal
{{- end -}}

{{- define "selfheal.postgresHost" -}}
selfheal-postgres
{{- end -}}

{{- define "selfheal.databaseUrl" -}}
postgresql+asyncpg://{{ .Values.postgres.username }}:{{ .Values.postgres.password }}@{{ include "selfheal.postgresHost" . }}:5432/{{ .Values.postgres.database }}
{{- end -}}

{{- define "selfheal.image" -}}
{{- $repo := index .Values.image.repository .pod -}}
{{- if .Values.image.registry -}}
{{ .Values.image.registry }}/{{ $repo }}:{{ .Values.image.tag }}
{{- else -}}
{{ $repo }}:{{ .Values.image.tag }}
{{- end -}}
{{- end -}}

{{- define "selfheal.commonEnv" -}}
- name: DATABASE_URL
  value: {{ include "selfheal.databaseUrl" . | quote }}
- name: ENVIRONMENT
  value: {{ .Values.env.environment | quote }}
- name: LOG_LEVEL
  value: {{ .Values.env.logLevel | quote }}
- name: APP_PORT
  value: {{ .Values.app.port | quote }}
- name: SENTINEL_PORT
  value: {{ .Values.sentinel.port | quote }}
- name: MCP_PORT
  value: {{ .Values.mcp.port | quote }}
- name: HEALER_PORT
  value: {{ .Values.healer.port | quote }}
- name: MCP_HOST
  value: "0.0.0.0"
- name: MCP_CLIENT_URL
  value: "http://selfheal-mcp:{{ .Values.mcp.port }}/mcp"
- name: SENTINEL_INGEST_URL
  # core/config.py:sentinel_base_url defaults to http://localhost:{port} --
  # a same-host-deployment assumption. app-pod is a separate Pod here, so
  # this override is required for the smoke test's /trigger/zero call to
  # actually reach sentinel-pod's real /ingest/error endpoint.
  value: "http://selfheal-sentinel:{{ .Values.sentinel.port }}"
- name: SESSION_SECRET
  valueFrom:
    secretKeyRef:
      name: selfheal-secrets
      key: session-secret
- name: HEALER_WEBHOOK_SECRET
  valueFrom:
    secretKeyRef:
      name: selfheal-secrets
      key: healer-webhook-secret
- name: SENTINEL_INGEST_TOKEN
  valueFrom:
    secretKeyRef:
      name: selfheal-secrets
      key: sentinel-ingest-token
- name: GITHUB_TOKEN
  valueFrom:
    secretKeyRef:
      name: selfheal-secrets
      key: github-token
- name: GITHUB_REPO
  value: {{ .Values.env.githubRepo | quote }}
- name: ADMIN_PASSWORD_HASH
  valueFrom:
    secretKeyRef:
      name: selfheal-secrets
      key: admin-password-hash
- name: AI_BACKEND
  value: {{ .Values.env.aiBackend | quote }}
- name: ANTHROPIC_API_KEY
  value: {{ .Values.env.anthropicApiKey | quote }}
- name: ANTHROPIC_BASE_URL
  value: {{ .Values.env.anthropicBaseUrl | quote }}
- name: AUTO_MERGE
  value: {{ .Values.env.autoMerge | quote }}
- name: HEALER_WEBHOOK_URL
  value: "http://selfheal-sentinel:{{ .Values.sentinel.port }}/webhooks/ci"
- name: PUBLIC_URL
  value: "http://selfheal-sentinel:{{ .Values.sentinel.port }}"
{{- end -}}
