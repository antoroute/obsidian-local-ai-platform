export type TaskKind = "meeting" | "summary" | "assistant" | "vault";

export interface TrackedTask {
  id: string;
  title: string;
  kind: TaskKind;
  status: "running" | "completed" | "failed" | "cancelled";
  percent: number;
  phase: string;
  detail: string;
  startedAt: number;
  updatedAt: number;
  jobId?: string;
  generationId?: string;
  outputPath?: string;
}

export interface TaskUpdate {
  percent?: number;
  phase?: string;
  detail?: string;
  jobId?: string;
  generationId?: string;
  outputPath?: string;
}

export interface TaskMilestone { percent: number; phase: string; detail: string }

const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled", "expired"]);
const COMPLETED_VISIBILITY_MS = 15_000;
const HISTORY_LIMIT = 50;
let nextTaskId = 0;

export function isTerminalTaskStatus(status: string): boolean {
  return TERMINAL_STATUSES.has(status);
}

/** Recently finished tasks remain visible briefly; older results belong to history. */
export function isTaskVisible(status: string, updatedAt: string | number, showHistory: boolean, now = Date.now()): boolean {
  if (!isTerminalTaskStatus(status) || showHistory) return true;
  const timestamp = typeof updatedAt === "number" ? updatedAt : Date.parse(updatedAt);
  return Number.isFinite(timestamp) && Number.isFinite(now) && now - timestamp <= COMPLETED_VISIBILITY_MS;
}

/** Session-only progress. Returned snapshots cannot mutate a running workflow. */
export class TaskTracker {
  private readonly tasks = new Map<string, TrackedTask>();
  private readonly listeners = new Set<() => void>();

  constructor(private readonly now: () => number = Date.now) {}

  begin(kind: TaskKind, title: string): string {
    const now = this.now();
    const id = `task-${now}-${++nextTaskId}`;
    this.tasks.set(id, {
      id, title, kind, status: "running", percent: 0,
      phase: "Préparation", detail: "", startedAt: now, updatedAt: now,
    });
    this.notify();
    return id;
  }

  update(id: string, patch: TaskUpdate): void {
    const task = this.tasks.get(id);
    if (!task || task.status !== "running") return;
    if (patch.percent !== undefined && Number.isFinite(patch.percent)) {
      task.percent = Math.max(task.percent, Math.min(99, Math.max(0, Math.floor(patch.percent))));
    }
    for (const key of ["phase", "detail", "jobId", "generationId", "outputPath"] as const) {
      if (patch[key] !== undefined) task[key] = patch[key];
    }
    this.touch(task);
    this.notify();
  }

  finish(id: string, outputPath?: string): void {
    const task = this.tasks.get(id);
    if (!task || task.status !== "running") return;
    task.status = "completed";
    task.percent = 100;
    task.phase = "Terminé";
    task.detail = "";
    if (outputPath !== undefined) task.outputPath = outputPath;
    this.finalize(task);
  }

  fail(id: string, message: string): void {
    const task = this.tasks.get(id);
    if (!task || task.status !== "running") return;
    task.status = "failed";
    task.phase = "Échec";
    task.detail = message;
    this.finalize(task);
  }

  cancel(id: string, message = "Le traitement a été annulé."): void {
    const task = this.tasks.get(id);
    if (!task || task.status !== "running") return;
    task.status = "cancelled";
    task.phase = "Annulé";
    task.detail = message;
    this.finalize(task);
  }

  get(id: string): TrackedTask | undefined {
    const task = this.tasks.get(id);
    return task ? { ...task } : undefined;
  }

  list(): TrackedTask[] {
    return Array.from(this.tasks.values()).reverse()
      .sort((left, right) => right.updatedAt - left.updatedAt)
      .map((task) => ({ ...task }));
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  }

  private finalize(task: TrackedTask): void {
    this.touch(task);
    const terminalTasks = this.list().filter((item) => isTerminalTaskStatus(item.status));
    for (const expired of terminalTasks.slice(HISTORY_LIMIT)) this.tasks.delete(expired.id);
    this.notify();
  }

  private touch(task: TrackedTask): void {
    task.updatedAt = Math.max(task.updatedAt, this.now());
    // Preserve update order even when two operations have the same millisecond timestamp.
    this.tasks.delete(task.id);
    this.tasks.set(task.id, task);
  }

  private notify(): void {
    for (const listener of [...this.listeners]) {
      try { listener(); } catch { /* UI failures must not abort a generation. */ }
    }
  }
}

/** Audio occupies the first 40% of a meeting workflow; only measured worker progress is used. */
export function audioMilestone(job: { status: string; phase: string; progress: number; stalled?: boolean; cancel_requested?: boolean }): TaskMilestone {
  if (job.status === "completed") {
    return { percent: 40, phase: "Transcription terminée", detail: "Le texte de la réunion est prêt." };
  }
  const progress = Number.isFinite(job.progress) ? Math.max(0, Math.min(99, job.progress)) : 0;
  const percent = 10 + Math.floor(progress * 0.3);
  if (job.status === "failed") return { percent, phase: "Échec de la transcription", detail: "La transcription n’a pas abouti." };
  if (job.status === "cancelled") return { percent, phase: "Transcription annulée", detail: "Le traitement audio a été annulé." };
  if (job.cancel_requested) return { percent, phase: "Annulation demandée", detail: "En attente de l’arrêt du traitement audio." };
  if (job.stalled) return { percent, phase: "Transcription en attente", detail: "Le moteur n’a pas signalé de progression récente." };

  const phases = new Map<string, [string, string]>([
    ["queued", ["Audio envoyé", "En attente du moteur de transcription."]],
    ["preparing", ["Préparation de l’audio", "Le moteur prépare le fichier audio."]],
    ["normalizing", ["Préparation de l’audio", "Conversion de l’audio pour la transcription."]],
    ["transcribing", ["Transcription en cours", "Progression mesurée sur l’audio traité."]],
    ["validating", ["Vérification de la transcription", "Le moteur vérifie le texte reconnu."]],
    ["diarizing", ["Identification des intervenants", "Association des paroles aux intervenants."]],
    ["saving", ["Enregistrement de la transcription", "Le serveur enregistre le texte reconnu."]],
  ]);
  const [phase, detail] = phases.get(job.phase) ?? ["Traitement de l’audio", "En attente de la prochaine mise à jour du moteur."];
  return { percent, phase, detail };
}

/** Model progress reflects completed server steps, never elapsed time or guessed token counts. */
export function generationMilestone(run: { status: string; progress: { completed: number; total: number } }): TaskMilestone {
  if (run.status === "completed") {
    return { percent: 90, phase: "Génération terminée", detail: "Résultat prêt à enregistrer dans le coffre." };
  }
  const total = Number.isFinite(run.progress.total) ? Math.max(0, Math.floor(run.progress.total)) : 0;
  const completed = Number.isFinite(run.progress.completed) ? Math.max(0, Math.min(total, Math.floor(run.progress.completed))) : 0;
  const percent = total > 0 ? 50 + Math.floor(40 * (completed / total)) : 50;
  const stepDetail = total > 0 ? `${completed} / ${total} étapes terminées.` : "En attente des étapes de génération.";
  const states = new Map<string, [string, string]>([
    ["waiting_relay", ["En attente du relais sur le PC", stepDetail]],
    ["waiting_auth", ["Connexion HeadMind requise", "Renseignez ou renouvelez le token dans les réglages."]],
    ["waiting_network", ["En attente du réseau", "Vérifiez l’accès au réseau d’entreprise ou au VPN."]],
    ["waiting_retry", ["Reprise nécessaire", "La génération attend une reprise explicite."]],
    ["paused", ["Génération en pause", "La génération attend une reprise explicite."]],
    ["uncertain", ["Réponse du modèle incertaine", "Vérifiez l’état avant de demander une nouvelle tentative."]],
    ["failed", ["Échec de la génération", "La génération n’a pas abouti."]],
    ["cancelled", ["Génération annulée", "Le traitement a été annulé."]],
    ["expired", ["Génération expirée", "Le délai de reprise est dépassé."]],
  ]);
  const [phase, detail] = states.get(run.status) ?? ["Génération en cours", stepDetail];
  return { percent, phase, detail };
}
