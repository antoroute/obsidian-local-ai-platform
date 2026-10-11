import assert from "node:assert/strict";
import test from "node:test";
import { TaskTracker, audioMilestone, generationMilestone, isTaskVisible, isTerminalTaskStatus } from "../src/task-progress";

test("task lifecycle advances monotonically and reserves 100% for successful delivery", () => {
  let now = 1_000;
  const tracker = new TaskTracker(() => now);
  const id = tracker.begin("meeting", "Réunion d’équipe");
  assert.equal(tracker.get(id)?.percent, 0);
  now += 10;
  tracker.update(id, { percent: 40, phase: "Transcription terminée", jobId: "audio-1" });
  tracker.update(id, { percent: 10, detail: "Préparation du compte rendu" });
  assert.equal(tracker.get(id)?.percent, 40);
  for (const percent of [NaN, Infinity, -Infinity, -1]) tracker.update(id, { percent });
  assert.equal(tracker.get(id)?.percent, 40);
  tracker.update(id, { percent: 150 });
  assert.equal(tracker.get(id)?.percent, 99);
  now += 10;
  tracker.finish(id, "Comptes rendus/Réunion.md");
  assert.deepEqual(tracker.get(id), {
    id, kind: "meeting", title: "Réunion d’équipe", status: "completed", percent: 100,
    phase: "Terminé", detail: "", startedAt: 1_000, updatedAt: 1_020,
    jobId: "audio-1", outputPath: "Comptes rendus/Réunion.md",
  });
  now += 10;
  tracker.update(id, { phase: "Ancienne réponse réseau", percent: 25 });
  tracker.fail(id, "Une réponse tardive ne doit pas annuler la réussite");
  tracker.finish(id, "Autre fichier.md");
  assert.equal(tracker.get(id)?.status, "completed");
  assert.equal(tracker.get(id)?.updatedAt, 1_020);
  assert.equal(tracker.get(id)?.outputPath, "Comptes rendus/Réunion.md");
});

test("parallel tasks remain independent and failures cannot become completed", () => {
  let now = 1;
  const tracker = new TaskTracker(() => now);
  const summary = tracker.begin("summary", "Résumé");
  const assistant = tracker.begin("assistant", "Assistant");
  assert.notEqual(summary, assistant);
  now = 2;
  tracker.update(summary, { percent: 70, generationId: "generation-1" });
  tracker.update(assistant, { percent: 20 });
  tracker.fail(assistant, "Connexion perdue");
  tracker.finish(assistant);
  assert.equal(tracker.get(assistant)?.status, "failed");
  assert.equal(tracker.get(assistant)?.detail, "Connexion perdue");
  assert.equal(tracker.get(assistant)?.percent, 20);
  assert.equal(tracker.get(summary)?.status, "running");
  assert.equal(tracker.get(summary)?.percent, 70);
  assert.equal(tracker.get(summary)?.generationId, "generation-1");
  tracker.update("unknown", { percent: 50 });
  tracker.finish("unknown");
  tracker.fail("unknown", "Error");
  assert.equal(tracker.list().length, 2);
});

test("task snapshots cannot mutate the tracker and recent updates are listed first", () => {
  let now = 0;
  const tracker = new TaskTracker(() => now);
  const first = tracker.begin("vault", "Recherche");
  const second = tracker.begin("summary", "Résumé");
  assert.deepEqual(tracker.list().map((task) => task.id), [second, first]);
  tracker.update(first, { percent: 10 });
  assert.equal(tracker.list()[0].id, first);
  now = 100;
  tracker.update(first, { percent: 30 });
  assert.equal(tracker.list()[0].id, first);
  const snapshot = tracker.get(first)!;
  snapshot.status = "completed";
  snapshot.percent = 100;
  tracker.list()[0].title = "Mutation externe";
  assert.equal(tracker.get(first)?.status, "running");
  assert.equal(tracker.get(first)?.percent, 30);
  assert.equal(tracker.get(first)?.title, "Recherche");
  now = 50;
  tracker.update(first, { percent: 35 });
  assert.equal(tracker.get(first)?.updatedAt, 100);
});

test("cancellation preserves measured progress and ignores late model responses", () => {
  const tracker = new TaskTracker(() => 0);
  const id = tracker.begin("meeting", "Réunion");
  tracker.update(id, { percent: 25 });
  tracker.cancel(id, "Annulée depuis le centre de tâches.");
  tracker.update(id, { percent: 90 });
  tracker.finish(id, "Résultat tardif.md");
  tracker.fail(id, "Réponse réseau tardive");
  assert.equal(tracker.get(id)?.status, "cancelled");
  assert.equal(tracker.get(id)?.percent, 25);
  assert.equal(tracker.get(id)?.detail, "Annulée depuis le centre de tâches.");
  assert.equal(tracker.get(id)?.outputPath, undefined);
});

test("history keeps the 50 most recent terminal tasks without evicting active work", () => {
  let now = 0;
  const tracker = new TaskTracker(() => ++now);
  const active = Array.from({ length: 55 }, () => tracker.begin("meeting", "Active"));
  const finished: string[] = [];
  for (let index = 0; index < 55; index++) {
    const id = tracker.begin("summary", `Résumé ${index}`);
    if (index % 2 === 0) tracker.finish(id);
    else tracker.fail(id, "Erreur");
    finished.push(id);
  }
  assert.equal(tracker.list().length, 105);
  for (const id of active) assert.equal(tracker.get(id)?.status, "running");
  for (const id of finished.slice(0, 5)) assert.equal(tracker.get(id), undefined);
  for (const id of finished.slice(5)) assert.ok(tracker.get(id));
});

test("UI listener failures do not interrupt task processing and unsubscribe works", () => {
  const tracker = new TaskTracker(() => 0);
  let notifications = 0;
  tracker.subscribe(() => { throw new Error("Broken panel"); });
  const unsubscribe = tracker.subscribe(() => { notifications++; });
  const id = tracker.begin("summary", "Résumé");
  tracker.update(id, { percent: 40 });
  assert.equal(notifications, 2);
  unsubscribe();
  tracker.finish(id);
  assert.equal(notifications, 2);
  assert.equal(tracker.get(id)?.status, "completed");
});

test("terminal tasks disappear from the active view after 15 seconds but remain in history", () => {
  const ended = Date.parse("2026-10-11T12:00:00Z");
  for (const status of ["completed", "failed", "cancelled", "expired"]) {
    assert.equal(isTerminalTaskStatus(status), true);
    assert.equal(isTaskVisible(status, ended, false, ended + 15_000), true);
    assert.equal(isTaskVisible(status, "2026-10-11T12:00:00Z", false, ended + 15_001), false);
    assert.equal(isTaskVisible(status, ended, true, ended + 86_400_000), true);
    assert.equal(isTaskVisible(status, "invalid", false, ended), false);
    assert.equal(isTaskVisible(status, NaN, false, ended), false);
    assert.equal(isTaskVisible(status, Infinity, false, ended), false);
    assert.equal(isTaskVisible(status, "invalid", true, ended), true);
  }
  for (const status of ["running", "queued", "waiting_relay", "paused", "uncertain"]) {
    assert.equal(isTerminalTaskStatus(status), false);
    assert.equal(isTaskVisible(status, "invalid", false, ended), true);
    assert.equal(isTaskVisible(status, ended, false, ended + 86_400_000), true);
  }
});

test("audio milestones follow measured worker progress and distinguish transcription from final delivery", () => {
  const state = (progress: number, phase = "transcribing", status = "processing") => audioMilestone({ progress, phase, status });
  assert.deepEqual(state(0, "queued", "queued"), {
    percent: 10, phase: "Audio envoyé", detail: "En attente du moteur de transcription.",
  });
  assert.equal(state(50).percent, 25);
  assert.equal(state(85).percent, 35);
  assert.equal(state(92, "diarizing").phase, "Identification des intervenants");
  assert.equal(state(100).percent, 39);
  assert.equal(state(100, "completed", "completed").percent, 40);
  assert.equal(state(NaN).percent, 10);
  assert.equal(state(Infinity).percent, 10);
  assert.equal(state(-10).percent, 10);
  assert.equal(state(999).percent, 39);
  assert.equal(state(50, "toString").phase, "Traitement de l’audio");
  assert.equal(state(50, "transcribing", "failed").phase, "Échec de la transcription");
  assert.equal(state(50, "cancelled", "cancelled").percent, 25);
  assert.equal(audioMilestone({ status: "processing", phase: "transcribing", progress: 50, stalled: true }).percent, 25);
  assert.equal(audioMilestone({ status: "processing", phase: "transcribing", progress: 50, cancel_requested: true }).phase, "Annulation demandée");
});

test("model progress counts completed steps without inventing progress while waiting", () => {
  const state = (completed: number, total: number, status = "running") => generationMilestone({ status, progress: { completed, total } });
  assert.equal(state(0, 4).percent, 50);
  assert.equal(state(1, 4).percent, 60);
  assert.equal(state(2, 4).percent, 70);
  assert.equal(state(4, 4).percent, 90);
  assert.equal(state(4, 4, "completed").percent, 90);
  assert.match(state(2, 4).detail, /2 \/ 4 étapes terminées/);
  for (const status of ["waiting_relay", "waiting_auth", "waiting_network", "waiting_retry", "paused", "uncertain"]) {
    const first = state(2, 4, status);
    assert.equal(first.percent, 70);
    assert.notEqual(first.phase, "Génération en cours");
    assert.deepEqual(state(2, 4, status), first);
  }
  assert.equal(state(NaN, 4).percent, 50);
  assert.equal(state(1, NaN).percent, 50);
  assert.equal(state(Infinity, 4).percent, 50);
  assert.equal(state(1, Infinity).percent, 50);
  assert.equal(state(-1, 4).percent, 50);
  assert.equal(state(2, 0).percent, 50);
  assert.equal(state(100, 4).percent, 90);
  assert.equal(state(1e308, 1e308).percent, 90);
  assert.equal(state(0, 4, "toString").phase, "Génération en cours");
  assert.equal(state(0, 0, "completed").percent, 90);
});
