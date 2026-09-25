// React 18 + htm from CDN, no build step (SPEC.md HARD CONSTRAINTS #1).
const html = htm.bind(React.createElement);
const { useState, useEffect, useRef, useCallback } = React;

async function api(path, opts) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (res.status === 401) {
    const err = new Error("unauthenticated");
    err.status = 401;
    throw err;
  }
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || `HTTP ${res.status}`);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}

function useToasts() {
  const [toasts, setToasts] = useState([]);
  const push = useCallback((message) => {
    const id = Math.random().toString(36).slice(2);
    setToasts((t) => [...t, { id, message }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 6000);
  }, []);
  return [toasts, push];
}

function ToastStack({ toasts }) {
  return html`
    <div class="toast-stack" role="status" aria-live="polite">
      ${toasts.map((t) => html`<div class="toast" key=${t.id}>${t.message}</div>`)}
    </div>
  `;
}

function Login({ onLoggedIn }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
      onLoggedIn();
    } catch (err) {
      setError(err.message || "Login failed");
    } finally {
      setBusy(false);
    }
  };

  return html`
    <div class="login-wrap">
      <form class="card login-card" onSubmit=${submit} aria-label="Login">
        <h1>Self-Healing Console</h1>
        <div class="field">
          <label for="username">Username</label>
          <input id="username" value=${username} onChange=${(e) => setUsername(e.target.value)} autoComplete="username" />
        </div>
        <div class="field">
          <label for="password">Password</label>
          <input id="password" type="password" value=${password} onChange=${(e) => setPassword(e.target.value)} autoComplete="current-password" />
        </div>
        <button class="primary" type="submit" disabled=${busy}>${busy ? "Signing in…" : "Sign in"}</button>
        ${error && html`<p class="error-text" role="alert">${error}</p>`}
      </form>
    </div>
  `;
}

function Stat({ label, value }) {
  return html`<div class="stat"><div class="value">${value}</div><div class="label">${label}</div></div>`;
}

function Metrics() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api("/api/metrics")
        .then((d) => !cancelled && setData(d))
        .catch((e) => !cancelled && setError(e.message));
    load();
    const id = setInterval(load, 10000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (error) return html`<div class="error-state">Failed to load metrics: ${error}</div>`;
  if (!data) return html`<div class="loading-state">Loading metrics…</div>`;

  return html`
    <div class="card">
      <h2>Metrics</h2>
      <div class="grid">
        <${Stat} label="MTTR (min)" value=${data.mttr_minutes ?? "—"} />
        <${Stat} label="Fix success rate" value=${data.fix_success_rate ?? "—"} />
        <${Stat} label="CI auto-fix rate" value=${data.ci_auto_fix_rate ?? "—"} />
        <${Stat} label="Contract catches" value=${data.contract_violation_catches ?? 0} />
        <${Stat} label="Rollbacks" value=${data.rollback_count ?? 0} />
        <${Stat} label="Cost / fix ($)" value=${data.cost_per_fix_usd ?? "—"} />
        <${Stat} label="Daily spend ($)" value=${`${data.daily_spend_usd ?? 0} / ${data.daily_budget_usd ?? 0}`} />
        <${Stat} label="Open anomalies" value=${data.open_anomalies ?? 0} />
      </div>
    </div>
  `;
}

function ErrorsPanel() {
  const [errors, setErrors] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api("/api/errors")
        .then((d) => !cancelled && setErrors(d))
        .catch((e) => !cancelled && setError(e.message));
    load();
    const id = setInterval(load, 8000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (error) return html`<div class="error-state">Failed to load errors: ${error}</div>`;
  if (!errors) return html`<div class="loading-state">Loading errors…</div>`;
  if (errors.length === 0) return html`<div class="empty-state">No open errors 🎉</div>`;

  return html`
    <table>
      <thead><tr><th>ID</th><th>Type</th><th>Location</th><th>Occurrences</th><th>Status</th></tr></thead>
      <tbody>
        ${errors.map((e) => html`
          <tr key=${e.id}>
            <td>#${e.id}</td>
            <td>${e.exception_type}</td>
            <td>${e.file_path}:${e.line_number}</td>
            <td>${e.occurrence_count}</td>
            <td><span class="badge">${e.status}</span></td>
          </tr>
        `)}
      </tbody>
    </table>
  `;
}

function PipelinePanel() {
  const [runs, setRuns] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api("/api/pipeline")
        .then((d) => !cancelled && setRuns(d))
        .catch((e) => !cancelled && setError(e.message));
    load();
    const id = setInterval(load, 8000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (error) return html`<div class="error-state">Failed to load pipeline runs: ${error}</div>`;
  if (!runs) return html`<div class="loading-state">Loading pipeline…</div>`;
  if (runs.length === 0) return html`<div class="empty-state">No pipeline runs yet</div>`;

  return html`
    <table>
      <thead><tr><th>Run</th><th>Workflow</th><th>Branch</th><th>Status</th><th>Conclusion</th></tr></thead>
      <tbody>
        ${runs.map((r) => html`
          <tr key=${r.run_id}>
            <td>#${r.run_id}</td>
            <td>${r.workflow_name}</td>
            <td>${r.branch}</td>
            <td>${r.status}</td>
            <td><span class=${`badge ${r.conclusion === "success" ? "success" : r.conclusion ? "danger" : ""}`}>${r.conclusion ?? "—"}</span></td>
          </tr>
        `)}
      </tbody>
    </table>
  `;
}

function HealthPanel() {
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api("/api/health")
        .then((d) => !cancelled && setHealth(d))
        .catch((e) => !cancelled && setError(e.message));
    load();
    const id = setInterval(load, 8000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  if (error) return html`<div class="error-state">Failed to load health: ${error}</div>`;
  if (!health) return html`<div class="loading-state">Checking health…</div>`;

  const pods = health.pods || [];
  return html`
    <div class="grid">
      ${pods.map((info) => html`
        <div class="stat" key=${info.pod}>
          <div class="label">${info.pod}</div>
          <span class=${`badge ${info.reachable ? "success" : "danger"}`}>
            ${info.reachable ? "healthy" : "unreachable"}
          </span>
        </div>
      `)}
    </div>
  `;
}

function Dashboard() {
  return html`
    <div>
      <div class="card"><h2>Deployment health</h2><${HealthPanel} /></div>
      <div class="card"><h2>Open errors</h2><${ErrorsPanel} /></div>
      <div class="card"><h2>Pipeline runs</h2><${PipelinePanel} /></div>
    </div>
  `;
}

function Chat({ socket, pushToast }) {
  const [sessionId, setSessionId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [text, setText] = useState("");
  const [error, setError] = useState(null);
  const logRef = useRef(null);

  useEffect(() => {
    api("/api/chat/session", { method: "POST" })
      .then((d) => setSessionId(d.session_id))
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    if (!socket) return;
    const onReply = (data) => {
      if (data.session_id !== sessionId) return;
      setMessages((m) => [...m, { role: "assistant", content: data.text }]);
    };
    socket.on("chat_reply", onReply);
    return () => socket.off("chat_reply", onReply);
  }, [socket, sessionId]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [messages]);

  const send = (e) => {
    e.preventDefault();
    if (!text.trim() || !socket || sessionId == null) return;
    setMessages((m) => [...m, { role: "user", content: text }]);
    socket.emit("chat_message", { session_id: sessionId, text });
    setText("");
  };

  return html`
    <div class="card">
      <h2>AI Chat</h2>
      ${error && html`<div class="error-state">${error}</div>`}
      <div class="chat-log" ref=${logRef} aria-live="polite">
        ${messages.length === 0 && html`<div class="empty-state">Ask about errors, the pipeline, deployments, or say "show stats".</div>`}
        ${messages.map((m, i) => html`<div class=${`chat-msg ${m.role}`} key=${i}>${m.content}</div>`)}
      </div>
      <form class="chat-input-row" onSubmit=${send}>
        <textarea
          rows="2"
          value=${text}
          onChange=${(e) => setText(e.target.value)}
          onKeyDown=${(e) => { if (e.key === "Enter" && !e.shiftKey) send(e); }}
          placeholder="e.g. show stats, is production healthy, roll back production"
          aria-label="Chat message"
        ></textarea>
        <button class="primary" type="submit">Send</button>
      </form>
      <p class="confirm-hint">Destructive actions (rollback, cancel) ask for a "yes" before running.</p>
    </div>
  `;
}

function TopBar({ page, setPage, theme, setTheme, onLogout }) {
  const link = (id, label) => html`
    <a href="#" aria-current=${page === id ? "page" : undefined} class=${page === id ? "active" : ""}
       onClick=${(e) => { e.preventDefault(); setPage(id); }}>${label}</a>
  `;
  return html`
    <header class="topbar">
      <span class="brand">Self-Healing Console</span>
      <nav aria-label="Main">
        ${link("dashboard", "Dashboard")}
        ${link("chat", "Chat")}
        ${link("metrics", "Metrics")}
      </nav>
      <button aria-label="Toggle dark mode" onClick=${() => setTheme(theme === "dark" ? "light" : "dark")}>
        ${theme === "dark" ? "☀️" : "🌙"}
      </button>
      <button onClick=${onLogout}>Sign out</button>
    </header>
  `;
}

function App() {
  const [authed, setAuthed] = useState(null); // null = checking
  const [page, setPage] = useState("dashboard");
  const [theme, setTheme] = useState(() => localStorage.getItem("selfheal-theme") || "system");
  const [socket, setSocket] = useState(null);
  const [toasts, pushToast] = useToasts();

  useEffect(() => {
    if (theme === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("selfheal-theme", theme); } catch (e) {}
  }, [theme]);

  useEffect(() => {
    api("/api/auth/session").then(() => setAuthed(true)).catch(() => setAuthed(false));
  }, []);

  useEffect(() => {
    if (!authed) return;
    const s = io({ withCredentials: true });
    s.on("notification", (payload) => pushToast(payload.message || payload.event));
    setSocket(s);
    return () => s.disconnect();
  }, [authed]);

  const logout = async () => {
    await api("/api/auth/logout", { method: "POST" });
    setAuthed(false);
    if (socket) socket.disconnect();
  };

  if (authed === null) return html`<div class="loading-state">Loading…</div>`;
  if (!authed) return html`<${Login} onLoggedIn=${() => setAuthed(true)} />`;

  return html`
    <div class="app-shell">
      <${TopBar} page=${page} setPage=${setPage} theme=${theme} setTheme=${setTheme} onLogout=${logout} />
      <main>
        ${page === "dashboard" && html`<${Dashboard} />`}
        ${page === "chat" && html`<${Chat} socket=${socket} pushToast=${pushToast} />`}
        ${page === "metrics" && html`<${Metrics} />`}
      </main>
      <${ToastStack} toasts=${toasts} />
    </div>
  `;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
