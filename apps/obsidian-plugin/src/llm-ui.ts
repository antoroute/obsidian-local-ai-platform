import { App, Modal, Notice, Platform, Setting, TFile, normalizePath, requestUrl } from "obsidian";
import { EnterpriseRelayClient, type GenerationStatus } from "./enterprise-relay";

type GenerationKind = "assistant" | "note_summary" | "meeting" | "meeting_from_job" | "vault_answer";
type Provider = "ollama" | "enterprise_relay";

interface LlmHost {
  app: App;
  getApiBaseUrl(): string;
  getApiToken(): string;
  getPersonalModel(): string;
  setPersonalModel(model: string): Promise<void>;
  getOutputFolder(): string;
}

const STATE_LABELS: Record<string, string> = {
  queued: "En attente", preparing: "Préparation", waiting_relay: "En attente du PC",
  running: "Génération", waiting_auth: "Token à renouveler", waiting_network: "Connexion indisponible",
  waiting_retry: "Nouvelle tentative nécessaire", uncertain: "Résultat incertain",
  completed: "Terminé", failed: "Échec", cancelled: "Annulé", expired: "Expiré",
  paused: "En pause",
};

class GatewayError extends Error {
  constructor(readonly status: number, message: string) { super(message); }
}

/** Device activation, enterprise model and credentials deliberately stay outside saveData(). */
export class LlmUiController {
  provider: Provider = "ollama";
  enterpriseModel = "";
  private models: string[] = [];
  private personalModels: string[] = [];
  private relay: EnterpriseRelayClient | null = null;
  private gatewayIdentity = "";
  private selectorContainers = new Set<HTMLElement>();
  private historyContainers = new Set<HTMLElement>();
  private history: GenerationStatus[] = [];
  private historyBusy = false;
  private historyUnsupportedUntil = 0;
  private executing = new Set<string>();

  constructor(private readonly host: LlmHost) {}

  get isEnterprise(): boolean { return this.provider === "enterprise_relay"; }

  get label(): string {
    return this.isEnterprise
      ? `Entreprise · ${this.enterpriseModel || "modèle à choisir"}`
      : `Personnel · ${this.host.getPersonalModel()}`;
  }

  private client(): EnterpriseRelayClient {
    const baseUrl = this.host.getApiBaseUrl();
    const token = this.host.getApiToken();
    const identity = `${baseUrl}\n${token}`;
    if (!this.relay || this.gatewayIdentity !== identity) {
      this.relay?.dispose();
      this.gatewayIdentity = identity;
      // Capture the gateway identity for this session; settings changes cannot redirect an active relay.
      this.relay = new EnterpriseRelayClient(async <T>(path: string, options: {
        method?: "GET" | "POST"; body?: unknown; headers?: Record<string, string>; signal?: AbortSignal;
      }): Promise<T> => {
        if (!/^\/v1\/(llm\/capabilities|generations(?:\/|$)|relay\/)/.test(path)) {
          throw new Error("Route du relais non autorisée.");
        }
        const url = new URL(baseUrl);
        if (url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname))) {
          throw new Error("Le relais nécessite un gateway HTTPS.");
        }
        if (options.signal?.aborted) throw new Error("Requête interrompue.");
        const response = await requestUrl({
          url: `${baseUrl}${path}`, method: options.method ?? "GET", throw: false,
          headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", ...options.headers },
          body: options.body === undefined ? undefined : JSON.stringify(options.body),
        });
        if (options.signal?.aborted) throw new Error("Requête interrompue.");
        if (response.status >= 400) {
          const messages: Record<number, string> = {
            401: "Le token du gateway est invalide ou expiré.",
            403: "Ce traitement ou ce modèle n’est pas autorisé par le gateway.",
            404: "Traitement introuvable ou serveur à mettre à jour pour le relais entreprise.",
            409: "Ce traitement nécessite une actualisation ou une reprise explicite.",
            413: "Le texte dépasse la taille autorisée.",
            422: "La demande de génération est invalide.",
            429: "Limite de traitements atteinte. Réessaie plus tard.",
            503: "Le relais entreprise est désactivé ou indisponible sur le serveur.",
          };
          throw new GatewayError(response.status, messages[response.status] ?? "Le gateway n’a pas pu traiter cette demande.");
        }
        try { return JSON.parse(response.text) as T; }
        catch { throw new Error("Réponse du gateway invalide."); }
      }, () => this.refreshSelectors(), { isDesktop: Platform.isDesktop });
    }
    return this.relay;
  }

  private refreshSelectors(): void {
    for (const container of this.selectorContainers) {
      if (!container.isConnected) { this.selectorContainers.delete(container); continue; }
      this.drawSelector(container);
    }
  }

  renderSelector(parent: HTMLElement): void {
    const container = parent.createDiv({ cls: "note-compagnon-llm-selector" });
    this.selectorContainers.add(container);
    this.drawSelector(container);
  }

  private drawSelector(container: HTMLElement): void {
    container.empty();
    new Setting(container).setName("Exécution du LLM").setDesc(this.label)
      .addDropdown(dropdown => {
        dropdown.addOption("ollama", "Personnel — Ollama");
        if (Platform.isDesktop) dropdown.addOption("enterprise_relay", "Entreprise — HeadMind");
        dropdown.setValue(this.provider).onChange(value => {
          this.provider = value as Provider;
          this.refreshSelectors();
        });
      });
    if (!this.isEnterprise) {
      new Setting(container).setName("Modèle personnel").addDropdown(dropdown => {
        for (const model of new Set([this.host.getPersonalModel(), ...this.personalModels])) dropdown.addOption(model, model);
        dropdown.setValue(this.host.getPersonalModel()).onChange(async model => {
          await this.host.setPersonalModel(model);
          this.refreshSelectors();
        });
      }).addButton(button => button.setButtonText("Actualiser").onClick(async () => {
        await this.withNotice(async () => {
          const result = await requestUrl({ url: `${this.host.getApiBaseUrl()}/v1/models`,
            headers: { Authorization: `Bearer ${this.host.getApiToken()}` }, throw: false });
          if (result.status !== 200) throw new Error("Impossible de charger les modèles personnels.");
          const data = JSON.parse(result.text) as { models?: unknown };
          if (!Array.isArray(data.models) || !data.models.every(model => typeof model === "string")) throw new Error("Liste de modèles invalide.");
          this.personalModels = data.models;
          this.refreshSelectors();
        });
      }));
      return;
    }
    new Setting(container).setName("Connexion entreprise")
      .setDesc(this.relay?.hasToken ? "Token présent en mémoire sur ce PC." : "Token requis sur ce PC. Il sera oublié à la fermeture d’Obsidian.")
      .addButton(button => button.setButtonText(this.relay?.hasToken ? "Remplacer le token" : "Connecter HeadMind")
        .onClick(() => new EnterpriseTokenModal(this.host.app, async token => {
          const client = this.client();
          client.setToken(token);
          this.models = await client.testConnection();
          if (!this.models.includes(this.enterpriseModel)) this.enterpriseModel = "";
          this.refreshSelectors();
          new Notice("HeadMind accessible. Choisis explicitement un modèle.");
        }).open()))
      .addButton(button => button.setButtonText("Déconnecter").onClick(() => {
        this.relay?.dispose();
        this.relay = null;
        this.models = [];
        this.enterpriseModel = "";
        this.refreshSelectors();
      }));
    new Setting(container).setName("Modèle entreprise").addDropdown(dropdown => {
      dropdown.addOption("", "Choisir un modèle accessible…");
      for (const model of this.models) dropdown.addOption(model, model);
      dropdown.setValue(this.enterpriseModel).onChange(model => {
        this.enterpriseModel = model;
        this.refreshSelectors();
      });
    }).addButton(button => button.setButtonText("Tester ce modèle").onClick(async () => {
      await this.withNotice(async () => {
        if (!this.enterpriseModel) throw new Error("Choisis d’abord un modèle.");
        await this.client().testConnection(this.enterpriseModel);
        new Notice("Génération de test HeadMind réussie (texte synthétique).");
      });
    }));
    container.createEl("p", { text: "Le contexte utile est envoyé à HeadMind ; les réponses reviennent au serveur personnel. Le serveur doit aussi autoriser le modèle choisi. Aucune bascule automatique vers Ollama." });
  }

  async execute(kind: GenerationKind, input: object): Promise<string> {
    const model = this.enterpriseModel;
    if (!model) throw new Error("Choisis un modèle entreprise dans Note Compagnon avant l’envoi.");
    const relay = this.client();
    const run = await relay.startGeneration(kind, { ...input, model }, model);
    this.executing.add(run.id);
    new Notice(`Entreprise · ${model}. Traitement disponible dans le centre de tâches.`);
    try {
      await relay.waitForGeneration(run.id);
      const result = await relay.getResult(run.id);
      return JSON.stringify({ ...(result as object), generation_id: run.id, provider: "enterprise_relay" });
    } finally {
      this.executing.delete(run.id);
      void this.refreshHistory();
    }
  }

  renderHistory(parent: HTMLElement): void {
    const container = parent.createDiv({ cls: "note-compagnon-generation-list" });
    this.historyContainers.add(container);
    this.drawHistory(container);
  }

  async refreshHistory(): Promise<void> {
    if (this.historyBusy || Date.now() < this.historyUnsupportedUntil) return;
    this.historyBusy = true;
    try {
      this.history = await this.client().listGenerations();
      for (const container of this.historyContainers) {
        if (!container.isConnected) { this.historyContainers.delete(container); continue; }
        this.drawHistory(container);
      }
    } catch (error) {
      if (error instanceof GatewayError && [404, 503].includes(error.status)) this.historyUnsupportedUntil = Date.now() + 60_000;
      // Refreshing history must not interrupt the existing personal dashboard.
    } finally { this.historyBusy = false; }
  }

  private drawHistory(container: HTMLElement): void {
    container.empty();
    if (!this.history.length) return;
    container.createEl("h4", { text: "Générations entreprise" });
    for (const run of this.history) {
      const row = container.createDiv({ cls: "note-compagnon-generation" });
      row.createEl("strong", { text: `${run.model_id} · ${run.id.slice(0, 8)}` });
      row.createEl("p", { text: `${STATE_LABELS[run.status] ?? run.status} · ${run.progress.completed}/${run.progress.total} étapes` });
      if (run.error) row.createEl("p", { text: run.error.message });
      const button = (label: string, action: () => Promise<void>) => {
        const element = row.createEl("button", { text: label });
        element.addEventListener("click", async () => {
          element.disabled = true;
          try { await this.withNotice(action); }
          finally { element.disabled = false; await this.refreshHistory(); }
        });
      };
      if (run.status === "completed" && !this.executing.has(run.id)) {
        button("Ouvrir / enregistrer le résultat", async () => { await this.exportResult(run); });
      } else if (!["completed", "cancelled", "expired", "failed"].includes(run.status)) {
        if (!["running", "preparing"].includes(run.status) && !this.executing.has(run.id)) {
          button("Reprendre sur ce PC", async () => {
            const uncertain = run.status === "uncertain" || run.error?.code === "uncertain";
            if (uncertain && !window.confirm("Le précédent appel a peut-être été exécuté par HeadMind. Relancer cette étape peut effectuer un second calcul. Continuer ?")) return;
            await this.client().resume(run.id, uncertain);
            new Notice(`Reprise sur ce PC avec ${run.model_id}.`);
          });
        }
        button("Annuler", async () => { await this.client().cancel(run.id); });
      }
    }
  }

  outputPath(id: string): string {
    if (!/^[a-zA-Z0-9-]{1,64}$/.test(id)) throw new Error("Identifiant de génération invalide.");
    return normalizePath(`${this.host.getOutputFolder()}/Entreprise - ${id}.md`);
  }

  async saveGeneratedNote(id: string, markdown: string): Promise<TFile> {
    const path = this.outputPath(id);
    const existing = this.host.app.vault.getAbstractFileByPath(path);
    if (existing instanceof TFile) return existing;
    const fields = `ai_provider: enterprise_relay\ngeneration_id: ${JSON.stringify(id)}\n`;
    const content = markdown.startsWith("---\n")
      ? markdown.replace(/^---\n/, `---\n${fields}`)
      : `---\n${fields}---\n\n${markdown}`;
    try { return await this.host.app.vault.create(path, content); }
    catch (error) {
      const concurrent = this.host.app.vault.getAbstractFileByPath(path);
      if (concurrent instanceof TFile) return concurrent;
      throw error;
    }
  }

  async exportResult(run: GenerationStatus): Promise<void> {
    const path = this.outputPath(run.id);
    let file = this.host.app.vault.getAbstractFileByPath(path);
    if (!file) {
      const result = await this.client().getResult(run.id) as Record<string, unknown>;
      const markdown = result.meeting_markdown ?? result.summary_markdown ?? result.answer_markdown;
      if (typeof markdown !== "string" || !markdown.trim()) throw new Error("Le résultat n’est pas un texte exploitable.");
      const folder = normalizePath(this.host.getOutputFolder());
      const segments = folder.split("/");
      for (let i = 1; i <= segments.length; i++) {
        const part = segments.slice(0, i).join("/");
        if (!this.host.app.vault.getAbstractFileByPath(part)) await this.host.app.vault.createFolder(part);
      }
      const sources = Array.isArray(result.sources) ? result.sources.filter((source): source is { path: string } =>
        !!source && typeof source === "object" && typeof (source as { path?: unknown }).path === "string") : [];
      const sourceText = sources.length ? `\n\n## Sources\n\n${sources.map(source => `- ${source.path}`).join("\n")}` : "";
      const contents = `---\nai_provider: enterprise_relay\nai_model: ${JSON.stringify(run.model_id)}\ngeneration_id: ${JSON.stringify(run.id)}\n---\n\n${markdown}${sourceText}\n`;
      // Fixed ID path plus create-only semantics prevents duplicate exports and overwriting edited notes.
      try { file = await this.host.app.vault.create(path, contents); }
      catch (error) { file = this.host.app.vault.getAbstractFileByPath(path); if (!file) throw error; }
    }
    if (!(file instanceof TFile)) throw new Error("Le chemin du résultat est occupé par un dossier.");
    await this.host.app.workspace.getLeaf(true).openFile(file);
  }

  private async withNotice(action: () => Promise<void>): Promise<void> {
    try { await action(); }
    catch (error) { new Notice(error instanceof Error ? error.message : "Opération indisponible.", 10000); }
  }

  dispose(): void {
    this.relay?.dispose();
    this.relay = null;
    this.gatewayIdentity = "";
    this.selectorContainers.clear();
    this.historyContainers.clear();
  }
}

class EnterpriseTokenModal extends Modal {
  constructor(app: App, private readonly connect: (token: string) => Promise<void>) { super(app); }

  onOpen(): void {
    this.contentEl.createEl("h3", { text: "Connexion à HeadMind" });
    this.contentEl.createEl("p", { text: "Le token reste en mémoire sur ce PC. Le test lit uniquement la liste des modèles accessibles." });
    const input = this.contentEl.createEl("input", { type: "password", placeholder: "Token HeadMind" });
    input.autocomplete = "off";
    input.setAttribute("aria-label", "Token HeadMind");
    input.style.width = "100%";
    const message = this.contentEl.createEl("p");
    message.setAttribute("role", "status");
    const submit = this.contentEl.createEl("button", { text: "Se connecter" });
    submit.addEventListener("click", async () => {
      if (!input.value.trim()) { message.textContent = "Saisis le token."; return; }
      submit.disabled = true;
      let token = input.value.trim();
      input.value = "";
      try { await this.connect(token); this.close(); }
      catch (error) { message.textContent = error instanceof Error ? error.message : "Connexion indisponible."; }
      finally { token = ""; submit.disabled = false; }
    });
    input.focus();
  }

  onClose(): void { this.contentEl.empty(); }
}
