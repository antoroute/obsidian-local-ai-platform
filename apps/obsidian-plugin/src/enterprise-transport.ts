/** Desktop-only, deliberately narrow transport. Never persist an instance or its token. */
export const ENTERPRISE_ORIGIN = "https://ai.headmind.com";
export type EnterpriseErrorCode = "auth_required" | "access_denied" | "rate_limited" | "network_unavailable" | "upstream_error" | "invalid_response" | "uncertain" | "cancelled";
export class EnterpriseError extends Error {
  constructor(public readonly code: EnterpriseErrorCode, message: string, public readonly status?: number) {
    super(message); this.name = "EnterpriseError";
  }
}
export interface EnterpriseMessage { role: "system" | "user" | "assistant"; content: string }
export interface EnterpriseTransportRequest {
  path: "/api/models" | "/api/chat/completions";
  method: "GET" | "POST";
  headers: Record<string, string>;
  body?: string;
  signal?: AbortSignal;
  timeoutMs: number;
  maxResponseBytes: number;
}
export interface EnterpriseTransportResponse { status: number; headers: Record<string, string | undefined>; body: string }
export type EnterpriseTransport = (request: EnterpriseTransportRequest) => Promise<EnterpriseTransportResponse>;

export const nativeEnterpriseTransport: EnterpriseTransport = async (input) => {
  if (input.path !== "/api/models" && input.path !== "/api/chat/completions") throw new EnterpriseError("access_denied", "Route entreprise interdite.");
  if (input.signal?.aborted) throw new EnterpriseError("cancelled", "Appel interrompu.");
  // Lazy require keeps the personal mode usable on mobile. Node HTTPS does not follow redirects.
  const https = require("node:https") as typeof import("node:https");
  return new Promise((resolve, reject) => {
    let settled = false;
    let request: import("node:http").ClientRequest;
    const finish = (error?: EnterpriseError, result?: EnterpriseTransportResponse) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      input.signal?.removeEventListener("abort", abort);
      if (error) { request?.destroy(); reject(error); }
      else resolve(result!);
    };
    const abort = () => finish(new EnterpriseError("cancelled", "Appel interrompu."));
    const timer = setTimeout(() => finish(new EnterpriseError(input.method === "POST" ? "uncertain" : "network_unavailable", "Délai de réponse entreprise dépassé.")), input.timeoutMs);
    try {
      request = https.request(new URL(input.path, ENTERPRISE_ORIGIN), { method: input.method, headers: input.headers, rejectUnauthorized: true }, (response) => {
        const status = response.statusCode ?? 0;
        if (status >= 300 && status < 400) {
          response.resume();
          finish(new EnterpriseError("access_denied", "Redirection HeadMind refusée.", status));
          return;
        }
        const chunks: Buffer[] = [];
        let bytes = 0;
        response.on("data", (chunk: Buffer) => {
          bytes += chunk.length;
          if (bytes > input.maxResponseBytes) { finish(new EnterpriseError("invalid_response", "Réponse entreprise trop volumineuse.")); return; }
          chunks.push(chunk);
        });
        response.on("aborted", () => finish(new EnterpriseError(input.method === "POST" ? "uncertain" : "network_unavailable", "Réponse entreprise interrompue.")));
        response.on("error", () => finish(new EnterpriseError("uncertain", "Réponse entreprise interrompue.")));
        response.on("end", () => {
          const headers: Record<string, string | undefined> = {};
          for (const [name, value] of Object.entries(response.headers)) headers[name] = Array.isArray(value) ? value.join(", ") : value;
          finish(undefined, { status, headers, body: Buffer.concat(chunks).toString("utf8") });
        });
      });
      request.on("error", () => finish(new EnterpriseError(input.method === "POST" ? "uncertain" : "network_unavailable", "Connexion à HeadMind indisponible.")));
      input.signal?.addEventListener("abort", abort, { once: true });
      if (input.signal?.aborted) { abort(); return; }
      if (input.body) request.write(input.body);
      request.end();
    } catch { finish(new EnterpriseError("network_unavailable", "Transport HTTPS indisponible sur cet appareil.")); }
  });
};

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function invalid(): never { throw new EnterpriseError("invalid_response", "Réponse HeadMind incomplète ou incompatible."); }
function completionText(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map((part) => {
    const item = record(part);
    if (item.type !== "text" || typeof item.text !== "string") invalid();
    return item.text as string;
  }).join("");
  if (value == null) return "";
  return invalid();
}
export function parseEnterpriseCompletion(response: EnterpriseTransportResponse, requestedModel: string): { model: string; content: string } {
  let model = requestedModel;
  let content = "";
  let finished = false;
  const consume = (value: unknown, streaming: boolean) => {
    const data = record(value);
    if (data.error) invalid();
    if (typeof data.model === "string" && data.model.length > 0 && data.model.length <= 200) model = data.model;
    if (!Array.isArray(data.choices)) invalid();
    if (!data.choices.length && streaming) return; // Usage-only terminal chunk.
    if (data.choices.length !== 1) invalid();
    const choice = record(data.choices[0]);
    const message = record(streaming ? choice.delta : choice.message);
    if ((Array.isArray(message.tool_calls) && message.tool_calls.length) || message.function_call || message.refusal) invalid();
    content += completionText(message.content);
    if (choice.finish_reason != null) {
      if (choice.finish_reason !== "stop") invalid();
      finished = true;
    }
    // reasoning_content/reasoning are deliberately not copied to notes or the gateway.
  };
  try {
    if ((response.headers["content-type"] ?? "").includes("text/event-stream") || response.body.trimStart().startsWith("data:")) {
      for (const event of response.body.replace(/\r\n/g, "\n").split(/\n\n+/)) {
        const data = event.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
        if (!data || data === "[DONE]") continue;
        consume(JSON.parse(data), true);
      }
    } else consume(JSON.parse(response.body), false);
  } catch (error) { if (error instanceof EnterpriseError) throw error; invalid(); }
  if (!finished || !content.trim()) invalid();
  return { model, content: content.trim() };
}

export class EnterpriseHttpClient {
  private token = "";
  constructor(private readonly isDesktop: boolean, private readonly transport: EnterpriseTransport = nativeEnterpriseTransport) {}
  get hasToken(): boolean { return Boolean(this.token); }
  setToken(value: string): void {
    const token = value.trim();
    if (token && (!/^[\x21-\x7e]+$/.test(token) || token.length > 16384)) throw new EnterpriseError("auth_required", "Format du token HeadMind invalide.");
    this.token = token;
  }
  private async request(path: EnterpriseTransportRequest["path"], body?: unknown, signal?: AbortSignal): Promise<EnterpriseTransportResponse> {
    if (!this.isDesktop) throw new EnterpriseError("access_denied", "Le mode entreprise nécessite Obsidian Desktop.");
    if (!this.token) throw new EnterpriseError("auth_required", "Saisissez votre token HeadMind.");
    const sentToken = this.token;
    let response: EnterpriseTransportResponse;
    try {
      response = await this.transport({ path, method: body ? "POST" : "GET", headers: { Authorization: `Bearer ${sentToken}`, Accept: "application/json, text/event-stream", ...(body ? { "Content-Type": "application/json" } : {}) }, body: body ? JSON.stringify(body) : undefined, signal, timeoutMs: body ? 240000 : 30000, maxResponseBytes: 8 * 1024 * 1024 });
    } catch (error) {
      if (error instanceof EnterpriseError) throw error;
      throw new EnterpriseError(body ? "uncertain" : "network_unavailable", "Connexion à HeadMind indisponible.");
    }
    if (signal?.aborted) throw new EnterpriseError("cancelled", "Appel interrompu.");
    if (Buffer.byteLength(response.body, "utf8") > 8 * 1024 * 1024) invalid();
    if (response.status === 401) {
      if (this.token === sentToken) this.token = "";
      throw new EnterpriseError("auth_required", "Le token HeadMind doit être renouvelé.", 401);
    }
    if (response.status === 403 || (response.status >= 300 && response.status < 400)) throw new EnterpriseError("access_denied", "Accès HeadMind refusé.", response.status);
    if (response.status === 429) throw new EnterpriseError("rate_limited", "Limite HeadMind atteinte. Réessayez plus tard.", 429);
    if (response.status >= 500) throw new EnterpriseError(body ? "uncertain" : "upstream_error", "HeadMind a rencontré une erreur.", response.status);
    if (response.status < 200 || response.status >= 300) throw new EnterpriseError("upstream_error", "Requête HeadMind refusée.", response.status);
    return response;
  }
  async listModels(signal?: AbortSignal): Promise<string[]> {
    const response = await this.request("/api/models", undefined, signal);
    let body: Record<string, unknown>;
    try { body = record(JSON.parse(response.body)); } catch { return invalid(); }
    if (!Array.isArray(body.data)) invalid();
    const models = body.data.map((item) => record(item).id).filter((id): id is string => typeof id === "string" && id.length > 0 && id.length <= 200);
    if (models.length > 1000) invalid();
    return [...new Set(models)];
  }
  async complete(model: string, messages: EnterpriseMessage[], signal?: AbortSignal): Promise<{ model: string; content: string }> {
    if (!model || model.length > 200 || !Array.isArray(messages) || !messages.length || messages.length > 100 || messages.some((m) => !m || !["system", "user", "assistant"].includes(m.role) || typeof m.content !== "string") || messages.reduce((size, m) => size + m.content.length, 0) > 500000) invalid();
    return parseEnterpriseCompletion(await this.request("/api/chat/completions", { model, messages, stream: false }, signal), model);
  }
}
