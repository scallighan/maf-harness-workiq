import { HttpAgent, type AgentSubscriber } from "@ag-ui/client";
import { BrowserAuthError, type AccountInfo } from "@azure/msal-browser";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { z } from "zod";
import { api, checkedResponse, SessionBootstrap, SignIn } from "./api";
import {
  ChatMessageSchema,
  ConfigSchema,
  ConsentSchema,
  ConversationSchema,
  PreviewSchema,
  ReferencesEventSchema,
  RunSchema,
  SessionSchema,
  ToolPayloadSchema,
  displayText,
  errorMessage,
  locationLabel,
  mergeRun,
  protocolMessages,
  type Consent,
  type Conversation,
  type FileReference,
  type Preview,
  type Run,
  type Session,
  type ToolPayload,
} from "./models";

type LoadToolDetails = (run: Run, callId: string, signal: AbortSignal) => Promise<ToolPayload>;

type IconName = "chat" | "plus" | "send" | "file" | "close" | "external" | "activity" | "stop";
const paths: Record<IconName, string> = {
  chat: "M4 4h16v12H8l-4 4V4Z",
  plus: "M12 5v14M5 12h14",
  send: "m4 4 17 8-17 8 3-8-3-8Zm3 8h14",
  file: "M6 3h8l4 4v14H6V3Zm8 0v5h4M9 12h6M9 16h6",
  close: "m6 6 12 12M18 6 6 18",
  external: "M14 4h6v6M20 4 10 14M10 4H4v16h16v-6",
  activity: "M3 12h4l3-8 4 16 3-8h4",
  stop: "M6 6h12v12H6Z",
};

function Icon({ name }: { name: IconName }) {
  return <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor"
    strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d={paths[name]} /></svg>;
}

export function MarkdownAnswer({ content }: { content: string }) {
  return <Markdown skipHtml remarkPlugins={[remarkGfm]} components={{
    a: ({ children, href }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
    img: ({ alt }) => <span>[Image omitted: {alt ?? "source image"}]</span>,
    table: ({ children }) => <div className="markdown-table" tabIndex={0} role="region" aria-label="Data table">
      <table>{children}</table>
    </div>,
  }}>{displayText(content)}</Markdown>;
}

function ToolDetails({ run, callId, load }: { run: Run; callId: string; load: LoadToolDetails }) {
  const [open, setOpen] = useState(false);
  const [payload, setPayload] = useState<ToolPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!open || run.status === "running" || payload) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void load(run, callId, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setPayload(value); })
      .catch((failure: unknown) => { if (!controller.signal.aborted) setError(errorMessage(failure)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); };
  }, [open, run.status, run.id, run.session_id, callId, load, retry, payload]);
  return <details className="tool-details" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>Input / output</summary>
    {open && <div>
      {run.status === "running" && <p>Payloads will be available when this prompt finishes.</p>}
      {loading && <p role="status">Loading actual tool input and output...</p>}
      {error && <p className="tool-error" role="alert">{error} <button onClick={() => setRetry((value) => value + 1)}>Retry</button></p>}
      {payload && <>
        <h4>Input {payload.input_truncated && <small>(truncated)</small>}</h4>
        <pre>{payload.input ?? "No input was recorded."}</pre>
        <h4>Output {payload.output_truncated && <small>(truncated)</small>}</h4>
        <pre>{payload.output ?? "No output was recorded."}</pre>
        <p className="activity-note">Loaded from Foundry, not copied into Cosmos. Credential fields and temporary authentication URLs are redacted.</p>
      </>}
    </div>}
  </details>;
}

export function Activity({ run, loadDetails }: { run: Run; loadDetails: LoadToolDetails }) {
  const labels = { running: "Running", completed: "Returned", failed: "Error", cancelled: "Interrupted" };
  const active = run.status === "running";
  return <details className={`activity ${active ? "is-running" : ""}`} open={active}>
    <summary>
      {active ? <span className="spinner" aria-hidden="true" /> : <Icon name="activity" />}
      <span>{active && run.activity.length === 0
        ? "Request in progress"
        : `${run.activity.length} ${run.activity.length === 1 ? "call" : "calls"}`}</span>
      <span className={`run-state ${run.status}`}>{active ? "In progress" : run.status}</span>
    </summary>
    <div className="activity-body">
      <div className="activity-prompt">{run.prompt_preview}</div>
      {run.activity.length === 0
        ? <p className={active ? "waiting-for-agent" : undefined}>{active
          ? "Waiting for the hosted agent. Tool calls will appear as they are reported."
          : "No tool or skill calls were reported for this prompt."}</p>
        : run.activity.map((call) => <div className="activity-entry" key={call.call_id}>
          <div className="activity-call">
            <span className={`status-dot ${call.status}`} />
            <div><code>{call.name.replace("___", " / ")}</code>
              {call.kind === "skill" && <small>Skill: {call.skill ?? "name not reported"}</small>}
            </div>
            <span>{labels[call.status]}</span>
            <small>{call.duration_ms === null ? "" : `${(call.duration_ms / 1000).toFixed(1)}s`}</small>
          </div>
          <ToolDetails run={run} callId={call.call_id} load={loadDetails} />
        </div>)}
      <p className="activity-note">Actual hosted-agent calls only. Private reasoning and Work IQ's internal execution are not exposed.</p>
    </div>
  </details>;
}

function PreviewFrame({ preview }: { preview: Preview }) {
  const frameName = useRef(`preview-${crypto.randomUUID()}`).current;
  const form = useRef<HTMLFormElement>(null);
  useEffect(() => {
    if (preview.post_url) form.current?.submit();
  }, [preview]);
  const parameters = preview.post_parameters ? [...new URLSearchParams(preview.post_parameters)] : [];
  return <>
    {preview.post_url && <form ref={form} action={preview.post_url} method="post" target={frameName} hidden>
      {parameters.map(([name, value], index) => <input key={index} name={name} value={value} readOnly />)}
    </form>}
    <iframe title="Referenced document preview" name={frameName}
      src={preview.get_url ?? "about:blank"} referrerPolicy="no-referrer"
      sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-downloads" />
  </>;
}

export function App() {
  const [signIn, setSignIn] = useState<SignIn | null>(null);
  const [account, setAccount] = useState<AccountInfo | null>(null);
  const [authBusy, setAuthBusy] = useState(false);
  const [authRetry, setAuthRetry] = useState(false);
  const [catalogBusy, setCatalogBusy] = useState(false);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [loading, setLoading] = useState(false);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState<Set<string>>(new Set());
  const [consent, setConsent] = useState<Consent | null>(null);
  const [reference, setReference] = useState<FileReference | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const accountRef = useRef<string | null>(null);
  const authEpoch = useRef(0);
  const selectedRef = useRef<string | null>(null);
  const cache = useRef(new Map<string, Conversation>());
  const bootstrap = useRef(new SessionBootstrap());
  const consents = useRef(new Map<string, Consent>());
  const agents = useRef(new Map<string, HttpAgent>());
  const pending = useRef(new Map<string, string>());
  const selection = useRef(0);
  const previewAbort = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    async function initialize() {
      try {
        const response = await checkedResponse(await fetch("/api/config", { cache: "no-store" }));
        const login = new SignIn(ConfigSchema.parse(await response.json()));
        const user = await login.initialize();
        if (!alive) return;
        setSignIn(login);
        setCatalogBusy(Boolean(user));
        setAccount(user);
        accountRef.current = user ? `${user.homeAccountId}:${authEpoch.current}` : null;
      } catch (failure) {
        if (alive) setError(errorMessage(failure));
      }
    }
    void initialize();
    return () => { alive = false; };
  }, []);

  async function token(): Promise<string> {
    if (!signIn || !account) throw new Error("Sign in before using the chat.");
    return signIn.token(account);
  }

  async function refreshSessions() {
    const identity = accountRef.current;
    const value = await api("/api/sessions", z.array(SessionSchema), await token());
    if (identity === accountRef.current) {
      setSessions(value);
      for (const session of value) {
        updateConversation(session.id, (chat) => ({ ...chat, session }));
      }
    }
    return value;
  }

  useEffect(() => {
    if (!account || !signIn) return;
    let alive = true;
    const identity = accountRef.current;
    if (!identity) return;
    setCatalogBusy(true);
    void bootstrap.current.load(identity, () => signIn.token(account), () => identity === accountRef.current)
      .then(async (value) => {
        if (!alive || identity !== accountRef.current || !value) return;
        setSessions(value.sessions);
        if (value.created) {
          cache.current.set(value.created.id, { session: value.created, messages: [], runs: [] });
          await chooseSession(value.created);
        }
      })
      .catch((failure: unknown) => {
        if (alive && identity === accountRef.current) setError(errorMessage(failure));
      })
      .finally(() => {
        if (alive && identity === accountRef.current) setCatalogBusy(false);
      });
    return () => { alive = false; };
  }, [account, signIn]);

  useEffect(() => { bottom.current?.scrollIntoView({ behavior: "smooth" }); }, [conversation?.messages]);

  function updateConversation(identifier: string, update: (value: Conversation) => Conversation) {
    const current = cache.current.get(identifier);
    if (!current) return;
    const next = update(current);
    cache.current.set(identifier, next);
    if (selectedRef.current === identifier) setConversation(next);
  }

  async function chooseSession(session: Session) {
    const sequence = ++selection.current;
    const identity = accountRef.current;
    selectedRef.current = session.id;
    setSelected(session.id);
    setConsent(consents.current.get(session.id) ?? null);
    setError(null);
    previewAbort.current?.abort();
    setReference(null);
    setPreview(null);
    setPreviewError(null);
    setDraft("");
    const cached = cache.current.get(session.id);
    setConversation(cached ?? null);
    if (agents.current.has(session.id) && cached) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const value = await api(`/api/sessions/${session.id}`, ConversationSchema, await token());
      if (sequence !== selection.current || identity !== accountRef.current) return;
      cache.current.set(session.id, value);
      setConversation(value);
    } catch (failure) {
      if (sequence === selection.current && identity === accountRef.current) setError(errorMessage(failure));
    } finally {
      if (sequence === selection.current) setLoading(false);
    }
  }

  async function newSession() {
    if (catalogBusy) return;
    setError(null);
    try {
      const identity = accountRef.current;
      const session = await api("/api/sessions", SessionSchema, await token(), { method: "POST", body: "{}" });
      if (identity !== accountRef.current) return;
      cache.current.set(session.id, { session, messages: [], runs: [] });
      setSessions((values) => [session, ...values]);
      await chooseSession(session);
    } catch (failure) {
      setError(errorMessage(failure));
    }
  }

  async function renameSession(session: Session) {
    const title = window.prompt("Session title", session.title)?.trim();
    if (!title || title === session.title) return;
    try {
      const changed = await api(`/api/sessions/${session.id}`, SessionSchema, await token(), {
        method: "PATCH", body: JSON.stringify({ title }),
      });
      setSessions((values) => values.map((value) => value.id === changed.id ? changed : value));
      updateConversation(session.id, (value) => ({ ...value, session: changed }));
    } catch (failure) { setError(errorMessage(failure)); }
  }

  async function deleteSession(session: Session) {
    if (!window.confirm(`Delete "${session.title}" and its Foundry conversation?`)) return;
    try {
      await api(`/api/sessions/${session.id}`, z.object({ deleted: z.literal(true) }), await token(), {
        method: "DELETE",
      });
      cache.current.delete(session.id);
      consents.current.delete(session.id);
      setSessions((values) => values.filter((value) => value.id !== session.id));
      if (selectedRef.current === session.id) {
        ++selection.current;
        selectedRef.current = null;
        setSelected(null);
        setConversation(null);
        setReference(null);
        setPreview(null);
        setConsent(null);
      }
    } catch (failure) { setError(errorMessage(failure)); }
  }

  async function send(text: string) {
    const identifier = selectedRef.current;
    const identity = accountRef.current;
    const current = identifier ? cache.current.get(identifier) : null;
    if (
      !identifier || !current || !identity || !text.trim()
      || agents.current.has(identifier) || pending.current.has(identifier)
    ) return;
    pending.current.set(identifier, identity);
    setRunning(new Set([...agents.current.keys(), ...pending.current.keys()]));
    setError(null);
    let agent: HttpAgent | undefined;
    try {
      const accessToken = await token();
      if (identity !== accountRef.current) return;
      const user = ChatMessageSchema.parse({
        id: crypto.randomUUID(), role: "user", content: text.trim(), references: [],
      });
      const initialMessages = [...current.messages, user];
      updateConversation(identifier, (value) => ({ ...value, messages: initialMessages }));
      agent = new HttpAgent({
        url: "/api/chat", threadId: identifier, headers: { Authorization: `Bearer ${accessToken}` },
        initialMessages: protocolMessages(initialMessages), debug: false,
        fetch: async (url, options) => checkedResponse(await fetch(url, options)),
      });
      agents.current.set(identifier, agent);
      pending.current.delete(identifier);
      setRunning(new Set(agents.current.keys()));
      consents.current.delete(identifier);
      if (selectedRef.current === identifier) {
        setDraft("");
        setConsent(null);
      }
      const subscriber: AgentSubscriber = {
        onMessagesChanged({ messages }) {
          if (identity !== accountRef.current) return;
          updateConversation(identifier, (value) => ({
            ...value,
            messages: messages.flatMap((message) => {
              if ((message.role !== "user" && message.role !== "assistant") || typeof message.content !== "string") {
                return [];
              }
              const prior = value.messages.find((entry) => entry.id === message.id);
              return [ChatMessageSchema.parse({ ...message, references: prior?.references ?? [] })];
            }),
          }));
        },
        onCustomEvent({ event }) {
          if (identity !== accountRef.current) return;
          if (event.name === "workiq.activity") {
            const run = RunSchema.parse(event.value);
            if (run.session_id !== identifier) throw new Error("Mismatched session activity.");
            updateConversation(identifier, (value) => ({ ...value, runs: mergeRun(value.runs, run) }));
          } else if (event.name === "workiq.references") {
            const value = ReferencesEventSchema.parse(event.value);
            updateConversation(identifier, (chat) => ({
              ...chat,
              messages: chat.messages.map((message) => message.id === value.message_id
                ? { ...message, references: value.references } : message),
            }));
          } else if (event.name === "workiq.consent") {
            const value = ConsentSchema.parse(event.value);
            consents.current.set(identifier, value);
            if (selectedRef.current === identifier) setConsent(value);
          }
        },
        onRunErrorEvent({ event }) {
          if (selectedRef.current === identifier && identity === accountRef.current) setError(event.message);
        },
      };
      await agent.runAgent({ runId: crypto.randomUUID() }, subscriber);
    } catch (failure) {
      if (selectedRef.current === identifier && identity === accountRef.current) setError(errorMessage(failure));
    } finally {
      if (agents.current.get(identifier) === agent) agents.current.delete(identifier);
      if (pending.current.get(identifier) === identity) pending.current.delete(identifier);
      setRunning(new Set([...agents.current.keys(), ...pending.current.keys()]));
      if (identity === accountRef.current) {
        void refreshSessions().catch((failure: unknown) => setError(errorMessage(failure)));
      }
    }
  }

  async function openPreview(source: FileReference) {
    if (!selected) return;
    const identifier = selected;
    previewAbort.current?.abort();
    const controller = new AbortController();
    previewAbort.current = controller;
    setReference(source);
    setPreview(null);
    setPreviewError(null);
    setPreviewLoading(false);
    setPreviewLoading(true);
    try {
      const value = await api(`/api/sessions/${identifier}/preview`, PreviewSchema, await token(), {
        method: "POST", body: JSON.stringify({ reference: source }), signal: controller.signal,
      });
      if (!controller.signal.aborted && selectedRef.current === identifier) setPreview(value);
    } catch (failure) {
      if (!controller.signal.aborted && selectedRef.current === identifier) setPreviewError(errorMessage(failure));
    } finally {
      if (!controller.signal.aborted) setPreviewLoading(false);
    }
  }

  async function login(retry = false) {
    if (!signIn || authBusy) return;
    setAuthBusy(true);
    setAuthRetry(false);
    setError(null);
    try {
      const user = await signIn.login(retry);
      accountRef.current = `${user.homeAccountId}:${++authEpoch.current}`;
      setCatalogBusy(true);
      setAccount(user);
    } catch (failure) {
      setAuthRetry(failure instanceof BrowserAuthError && failure.errorCode === "interaction_in_progress");
      setError(errorMessage(failure));
    }
    finally { setAuthBusy(false); }
  }

  async function logout() {
    if (!signIn || !account || authBusy) return;
    setAuthBusy(true);
    setAuthRetry(false);
    accountRef.current = null;
    ++authEpoch.current;
    ++selection.current;
    selectedRef.current = null;
    for (const agent of agents.current.values()) agent.abortRun();
    agents.current.clear();
    pending.current.clear();
    cache.current.clear();
    bootstrap.current.clear();
    consents.current.clear();
    previewAbort.current?.abort();
    setSessions([]);
    setSelected(null);
    setConversation(null);
    setCatalogBusy(false);
    setRunning(new Set());
    setConsent(null);
    setReference(null);
    setPreview(null);
    const user = account;
    setAccount(null);
    try { await signIn.logout(user); }
    catch (failure) { setError(errorMessage(failure)); }
    finally { setAuthBusy(false); }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void send(draft);
  }

  const isRunning = selected !== null && running.has(selected);
  const activeCall = conversation?.runs.filter((run) => run.status === "running").at(-1)
    ?.activity.filter((call) => call.status === "running").at(-1);
  const loadToolDetails = useCallback(async (run: Run, callId: string, signal: AbortSignal) => {
    if (!signIn || !account) throw new Error("Sign in to view tool details.");
    const identity = accountRef.current;
    const accessToken = await signIn.token(account);
    if (signal.aborted || identity !== accountRef.current) throw new DOMException("Cancelled", "AbortError");
    const value = await api(
      `/api/sessions/${run.session_id}/runs/${run.id}/calls/${encodeURIComponent(callId)}`,
      ToolPayloadSchema, accessToken, { signal },
    );
    if (identity !== accountRef.current) throw new DOMException("Cancelled", "AbortError");
    return value;
  }, [signIn, account]);
  const messages = conversation?.messages.filter((message) => message.content.trim()) ?? [];
  const unattached = conversation?.runs.filter((run) =>
    !run.assistant_message_ids.some((identifier) => messages.some((message) => message.id === identifier))) ?? [];

  return <div className={`app ${reference ? "has-preview" : ""}`}>
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark"><Icon name="activity" /></span>
        <div>Work IQ<span>Research workspace</span></div>
      </div>
      <button className="new-session" onClick={() => void newSession()} disabled={!account || catalogBusy || authBusy}>
        <Icon name="plus" />New session
      </button>
      <div className="sidebar-label">YOUR SESSIONS <span>{sessions.length}</span></div>
      <nav aria-label="Chat sessions" className="session-list">
        {sessions.map((session) => <div key={session.id} className={`session ${selected === session.id ? "selected" : ""}`}>
          <button className="session-select" onClick={() => void chooseSession(session)}>
            <Icon name="chat" /><span>{session.title}</span>
            {(running.has(session.id) || session.running) && <span className="live-dot" />}
          </button>
          <div className="session-actions">
            <button aria-label={`Rename ${session.title}`} onClick={() => void renameSession(session)}>Rename</button>
            <button aria-label={`Delete ${session.title}`} disabled={running.has(session.id) || session.running}
              onClick={() => void deleteSession(session)}>Delete</button>
          </div>
        </div>)}
        {account && sessions.length === 0 && <p className="no-sessions">Start a session to begin researching. Your sessions stay private to your account.</p>}
      </nav>
      <div className="account">
        <span className="avatar">{account?.name?.slice(0, 1) ?? "W"}</span>
        <div><strong>{account?.name ?? "Not signed in"}</strong>
          <button disabled={!signIn || authBusy} onClick={() => void (account ? logout() : login())}>
            {account ? "Sign out" : authBusy ? "Signing in..." : "Sign in with Microsoft"}
          </button>
        </div>
      </div>
    </aside>

    <main className="chat">
      <header className="chat-header">
        <div><span className="eyebrow">FOUNDRY HOSTED AGENT</span>
          <h1>{conversation?.session.title ?? "Work IQ Research"}</h1>
        </div>
        <span className="connection-badge"><span />Direct MCP</span>
      </header>
      {error && <div className="error-banner" role="alert">
        <span>{authRetry
          ? "A previous sign-in attempt is still pending. Close any old sign-in window, then retry."
          : error}</span>
        {authRetry && <button disabled={authBusy} onClick={() => void login(true)}>Retry sign-in</button>}
        <button aria-label="Dismiss error" onClick={() => setError(null)}><Icon name="close" /></button>
      </div>}
      {consent && <div className="consent-banner">
        <strong>Work IQ authorization required</strong>
        <p>Authorize as your Microsoft 365 account, then retry your message.</p>
        <a href={consent.url} target="_blank" rel="noopener noreferrer">Authorize Work IQ <Icon name="external" /></a>
      </div>}
      <div className="messages" aria-live="polite" aria-busy={loading || isRunning}>
        {catalogBusy && <div className="loading" role="status"><span className="spinner" aria-hidden="true" /> Preparing your workspace...</div>}
        {loading && <div className="loading">Loading conversation...</div>}
        {!loading && messages.length === 0 && <div className="welcome">
          <div className="welcome-symbol"><Icon name="file" /></div>
          <span className="eyebrow">YOUR WORK, WITH CONTEXT</span>
          <h2>Find the source.<br />Explore the details.</h2>
          <p>Search your Microsoft 365 work context, follow the sources, and see exactly which agent tools were called.</p>
          {!account
            ? <button className="primary" disabled={!signIn || authBusy} onClick={() => void login()}>Sign in with Microsoft</button>
            : !selected
              ? <button className="primary" disabled={catalogBusy} onClick={() => void newSession()}>Create a session</button>
              : <div className="suggestions">
                {["Find the documents behind our renewal planning.", "Search my OneDrive and email for the latest project overview."].map((text) =>
                  <button key={text} onClick={() => void send(text)}>{text}<Icon name="send" /></button>)}
              </div>}
          <small>Sources and access depend on your permissions. Page and slide locations are not independently verified.</small>
        </div>}
        {messages.map((message) => <article key={message.id} className={`message ${message.role}`}>
          <div className="message-author">{message.role === "user" ? "You" : "Work IQ"}</div>
          <div className="message-content">
            {message.role === "user" ? <p>{message.content}</p> : <MarkdownAnswer content={message.content} />}
          </div>
          {message.references.length > 0 && <div className="references">
            {message.references.map((source, index) => <button className="reference" key={`${source.url}-${index}`}
              onClick={() => void openPreview(source)}>
              <Icon name="file" /><span><strong>{source.title}</strong>
                <small>{locationLabel(source)}{source.page || source.slide ? " - not verified" : ""}</small>
              </span><Icon name="external" />
            </button>)}
          </div>}
          {conversation?.runs.filter((run) => run.assistant_message_ids.at(-1) === message.id)
            .map((run) => <Activity key={run.id} run={run} loadDetails={loadToolDetails} />)}
        </article>)}
        {unattached.map((run) => <Activity key={run.id} run={run} loadDetails={loadToolDetails} />)}
        <div ref={bottom} />
      </div>
      <footer className="composer-area">
        {isRunning && <div className="request-progress" role="status">
          <span className="spinner" aria-hidden="true" />
          <div><strong>{activeCall ? `Running ${activeCall.name.replace("___", " / ")}` : "Request in progress"}</strong>
            <span>Keep this session open, switch sessions, or stop streaming below.</span>
          </div>
        </div>}
        <form className="composer" onSubmit={submit}>
          <textarea aria-label="Message" placeholder={selected ? "Ask about your work..." : "Create or select a session to start"}
            value={draft} disabled={!selected || !account || isRunning} maxLength={20000} rows={2}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(draft); }
            }} />
          {isRunning
            ? <button type="button" aria-label="Stop streaming" onClick={() => selected && agents.current.get(selected)?.abortRun()}><Icon name="stop" /></button>
            : <button type="submit" aria-label="Send message" disabled={!selected || !draft.trim()}><Icon name="send" /></button>}
        </form>
        <div className="composer-note">Read-only research. No per-call approval prompts. Verify important facts against the original source.</div>
      </footer>
    </main>

    {reference && <aside className="preview-pane" aria-label="Document preview">
      <header><div><span className="eyebrow">SOURCE PREVIEW</span><h2>{reference.title}</h2></div>
        <button aria-label="Close preview" onClick={() => { previewAbort.current?.abort(); setReference(null); setPreview(null); }}><Icon name="close" /></button>
      </header>
      <div className="preview-toolbar"><span>{locationLabel(reference)}</span>
        <a href={reference.url} target="_blank" rel="noopener noreferrer">Open original <Icon name="external" /></a>
      </div>
      {(reference.page || reference.slide) && <div className="location-warning">Location reported by the agent, not verified. The file viewer may not support jumping to this page or slide.</div>}
      {preview?.warning && <div className="location-warning">{preview.warning}</div>}
      {previewLoading && <div className="preview-placeholder" role="status">
        <span className="spinner" aria-hidden="true" /><p>Requesting a preview with your permissions...</p>
      </div>}
      {previewError && <div className="preview-placeholder"><Icon name="file" /><p>{previewError}</p>
        <a href={reference.url} target="_blank" rel="noopener noreferrer">Open the original file</a></div>}
      {preview && <PreviewFrame key={`${reference.url}-${reference.page}-${reference.slide}`} preview={preview} />}
    </aside>}
  </div>;
}
