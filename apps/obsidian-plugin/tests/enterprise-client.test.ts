import assert from "node:assert/strict";
import test from "node:test";
import { ENTERPRISE_ORIGIN, EnterpriseError, EnterpriseHttpClient, parseEnterpriseCompletion, type EnterpriseTransport, type EnterpriseTransportResponse } from "../src/enterprise-transport";
import { EnterpriseRelayClient, type GatewayRequest, type GatewayRequestOptions, type GenerationStatus } from "../src/enterprise-relay";

const TOKEN = "fake-enterprise-token-for-tests";
const MODEL = "company-model";
const json = (value: unknown, status = 200): EnterpriseTransportResponse => ({ status, headers: { "content-type": "application/json" }, body: JSON.stringify(value) });
const completion = () => json({ model: MODEL, choices: [{ message: { content: "Réponse utile.", reasoning_content: "Raisonnement privé." }, finish_reason: "stop" }] });
const models = () => json({ data: [{ id: MODEL }, { id: MODEL }, { id: "not-enabled" }] });
const tick = (ms = 1) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(predicate: () => boolean): Promise<void> {
  for (let count = 0; count < 300; count++) { if (predicate()) return; await tick(); }
  assert.fail("Expected state not reached");
}

test("desktop guard rejects before any network access", async () => {
  let calls = 0;
  const client = new EnterpriseHttpClient(false, async () => { calls++; return models(); });
  client.setToken(TOKEN);
  await assert.rejects(client.listModels(), (error: EnterpriseError) => error.code === "access_denied");
  assert.equal(calls, 0);
});

test("transport receives only fixed paths and explicit text completion payload", async () => {
  const calls: Parameters<EnterpriseTransport>[0][] = [];
  const client = new EnterpriseHttpClient(true, async (request) => { calls.push(request); return request.path === "/api/models" ? models() : completion(); });
  client.setToken(TOKEN);
  assert.deepEqual(await client.listModels(), [MODEL, "not-enabled"]);
  assert.deepEqual(await client.complete(MODEL, [{ role: "user", content: "Question" }]), { model: MODEL, content: "Réponse utile." });
  assert.deepEqual(calls.map((call) => call.path), ["/api/models", "/api/chat/completions"]);
  assert.equal(calls[1].headers.Authorization, `Bearer ${TOKEN}`);
  assert.deepEqual(JSON.parse(calls[1].body!), { model: MODEL, messages: [{ role: "user", content: "Question" }], stream: false });
  await assert.rejects(client.complete(MODEL, [{ role: "tool", content: "bad" }] as never));
  assert.equal(calls.length, 2);
});

test("HeadMind gpt-oss-120b curl contract preserves the request and returns only the final answer", async () => {
  const calls: Parameters<EnterpriseTransport>[0][] = [];
  // Sanitized response captured by the user on the professional PC; no live API call.
  const response = {
    id: "chatcmpl-98fa01812f7a7223", object: "chat.completion", created: 1791557345,
    model: "gpt-oss-120b",
    choices: [{
      index: 0,
      message: {
        role: "assistant", content: "OK", refusal: null, annotations: null, audio: null,
        function_call: null,
        reasoning: 'User says "Réponds simplement OK." Means "Reply simply OK." So we should reply "OK". Probably just "OK".',
      },
      logprobs: null, finish_reason: "stop", stop_reason: null, token_ids: null, routed_experts: null,
    }],
    service_tier: null, system_fingerprint: "vllm-0.24.0-b59144db",
    usage: { prompt_tokens: 73, total_tokens: 111, completion_tokens: 38, prompt_tokens_details: null },
    prompt_logprobs: null, prompt_token_ids: null, prompt_text: null, kv_transfer_params: null,
  };
  const client = new EnterpriseHttpClient(true, async (request) => { calls.push(request); return json(response); });
  client.setToken(TOKEN);

  const result = await client.complete("gpt-oss-120b", [{ role: "user", content: "Réponds simplement OK." }]);

  assert.equal(calls.length, 1);
  assert.equal(new URL(calls[0].path, ENTERPRISE_ORIGIN).href, "https://ai.headmind.com/api/chat/completions");
  assert.equal(calls[0].method, "POST");
  assert.equal(calls[0].headers.Authorization, `Bearer ${TOKEN}`);
  assert.equal(calls[0].headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[0].body!), {
    model: "gpt-oss-120b", messages: [{ role: "user", content: "Réponds simplement OK." }], stream: false,
  });
  assert.deepEqual(result, { model: "gpt-oss-120b", content: "OK" });
});

test("model discovery refreshes a single-model catalogue when more models become available", async () => {
  const calls: Parameters<EnterpriseTransport>[0][] = [];
  const catalogues = [
    { data: [{ id: "gpt-oss-120b" }] },
    { data: [{ id: "gpt-oss-120b" }, { id: "future-company-model" }, { id: "gpt-oss-120b" }] },
  ];
  const client = new EnterpriseHttpClient(true, async (request) => {
    calls.push(request);
    return json(catalogues[calls.length - 1]);
  });
  client.setToken(TOKEN);

  assert.deepEqual(await client.listModels(), ["gpt-oss-120b"]);
  assert.deepEqual(await client.listModels(), ["gpt-oss-120b", "future-company-model"]);
  assert.equal(calls.length, 2);
  for (const call of calls) {
    assert.equal(new URL(call.path, ENTERPRISE_ORIGIN).href, "https://ai.headmind.com/api/models");
    assert.equal(call.method, "GET");
    assert.equal(call.headers.Authorization, `Bearer ${TOKEN}`);
    assert.equal(call.body, undefined);
  }
});

test("SSE reasoning is excluded and unfinished or tool results are rejected", () => {
  const body = [
    { choices: [{ delta: { reasoning_content: "Secret reasoning" }, finish_reason: null }] },
    { model: MODEL, choices: [{ delta: { content: "Bonjour" }, finish_reason: null }] },
    { choices: [{ delta: {}, finish_reason: "stop" }] },
  ].map((data) => `data: ${JSON.stringify(data)}\n\n`).join("") + "data: [DONE]\n\n";
  assert.deepEqual(parseEnterpriseCompletion({ status: 200, headers: { "content-type": "text/event-stream" }, body }, MODEL), { model: MODEL, content: "Bonjour" });
  for (const invalid of [
    { choices: [{ message: { content: "partial" }, finish_reason: "length" }] },
    { choices: [{ message: { content: "partial" } }] },
    { choices: [{ message: { content: "", reasoning_content: "Only reasoning" }, finish_reason: "stop" }] },
    { choices: [{ message: { content: "text", tool_calls: [{ id: "tool" }] }, finish_reason: "stop" }] },
  ]) assert.throws(() => parseEnterpriseCompletion(json(invalid), MODEL), EnterpriseError);
  assert.throws(() => parseEnterpriseCompletion({ status: 200, headers: { "content-type": "text/event-stream" }, body: 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\ndata: [DONE]\n\n' }, MODEL), EnterpriseError);
});

test("redirects and upstream error bodies cannot leak tokens into errors", async () => {
  for (const status of [302, 401, 403, 429, 500]) {
    let calls = 0;
    const client = new EnterpriseHttpClient(true, async () => { calls++; return json({ error: `secret ${TOKEN}` }, status); });
    client.setToken(TOKEN);
    await assert.rejects(client.listModels(), (error: EnterpriseError) => error instanceof EnterpriseError && !error.message.includes(TOKEN));
    assert.equal(calls, 1);
    assert.equal(client.hasToken, status !== 401);
  }
  const client = new EnterpriseHttpClient(true, async () => { throw new Error(`socket failed ${TOKEN}`); });
  client.setToken(TOKEN);
  await assert.rejects(client.complete(MODEL, [{ role: "user", content: "Test" }]), (error: EnterpriseError) => error.code === "uncertain" && !error.message.includes(TOKEN));
});

test("a late rejection of the old token preserves a replacement entered meanwhile", async () => {
  let respond!: (response: EnterpriseTransportResponse) => void;
  const client = new EnterpriseHttpClient(true, async () => new Promise(resolve => { respond = resolve; }));
  client.setToken("old-token");
  const pending = client.listModels();
  client.setToken("replacement-token");
  respond(json({}, 401));
  await assert.rejects(pending, (error: EnterpriseError) => error.code === "auth_required");
  assert.equal(client.hasToken, true);
});

test("tokens with control characters are rejected before creating headers", () => {
  const client = new EnterpriseHttpClient(true);
  assert.throws(() => client.setToken("secret\r\nInjected: value"), EnterpriseError);
  assert.equal(client.hasToken, false);
});

function fixture(transportOverride?: EnterpriseTransport) {
  let upstreamCalls = 0;
  let deliveries = 0;
  let failDelivery = false;
  let claimed = false;
  let claimedModel = MODEL;
  let revoked = false;
  const requests: { path: string; options: GatewayRequestOptions }[] = [];
  const run: GenerationStatus = { id: "generation-1", kind: "assistant", provider: "enterprise_relay", model_id: MODEL, status: "waiting_relay", progress: { completed: 0, total: 1 }, error: null, created_at: new Date().toISOString(), updated_at: new Date().toISOString() };
  const gateway = async <T>(path: string, options: GatewayRequestOptions): Promise<T> => {
    requests.push({ path, options });
    const body = options.body as Record<string, unknown>;
    if (path === "/v1/llm/capabilities") return { providers: [{ id: "enterprise_relay", enabled: true, models: [MODEL] }] } as T;
    if (path === "/v1/relay/sessions") return { session_id: "session-1", session_secret: "fake-relay-secret", expires_at: new Date(Date.now() + 3600000).toISOString() } as T;
    if (path.endsWith("/revoke")) { revoked = true; return {} as T; }
    if (path === "/v1/generations" && options.method === "POST") return { ...run } as T;
    if (path === "/v1/generations") return { generations: [{ ...run }] } as T;
    if (path.endsWith("/claim")) {
      if (claimed) { await tick(2); return { step: null } as T; }
      claimed = true; run.status = "running";
      return { step: { id: "step-1", generation_id: run.id, attempt: 1, model_id: claimedModel, messages: [{ role: "user", content: "Question privée." }] } } as T;
    }
    if (path.endsWith("/heartbeat")) return {} as T;
    if (path.includes("/steps/") && path.endsWith("/result")) {
      deliveries++;
      if (failDelivery && deliveries === 1) throw new Error("Network interruption");
      assert.equal(body.content, "Réponse utile.");
      run.status = "completed"; run.progress.completed = 1;
      return {} as T;
    }
    if (path.includes("/steps/") && path.endsWith("/error")) {
      run.status = body.code === "uncertain" ? "uncertain" : "paused";
      run.error = { code: body.code as string, message: "Safe error" };
      return {} as T;
    }
    if (path.endsWith("/cancel")) { run.status = "cancelled"; return { ...run } as T; }
    if (path.endsWith("/result")) return { content: "Réponse utile." } as T;
    if (path === `/v1/generations/${run.id}`) return { ...run } as T;
    throw new Error(`Unexpected test path ${path}`);
  };
  const transport: EnterpriseTransport = async (request) => {
    if (request.path === "/api/models") return models();
    upstreamCalls++;
    return transportOverride ? transportOverride(request) : completion();
  };
  const client = new EnterpriseRelayClient(gateway as GatewayRequest, () => {}, { isDesktop: true, transport, pollMs: 1, retryMs: 1, heartbeatMs: 10000, deviceId: "test-device" });
  client.setToken(TOKEN);
  return { client, requests, run, calls: () => upstreamCalls, deliveries: () => deliveries, revoked: () => revoked, failDelivery: () => { failDelivery = true; }, wrongModel: () => { claimedModel = "forged-model"; } };
}

test("connection tests create no relay session and synthetic inference requires an explicit model", async () => {
  const f = fixture();
  try {
    await f.client.testConnection();
    assert.equal(f.calls(), 0);
    await f.client.testConnection(MODEL);
    assert.equal(f.calls(), 1);
    assert.equal(f.requests.length, 0);
  } finally { f.client.dispose(); }
});

test("delivery retries reuse one inference, secrets stay out of gateway payloads, and completed result stays retrievable", async () => {
  const f = fixture();
  f.failDelivery();
  try {
    const run = await f.client.startGeneration("assistant", { message: "Question" }, MODEL);
    assert.equal((await f.client.waitForGeneration(run.id)).status, "completed");
    assert.equal(f.calls(), 1);
    assert.equal(f.deliveries(), 2);
    await f.client.getResult(run.id); await f.client.getResult(run.id);
    assert.equal(f.calls(), 1);
    assert.equal(JSON.stringify(f.requests).includes(TOKEN), false);
    const create = f.requests.find((request) => request.path === "/v1/relay/sessions")!;
    assert.deepEqual((create.options.body as Record<string, unknown>).models, [MODEL]);
    const claim = f.requests.find((request) => request.path.endsWith("/claim"))!;
    assert.deepEqual((claim.options.body as Record<string, unknown>).run_ids, [run.id]);
    assert.equal(claim.options.headers?.["X-Relay-Secret"], "fake-relay-secret");
  } finally { f.client.dispose(); }
  assert.equal(f.revoked(), true);
});

test("a forged claim cannot change the explicitly chosen model", async () => {
  const f = fixture(); f.wrongModel();
  try {
    await f.client.startGeneration("assistant", {}, MODEL);
    await until(() => f.client.connectionState === "gateway_unavailable");
    assert.equal(f.calls(), 0);
    assert.equal(f.deliveries(), 0);
  } finally { f.client.dispose(); }
});

test("upstream 401 clears token and releases waiting UI without another inference", async () => {
  const f = fixture(async () => json({ error: TOKEN }, 401));
  try {
    const run = await f.client.startGeneration("assistant", {}, MODEL);
    await assert.rejects(f.client.waitForGeneration(run.id), (error: EnterpriseError) => error.code === "auth_required");
    assert.equal(f.client.hasToken, false);
    assert.equal(f.calls(), 1);
    assert.equal(f.run.error?.code, "auth_required");
    assert.equal(f.deliveries(), 0);
  } finally { f.client.dispose(); }
});

test("ambiguous upstream failure is paused as uncertain with no automatic retry", async () => {
  const f = fixture(async () => { throw new Error("connection ended after request body"); });
  try {
    const run = await f.client.startGeneration("assistant", {}, MODEL);
    await assert.rejects(f.client.waitForGeneration(run.id), (error: EnterpriseError) => error.code === "uncertain");
    assert.equal(f.calls(), 1);
    assert.equal(f.run.status, "uncertain");
  } finally { f.client.dispose(); }
});

test("cancel aborts the active upstream request and no late result is submitted", async () => {
  let aborted = false;
  const f = fixture((request) => new Promise((_resolve, reject) => {
    request.signal!.addEventListener("abort", () => { aborted = true; reject(new EnterpriseError("cancelled", "aborted")); });
  }));
  try {
    const run = await f.client.startGeneration("assistant", {}, MODEL);
    await until(() => f.calls() === 1);
    await f.client.cancel(run.id);
    await until(() => aborted);
    assert.equal(f.run.status, "cancelled");
    assert.equal(f.deliveries(), 0);
    assert.equal(f.requests.some((request) => request.path.endsWith("/error")), false);
  } finally { f.client.dispose(); }
});
