import assert from "node:assert/strict";
import test from "node:test";
import LocalAiPlatformPlugin from "../src/main";
import { EnterpriseError } from "../src/enterprise-transport";
import type { GenerationStatus } from "../src/enterprise-relay";
import type { TrackedTask } from "../src/task-progress";

const API_URL = "https://gateway.example.test";
const API_TOKEN = "synthetic-gateway-token";
const SUMMARY_REQUEST = {
  title: "Décisions de réunion", note_content: "La livraison est prévue lundi.",
  template: "Résume la note.", model: "local-model",
};
const SUMMARY_RESPONSE = {
  model: "local-model", title: SUMMARY_REQUEST.title, summary_markdown: "Livraison lundi.",
  usage: { prompt_chars: 34, template_chars: 15 },
};

function fixture(request: (options: { path: string; method: string; body?: string }) => Promise<string>) {
  // Do not call onload: no Obsidian lifecycle, vault or real network is involved.
  const plugin = new LocalAiPlatformPlugin({} as never, {} as never);
  plugin.llm = { isEnterprise: false, label: "Personnel · local-model" } as never;
  Object.defineProperty(plugin, "performJsonRequest", { value: request });
  const snapshots: TrackedTask[] = [];
  plugin.tasks.subscribe(() => { snapshots.push(...plugin.tasks.list()); });
  return { plugin, snapshots };
}

test("summary in a parent workflow stays at 90 percent until the caller saves its note", async () => {
  let resolveResponse!: (value: string) => void;
  const response = new Promise<string>((resolve) => { resolveResponse = resolve; });
  const { plugin, snapshots } = fixture(async (options) => {
    assert.equal(options.path, "/v1/notes/summarize");
    assert.equal(options.method, "POST");
    assert.deepEqual(JSON.parse(options.body!), SUMMARY_REQUEST);
    return response;
  });
  const parentId = plugin.tasks.begin("summary", SUMMARY_REQUEST.title);
  const pending = plugin.requestSummary(API_URL, API_TOKEN, SUMMARY_REQUEST, parentId);
  assert.equal(plugin.tasks.get(parentId)?.status, "running");
  assert.equal(plugin.tasks.get(parentId)?.percent, 60);

  resolveResponse(JSON.stringify(SUMMARY_RESPONSE));
  assert.deepEqual(await pending, SUMMARY_RESPONSE);
  assert.equal(plugin.tasks.list().length, 1);
  assert.equal(plugin.tasks.get(parentId)?.status, "running");
  assert.equal(plugin.tasks.get(parentId)?.percent, 90);
  assert.ok(snapshots.every(task => task.percent <= 90 && task.status === "running"));

  // Saving belongs to the caller: its explicit acknowledgement can now finish the task.
  plugin.tasks.finish(parentId, "Résumés/Réunion.md");
  assert.equal(plugin.tasks.get(parentId)?.percent, 100);
  assert.equal(plugin.tasks.get(parentId)?.outputPath, "Résumés/Réunion.md");
});

test("standalone summary completes only after a valid response has been parsed", async () => {
  let resolveResponse!: (value: string) => void;
  const { plugin, snapshots } = fixture(async () => new Promise(resolve => { resolveResponse = resolve; }));
  const pending = plugin.requestSummary(API_URL, API_TOKEN, SUMMARY_REQUEST);
  assert.equal(plugin.tasks.list()[0].status, "running");
  assert.equal(plugin.tasks.list()[0].percent, 60);
  assert.ok(snapshots.every(task => task.percent < 100));

  resolveResponse(JSON.stringify(SUMMARY_RESPONSE));
  assert.deepEqual(await pending, SUMMARY_RESPONSE);
  const final = plugin.tasks.list()[0];
  assert.equal(final.status, "completed");
  assert.equal(final.percent, 100);
  assert.equal(snapshots.at(-2)?.phase, "Réponse reçue et validée");
  assert.ok(snapshots.slice(0, -1).every(task => task.percent < 100));
});

for (const [failure, request] of [
  ["invalid JSON", async () => "{invalid"],
  ["invalid response schema", async () => JSON.stringify({ summary_markdown: "partial" })],
  ["network interruption", async () => { throw new Error("Synthetic connection interruption"); }],
] as const) {
  test(`summary ${failure} fails without showing 100 percent`, async () => {
    const { plugin, snapshots } = fixture(request);
    await assert.rejects(plugin.requestSummary(API_URL, API_TOKEN, SUMMARY_REQUEST));
    assert.equal(plugin.tasks.list()[0].status, "failed");
    assert.ok(snapshots.every(task => task.percent < 100));
    assert.equal(plugin.tasks.list().length, 1);
  });
}

test("enterprise summary attaches its generation ID and observed steps to the parent workflow", async () => {
  const { plugin, snapshots } = fixture(async () => { throw new Error("Personal endpoint must not be called"); });
  const parentId = plugin.tasks.begin("summary", SUMMARY_REQUEST.title);
  const generationId = "enterprise-generation-1";
  plugin.llm = {
    isEnterprise: true,
    label: "Entreprise · gpt-oss-120b",
    execute: async (kind: string, payload: unknown, onProgress: (run: GenerationStatus) => void) => {
      assert.equal(kind, "note_summary");
      assert.deepEqual(payload, SUMMARY_REQUEST);
      for (const [status, completed] of [["waiting_relay", 0], ["running", 1], ["running", 2], ["completed", 3]] as const) {
        onProgress({ id: generationId, status, progress: { completed, total: 3 } } as GenerationStatus);
      }
      return JSON.stringify(SUMMARY_RESPONSE);
    },
  } as never;

  await plugin.requestSummary(API_URL, API_TOKEN, SUMMARY_REQUEST, parentId);

  const generatedSnapshots = snapshots.filter(task => task.generationId === generationId);
  assert.deepEqual(generatedSnapshots.slice(0, 4).map(task => task.percent), [50, 63, 76, 90]);
  assert.equal(generatedSnapshots[0].phase, "En attente du relais sur le PC");
  assert.equal(generatedSnapshots[1].detail, "1 / 3 étapes terminées.");
  assert.equal(generatedSnapshots[2].detail, "2 / 3 étapes terminées.");
  assert.equal(plugin.tasks.get(parentId)?.generationId, generationId);
  assert.equal(plugin.tasks.get(parentId)?.status, "running");
  assert.equal(plugin.tasks.get(parentId)?.percent, 90);
  assert.ok(snapshots.every(task => task.percent <= 90));
});

test("cancelled enterprise summary marks the tracked task cancelled rather than failed", async () => {
  const { plugin, snapshots } = fixture(async () => { throw new Error("Personal endpoint must not be called"); });
  plugin.llm = {
    isEnterprise: true,
    label: "Entreprise · gpt-oss-120b",
    execute: async () => { throw new EnterpriseError("cancelled", "Appel interrompu."); },
  } as never;

  await assert.rejects(plugin.requestSummary(API_URL, API_TOKEN, SUMMARY_REQUEST),
    (error: unknown) => error instanceof EnterpriseError && error.code === "cancelled");

  assert.equal(plugin.tasks.list().length, 1);
  assert.equal(plugin.tasks.list()[0].status, "cancelled");
  assert.equal(plugin.tasks.list()[0].phase, "Annulé");
  assert.ok(snapshots.every(task => task.percent < 100 && task.status !== "failed"));
});

function audioJob(status: "queued" | "processing" | "completed" | "cancelled", progress: number, phase: string) {
  return {
    job_id: "audio-job-1", status, type: "audio_transcription", phase, progress,
    progress_message: null, attempts: 1, stalled: false, cancel_requested: false,
    created_at: "2026-10-11T10:00:00Z", updated_at: "2026-10-11T10:01:00Z", error: null,
  };
}

test("audio polling transfers queued and worker progress but transcription completion leaves the meeting open", async () => {
  const jobs = [audioJob("queued", 0, "queued"), audioJob("processing", 75, "transcribing"), audioJob("completed", 100, "completed")];
  let requests = 0;
  const { plugin, snapshots } = fixture(async options => {
    assert.equal(options.path, "/v1/jobs/audio-job-1");
    assert.equal(options.method, "GET");
    assert.ok(requests < jobs.length, "Polling must stop after the completed job");
    return JSON.stringify(jobs[requests++]);
  });
  const parentId = plugin.tasks.begin("meeting", "Compte rendu");
  const waits: number[] = [];
  const previousWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  Object.defineProperty(globalThis, "window", {
    configurable: true,
    value: { setTimeout: (callback: () => void, delay: number) => { waits.push(delay); queueMicrotask(callback); return 0; } },
  });
  try {
    const result = await plugin.pollAudioJob(API_URL, API_TOKEN, "audio-job-1", parentId);
    assert.equal(result.status, "completed");
    assert.deepEqual(waits, [3000, 3000]);
    assert.equal(requests, 3);
    assert.deepEqual(snapshots.map(task => task.percent), [0, 10, 32, 40]);
    assert.equal(snapshots.at(-2)?.phase, "Transcription en cours");
    assert.equal(plugin.tasks.get(parentId)?.phase, "Transcription terminée");
    assert.equal(plugin.tasks.get(parentId)?.status, "running");
    assert.ok(snapshots.every(task => task.percent < 100));
  } finally {
    if (previousWindow) Object.defineProperty(globalThis, "window", previousWindow);
    else Reflect.deleteProperty(globalThis, "window");
  }
});

test("cancelled audio stops polling and marks the parent task cancelled without completing it", async () => {
  let requests = 0;
  const { plugin, snapshots } = fixture(async () => {
    requests++;
    return JSON.stringify(audioJob("cancelled", 75, "transcribing"));
  });
  const parentId = plugin.tasks.begin("meeting", "Compte rendu");
  await assert.rejects(plugin.pollAudioJob(API_URL, API_TOKEN, "audio-job-1", parentId), /cancelled/);
  assert.equal(requests, 1);
  assert.equal(plugin.tasks.get(parentId)?.status, "cancelled");
  assert.equal(plugin.tasks.get(parentId)?.phase, "Annulé");
  assert.ok(snapshots.every(task => task.percent < 100));
});
