import { EnterpriseError, EnterpriseHttpClient, type EnterpriseErrorCode, type EnterpriseMessage, type EnterpriseTransport } from "./enterprise-transport";
export { EnterpriseError } from "./enterprise-transport";

export type GenerationKind = "assistant" | "note_summary" | "meeting" | "meeting_from_job" | "vault_answer";
export interface GenerationStatus {
  id: string; kind: GenerationKind; status: string; provider: string; model_id: string;
  progress: { completed: number; total: number };
  error: { code: string; message: string } | null;
  created_at: string; updated_at: string;
}
export interface GatewayRequestOptions { method?: "GET" | "POST"; body?: unknown; headers?: Record<string, string>; signal?: AbortSignal }
export type GatewayRequest = <T>(path: string, options: GatewayRequestOptions) => Promise<T>;
interface RelaySession { session_id: string; session_secret: string; expires_at: string }
interface RelayStep { id: string; generation_id: string; attempt: number; model_id: string; messages: EnterpriseMessage[] }
export interface EnterpriseRelayOptions {
  isDesktop: boolean; deviceId?: string; transport?: EnterpriseTransport;
  pollMs?: number; retryMs?: number; heartbeatMs?: number;
}
const TERMINAL = new Set(["completed", "failed", "cancelled", "expired"]);
const PAUSED = new Set(["paused", "waiting_auth", "waiting_network", "waiting_retry", "uncertain"]);
function httpStatus(error: unknown): number | undefined {
  if (error && typeof error === "object" && "status" in error && typeof error.status === "number") return error.status;
  return undefined;
}
function id(value: string): string {
  if (!/^[a-zA-Z0-9-]{1,80}$/.test(value)) throw new EnterpriseError("invalid_response", "Identifiant du gateway invalide.");
  return value;
}
function uuid(): string {
  // Node is available on Desktop; construction alone must also work in mobile personal mode.
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return (require("node:crypto") as typeof import("node:crypto")).randomUUID();
}
function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) { resolve(); return; }
    const done = () => { clearTimeout(timer); signal.removeEventListener("abort", done); resolve(); };
    const timer = setTimeout(done, ms);
    signal.addEventListener("abort", done, { once: true });
  });
}

/** One relay consumer per plugin, scoped to explicitly launched/resumed runs on this device. */
export class EnterpriseRelayClient {
  private readonly http: EnterpriseHttpClient;
  private readonly lifetime = new AbortController();
  private readonly runs = new Map<string, string>();
  private session: RelaySession | null = null;
  private sessionCreation: Promise<RelaySession> | null = null;
  private models: string[] = [];
  private pumping = false;
  private active: { runId: string; controller: AbortController } | null = null;
  private state = "disconnected";
  private readonly deviceId: string;
  constructor(private readonly gateway: GatewayRequest, private readonly onChange: () => void = () => {}, private readonly options: EnterpriseRelayOptions = { isDesktop: false }) {
    this.http = new EnterpriseHttpClient(options.isDesktop, options.transport);
    this.deviceId = options.deviceId ?? uuid();
  }
  get hasToken(): boolean { return this.http.hasToken; }
  get connectionState(): string { return this.state; }
  setToken(token: string): void {
    this.ensureAlive();
    this.http.setToken(token);
    this.setState(this.hasToken ? "token_present" : "disconnected");
    if (!this.hasToken) { this.active?.controller.abort(); this.runs.clear(); }
  }
  private setState(state: string): void { this.state = state; this.onChange(); }
  private ensureAlive(): void {
    if (this.lifetime.signal.aborted) throw new EnterpriseError("cancelled", "Relais désactivé. Reconnectez HeadMind.");
  }
  private sessionOptions(session: RelaySession, body: unknown): GatewayRequestOptions {
    return { method: "POST", body, headers: { "X-Relay-Secret": session.session_secret }, signal: this.lifetime.signal };
  }
  async listModels(): Promise<string[]> {
    this.ensureAlive();
    this.models = await this.http.listModels(this.lifetime.signal);
    this.setState("connected");
    return [...this.models];
  }
  async testConnection(modelId?: string): Promise<string[]> {
    const models = await this.listModels();
    if (modelId) {
      if (!models.includes(modelId)) throw new EnterpriseError("access_denied", "Ce modèle n’est pas accessible sur HeadMind.");
      await this.http.complete(modelId, [{ role: "user", content: "Réponds uniquement par le mot OK. Ceci est un test de connexion sans données privées." }], this.lifetime.signal);
    }
    return models;
  }
  async createSession(models?: string[]): Promise<RelaySession> {
    this.ensureAlive();
    if (!this.hasToken) throw new EnterpriseError("auth_required", "Connectez HeadMind sur ce PC avant de lancer ou reprendre le traitement.");
    if (this.session && Date.parse(this.session.expires_at) > Date.now() + 60000) return this.session;
    if (this.sessionCreation) return this.sessionCreation;
    this.sessionCreation = (async () => {
      const available = models ?? await this.listModels();
      const capabilities = await this.gateway<{ providers: { id: string; enabled: boolean; models: string[] }[] }>("/v1/llm/capabilities", { signal: this.lifetime.signal });
      const provider = capabilities.providers.find((entry) => entry.id === "enterprise_relay" && entry.enabled);
      const allowed = available.filter((model) => provider?.models.includes(model)).slice(0, 100);
      if (!allowed.length) throw new EnterpriseError("access_denied", "Aucun modèle HeadMind accessible n’est autorisé par le gateway.");
      const session = await this.gateway<RelaySession>("/v1/relay/sessions", { method: "POST", body: { device_id: this.deviceId, profile_id: "headmind", models: allowed }, signal: this.lifetime.signal });
      id(session.session_id);
      if (typeof session.session_secret !== "string" || !session.session_secret || !Number.isFinite(Date.parse(session.expires_at))) throw new EnterpriseError("invalid_response", "Session de relais invalide.");
      this.session = session;
      return session;
    })();
    try { return await this.sessionCreation; } finally { this.sessionCreation = null; }
  }
  async startGeneration(kind: GenerationKind, input: object, modelId: string, idempotencyKey = uuid()): Promise<GenerationStatus> {
    this.ensureAlive();
    // Refresh before a new job so an old discovery cannot silently authorize a removed model.
    const models = await this.listModels();
    if (!models.includes(modelId)) throw new EnterpriseError("access_denied", "Ce modèle n’est pas accessible sur HeadMind.");
    const session = await this.createSession(models);
    const run = await this.gateway<GenerationStatus>("/v1/generations", { method: "POST", body: { kind, input, target: { provider: "enterprise_relay", model_id: modelId, profile_id: "headmind", relay_session_id: session.session_id }, idempotency_key: idempotencyKey }, signal: this.lifetime.signal });
    this.authorize(run, modelId);
    return run;
  }
  private authorize(run: GenerationStatus, expectedModel: string): void {
    id(run.id);
    if (run.provider !== "enterprise_relay" || run.model_id !== expectedModel) throw new EnterpriseError("invalid_response", "La cible du traitement ne correspond pas au choix effectué.");
    if (!TERMINAL.has(run.status) && !PAUSED.has(run.status)) { this.runs.set(run.id, run.model_id); this.kick(); }
  }
  async getGeneration(runId: string): Promise<GenerationStatus> {
    this.ensureAlive();
    const run = await this.gateway<GenerationStatus>(`/v1/generations/${id(runId)}`, { signal: this.lifetime.signal });
    if (TERMINAL.has(run.status) || PAUSED.has(run.status)) this.runs.delete(runId);
    return run;
  }
  async listGenerations(): Promise<GenerationStatus[]> {
    this.ensureAlive();
    const result = await this.gateway<{ generations: GenerationStatus[] }>("/v1/generations", { signal: this.lifetime.signal });
    return result.generations;
  }
  async getResult<T = unknown>(runId: string): Promise<T> {
    this.ensureAlive();
    return this.gateway<T>(`/v1/generations/${id(runId)}/result`, { signal: this.lifetime.signal });
  }
  async waitForGeneration(runId: string): Promise<GenerationStatus> {
    for (;;) {
      this.ensureAlive();
      const run = await this.getGeneration(runId);
      if (run.status === "completed") return run;
      if (TERMINAL.has(run.status) || PAUSED.has(run.status)) {
        const pausedCode = run.error?.code;
        const code: EnterpriseErrorCode = run.status === "uncertain" ? "uncertain" : run.status === "waiting_auth" || pausedCode === "auth_required" ? "auth_required" : run.status === "waiting_network" || pausedCode === "network_unavailable" ? "network_unavailable" : run.status === "cancelled" ? "cancelled" : "upstream_error";
        const labels: Record<string, string> = { waiting_auth: "Renouvelez le token HeadMind, puis reprenez depuis le centre de tâches.", waiting_network: "Connexion entreprise indisponible. Reprise disponible dans le centre de tâches.", waiting_retry: "Le traitement attend une reprise explicite dans le centre de tâches.", uncertain: "Le résultat de l’appel est incertain. Vérifiez le centre de tâches avant de relancer.", cancelled: "Traitement annulé.", failed: "Le traitement a échoué. Consultez le centre de tâches.", expired: "Le traitement a expiré." };
        throw new EnterpriseError(code, labels[run.status] ?? "Le traitement attend une reprise explicite dans le centre de tâches.");
      }
      if (!this.runs.has(runId) && !this.active) throw new EnterpriseError("network_unavailable", "Relais interrompu. Reprenez le traitement dans le centre de tâches.");
      await delay(this.options.pollMs ?? 2500, this.lifetime.signal);
    }
  }
  async resume(runId: string, retryUncertain = false): Promise<GenerationStatus> {
    const previous = await this.getGeneration(runId);
    const models = await this.listModels();
    if (!models.includes(previous.model_id)) throw new EnterpriseError("access_denied", "Le modèle de ce traitement n’est plus accessible.");
    const session = await this.createSession(models);
    const run = await this.gateway<GenerationStatus>(`/v1/generations/${id(runId)}/resume`, { method: "POST", body: { relay_session_id: session.session_id, retry_uncertain: retryUncertain }, signal: this.lifetime.signal });
    this.authorize(run, previous.model_id);
    return run;
  }
  async cancel(runId: string): Promise<GenerationStatus> {
    this.ensureAlive();
    const run = await this.gateway<GenerationStatus>(`/v1/generations/${id(runId)}/cancel`, { method: "POST", body: {}, signal: this.lifetime.signal });
    this.runs.delete(runId);
    if (this.active?.runId === runId) this.active.controller.abort();
    return run;
  }
  private kick(): void {
    if (this.pumping || this.lifetime.signal.aborted) return;
    this.pumping = true;
    void this.pump().catch(() => { this.runs.clear(); this.setState("gateway_unavailable"); }).finally(() => {
      this.pumping = false;
      // A newly accepted run can arrive between the last loop and this microtask.
      if (this.runs.size && this.hasToken && !this.lifetime.signal.aborted) this.kick();
    });
  }
  private async pump(): Promise<void> {
    while (this.runs.size && this.hasToken && !this.lifetime.signal.aborted) {
      const session = this.session;
      if (!session || Date.parse(session.expires_at) <= Date.now()) { this.runs.clear(); this.setState("session_expired"); return; }
      let step: RelayStep | null;
      try {
        const claim = await this.gateway<{ step: RelayStep | null }>(`/v1/relay/sessions/${id(session.session_id)}/claim`, this.sessionOptions(session, { run_ids: [...this.runs.keys()].slice(0, 10), wait_seconds: 25 }));
        step = claim.step;
      } catch (error) {
        if ([401, 403, 404, 409, 410, 503].includes(httpStatus(error) ?? 0)) { this.runs.clear(); this.setState("gateway_unavailable"); return; }
        this.setState("gateway_unavailable");
        await delay(this.options.retryMs ?? 5000, this.lifetime.signal);
        continue;
      }
      if (this.lifetime.signal.aborted) return;
      if (!step) {
        for (const runId of [...this.runs.keys()]) await this.getGeneration(runId);
        // Some proxies return an empty claim immediately; keep that from becoming a hot loop.
        await delay(this.options.pollMs ?? 2500, this.lifetime.signal);
        continue;
      }
      if (!this.runs.has(step.generation_id)) continue;
      if (this.runs.get(step.generation_id) !== step.model_id || !Number.isInteger(step.attempt) || step.attempt < 1) throw new EnterpriseError("invalid_response", "Étape de relais invalide.");
      id(step.id);
      await this.executeStep(step, session);
    }
    if (!this.lifetime.signal.aborted && this.hasToken) this.setState("connected");
  }
  private async executeStep(step: RelayStep, session: RelaySession): Promise<void> {
    const controller = new AbortController();
    const abort = () => controller.abort();
    this.lifetime.signal.addEventListener("abort", abort, { once: true });
    this.active = { runId: step.generation_id, controller };
    let fenced = false;
    let resultReady = false;
    let heartbeatBusy = false;
    const heartbeat = setInterval(() => {
      if (heartbeatBusy || controller.signal.aborted) return;
      heartbeatBusy = true;
      void (async () => {
        await this.gateway(`/v1/relay/sessions/${id(session.session_id)}/heartbeat`, this.sessionOptions(session, {}));
        await this.gateway(`/v1/relay/steps/${id(step.id)}/heartbeat`, this.sessionOptions(session, { session_id: session.session_id, attempt: step.attempt }));
      })().catch((error) => {
        if (!resultReady && [401, 403, 404, 409, 410].includes(httpStatus(error) ?? 0)) { fenced = true; controller.abort(); this.runs.delete(step.generation_id); }
      }).finally(() => { heartbeatBusy = false; });
    }, this.options.heartbeatMs ?? 20000);
    try {
      this.setState("running");
      const result = await this.http.complete(step.model_id, step.messages, controller.signal);
      resultReady = true;
      if (!this.runs.has(step.generation_id) || fenced || this.lifetime.signal.aborted) return;
      await this.deliver(step, session, "result", result);
    } catch (error) {
      if (!fenced && this.runs.has(step.generation_id) && !this.lifetime.signal.aborted) {
        const code = error instanceof EnterpriseError ? (error.code === "cancelled" ? "uncertain" : error.code) : "uncertain";
        await this.deliver(step, session, "error", { code });
        this.runs.delete(step.generation_id);
        this.setState(code);
        if (code === "auth_required") this.runs.clear();
      }
    } finally {
      clearInterval(heartbeat);
      this.lifetime.signal.removeEventListener("abort", abort);
      this.active = null;
    }
  }
  private async deliver(step: RelayStep, session: RelaySession, suffix: "result" | "error", value: object): Promise<void> {
    // Retry only delivery of the same frozen result. Never invoke HeadMind here.
    const body = { session_id: session.session_id, attempt: step.attempt, ...value };
    while (!this.lifetime.signal.aborted && this.runs.has(step.generation_id)) {
      try { await this.gateway(`/v1/relay/steps/${id(step.id)}/${suffix}`, this.sessionOptions(session, body)); return; }
      catch (error) {
        if ([400, 401, 403, 404, 409, 410, 413, 422].includes(httpStatus(error) ?? 0) || Date.parse(session.expires_at) <= Date.now()) {
          this.runs.delete(step.generation_id); this.setState("gateway_unavailable"); return;
        }
        this.setState("delivering");
        await delay(this.options.retryMs ?? 5000, this.lifetime.signal);
      }
    }
  }
  dispose(): void {
    if (this.lifetime.signal.aborted) return;
    const session = this.session;
    if (session) {
      // Best effort: server-side expiry still fences attempts if the PC is offline.
      void this.gateway(`/v1/relay/sessions/${id(session.session_id)}/revoke`, { method: "POST", body: {}, headers: { "X-Relay-Secret": session.session_secret } }).catch(() => {});
    }
    this.lifetime.abort();
    this.active?.controller.abort();
    this.http.setToken("");
    this.session = null;
    this.models = [];
    this.runs.clear();
    this.setState("disconnected");
  }
}
