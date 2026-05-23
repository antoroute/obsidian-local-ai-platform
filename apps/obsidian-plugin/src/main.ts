import {
  App,
  Modal,
  Notice,
  Plugin,
  PluginSettingTab,
  Setting,
  TAbstractFile,
  TFile,
  normalizePath,
  requestUrl,
} from "obsidian";

const DEFAULT_TEMPLATE_FILE = "compte-rendu-standard.md";
const DEFAULT_OUTPUT_FOLDER = "AI Summaries";
const DEFAULT_MODEL = "qwen2.5:14b";
const FALLBACK_TEMPLATE = `# Summary

## Key points

- 

## Decisions

- 

## Action items

- 

## Risks or uncertainties

- Mention any unclear or missing information explicitly.
`;

class UserFacingError extends Error {}

interface SummarizeRequestPayload {
  title: string;
  note_content: string;
  template: string;
  model: string;
}

interface SummarizeResponsePayload {
  model: string;
  title: string;
  summary_markdown: string;
  usage: {
    prompt_chars: number;
    template_chars: number;
  };
}

interface ModelsResponsePayload {
  models: string[];
}

interface TemplateChoice {
  label: string;
  templateContent: string;
  sourcePath: string | null;
}

interface PluginSettings {
  apiBaseUrl: string;
  apiToken: string;
  defaultModel: string;
  templatesFolder: string;
  outputFolder: string;
}

const DEFAULT_SETTINGS: PluginSettings = {
  apiBaseUrl: "",
  apiToken: "",
  defaultModel: DEFAULT_MODEL,
  templatesFolder: "Templates",
  outputFolder: DEFAULT_OUTPUT_FOLDER,
};

export default class LocalAiPlatformPlugin extends Plugin {
  settings: PluginSettings = DEFAULT_SETTINGS;

  async onload(): Promise<void> {
    await this.loadSettings();

    this.addSettingTab(new LocalAiPlatformSettingTab(this.app, this));
    this.addCommand({
      id: "summarize-current-note",
      name: "AI Meeting Assistant: Summarize current note",
      callback: async () => {
        await this.summarizeCurrentNote();
      },
    });
  }

  async loadSettings(): Promise<void> {
    const loaded = await this.loadData();
    this.settings = { ...DEFAULT_SETTINGS, ...(loaded as Partial<PluginSettings> | null) };
  }

  async saveSettings(): Promise<void> {
    await this.saveData(this.settings);
  }

  async summarizeCurrentNote(): Promise<void> {
    try {
      this.validateSummarySettings();

      const activeFile = this.app.workspace.getActiveFile();
      if (!activeFile) {
        throw new UserFacingError("Open a note before generating a summary.");
      }

      const noteContent = await this.app.vault.read(activeFile);
      if (!noteContent.trim()) {
        throw new UserFacingError("The active note is empty.");
      }

      const apiBaseUrl = this.getApiBaseUrl();
      const apiToken = this.getApiToken();
      const templateChoice = await this.chooseTemplate();
      const payload: SummarizeRequestPayload = {
        title: activeFile.basename,
        note_content: noteContent,
        template: templateChoice.templateContent,
        model: this.getDefaultModel(),
      };

      new Notice("Generating AI summary...");
      const result = await this.requestSummary(apiBaseUrl, apiToken, payload);
      const outputFile = await this.writeSummaryNote(activeFile, result, templateChoice);

      new Notice(`AI summary created: ${outputFile.path}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "An unexpected error occurred.";
      new Notice(message, 8000);
    }
  }

  getApiBaseUrl(): string {
    const value = this.settings.apiBaseUrl.trim().replace(/\/+$/, "");
    if (!value) {
      throw new UserFacingError("Missing API Base URL.");
    }
    return value;
  }

  getApiToken(): string {
    const value = this.settings.apiToken.trim();
    if (!value) {
      throw new UserFacingError("Missing API token.");
    }
    return value;
  }

  getDefaultModel(): string {
    const value = this.settings.defaultModel.trim();
    if (!value) {
      throw new UserFacingError("Missing default model.");
    }
    return value;
  }

  getOutputFolder(): string {
    const value = this.settings.outputFolder.trim();
    if (!value) {
      throw new UserFacingError("Missing output folder.");
    }
    return normalizePath(value);
  }

  validateSummarySettings(): void {
    this.getApiBaseUrl();
    this.getApiToken();
    this.getDefaultModel();
    this.getOutputFolder();
  }

  async chooseTemplate(): Promise<TemplateChoice> {
    const availableTemplates = await this.listTemplateChoices();
    if (availableTemplates.length === 0) {
      return {
        label: "Built-in default template",
        templateContent: FALLBACK_TEMPLATE,
        sourcePath: null,
      };
    }

    return chooseTemplateWithModal(this.app, availableTemplates);
  }

  async listTemplateChoices(): Promise<TemplateChoice[]> {
    const choices: TemplateChoice[] = [
      {
        label: "Built-in default template",
        templateContent: FALLBACK_TEMPLATE,
        sourcePath: null,
      },
    ];
    const templatesFolder = this.settings.templatesFolder.trim();
    if (!templatesFolder) {
      return choices;
    }

    const folder = this.app.vault.getAbstractFileByPath(normalizePath(templatesFolder));
    if (!folder) {
      return choices;
    }

    const markdownFiles = collectMarkdownFiles(folder).sort((left, right) => left.path.localeCompare(right.path));
    for (const file of markdownFiles) {
      const content = await this.app.vault.read(file);
      choices.push({
        label: file.basename,
        templateContent: content.trim() ? content : FALLBACK_TEMPLATE,
        sourcePath: file.path,
      });
    }

    return choices;
  }

  async testConnection(): Promise<void> {
    try {
      const apiBaseUrl = this.getApiBaseUrl();
      const apiToken = this.getApiToken();
      new Notice("Testing AI Gateway connection...");

      const responseText = await this.performRequest({
        apiBaseUrl,
        apiToken,
        path: "/v1/models",
        method: "GET",
        errorMap: {
          401: "The API token is invalid or expired.",
          403: "The token is missing the models:list scope.",
        },
        unavailableMessage: "The AI Gateway is unavailable.",
        invalidJsonMessage: "The AI Gateway returned an invalid models response.",
      });

      const payload = this.parseModelsResponse(responseText);
      new Notice(`Connection successful. Models: ${payload.models.join(", ")}`, 8000);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Connection test failed.";
      new Notice(message, 8000);
    }
  }

  async requestSummary(apiBaseUrl: string, apiToken: string, payload: SummarizeRequestPayload): Promise<SummarizeResponsePayload> {
    const responseText = await this.performRequest({
      apiBaseUrl,
      apiToken,
      path: "/v1/notes/summarize",
      method: "POST",
      body: JSON.stringify(payload),
      errorMap: {
        401: "The API token is invalid or expired.",
        403: "The token is missing notes:summarize or the model is not allowed.",
        413: "The note or template is too large for the AI Gateway limits.",
        422: "The AI Gateway rejected this note as invalid.",
        502: "The AI Gateway returned an invalid upstream response.",
        503: "The AI Gateway summarization service is unavailable.",
      },
      unavailableMessage: "The AI Gateway is unreachable.",
      invalidJsonMessage: "The AI Gateway returned an invalid JSON response.",
    });

    return this.parseSummaryResponse(responseText);
  }

  async performRequest(input: {
    apiBaseUrl: string;
    apiToken: string;
    path: string;
    method: "GET" | "POST";
    body?: string;
    errorMap: Record<number, string>;
    unavailableMessage: string;
    invalidJsonMessage: string;
  }): Promise<string> {
    let responseText = "";

    try {
      const response = await requestUrl({
        url: `${input.apiBaseUrl}${input.path}`,
        method: input.method,
        headers: {
          Authorization: `Bearer ${input.apiToken}`,
          "Content-Type": "application/json",
        },
        body: input.body,
      });

      responseText = response.text;
      if (response.status >= 400) {
        this.throwApiError(response.status, responseText, input.errorMap);
      }

      return responseText;
    } catch (error) {
      if (error instanceof UserFacingError) {
        throw error;
      }

      if (error instanceof Error && error.message) {
        if (error.message.includes("ECONNREFUSED") || error.message.includes("ENOTFOUND")) {
          throw new UserFacingError(`${input.unavailableMessage} Check the API Base URL and server availability.`);
        }

        if (error.message.includes("Certificate") || error.message.includes("SSL")) {
          throw new UserFacingError("TLS validation failed. Use HTTPS with a valid certificate, or http://127.0.0.1 only for local development.");
        }

        if (error.message.includes("Unexpected token") || error.message.includes("JSON")) {
          throw new UserFacingError(input.invalidJsonMessage);
        }
      }

      if (typeof error === "object" && error !== null) {
        throw new UserFacingError(`${input.unavailableMessage} The request failed before a valid response was received.`);
      }

      throw error;
    }
  }

  throwApiError(status: number, responseText: string, errorMap: Record<number, string>): never {
    const detail = this.extractErrorDetail(responseText);
    const mappedMessage = errorMap[status];
    if (mappedMessage) {
      throw new UserFacingError(mappedMessage);
    }

    throw new UserFacingError(detail || `HTTP ${status}: The AI Gateway returned an unexpected error.`);
  }

  extractErrorDetail(responseText: string): string {
    try {
      const parsed = JSON.parse(responseText) as { detail?: unknown };
      return typeof parsed.detail === "string" ? parsed.detail : "";
    } catch {
      return "";
    }
  }

  parseSummaryResponse(responseText: string): SummarizeResponsePayload {
    let parsed: unknown;
    try {
      parsed = JSON.parse(responseText);
    } catch {
      throw new UserFacingError("The AI Gateway returned an invalid JSON response.");
    }

    if (!isSummarizeResponsePayload(parsed)) {
      throw new UserFacingError("The AI Gateway returned an invalid summary payload.");
    }

    return parsed;
  }

  parseModelsResponse(responseText: string): ModelsResponsePayload {
    let parsed: unknown;
    try {
      parsed = JSON.parse(responseText);
    } catch {
      throw new UserFacingError("The AI Gateway returned an invalid models response.");
    }

    if (!isModelsResponsePayload(parsed)) {
      throw new UserFacingError("The AI Gateway returned an invalid models response.");
    }

    return parsed;
  }

  async writeSummaryNote(
    sourceFile: TFile,
    response: SummarizeResponsePayload,
    templateChoice: TemplateChoice,
  ): Promise<TFile> {
    const outputFolder = this.getOutputFolder();
    await ensureFolderExists(this.app, outputFolder);

    const date = formatDate(new Date());
    const safeTitle = sanitizeFileName(sourceFile.basename);
    const outputPath = normalizePath(`${outputFolder}/${date} - AI Summary - ${safeTitle}.md`);
    const sourceLink = this.app.metadataCache.fileToLinktext(sourceFile, "", true);
    const noteContent = buildOutputNote({
      title: response.title || sourceFile.basename,
      sourceLink,
      model: response.model,
      templateLabel: templateChoice.label,
      summaryMarkdown: response.summary_markdown,
      generatedAt: new Date(),
    });

    const existing = this.app.vault.getAbstractFileByPath(outputPath);
    if (existing instanceof TFile) {
      await this.app.vault.modify(existing, noteContent);
      return existing;
    }

    return this.app.vault.create(outputPath, noteContent);
  }
}

class LocalAiPlatformSettingTab extends PluginSettingTab {
  plugin: LocalAiPlatformPlugin;

  constructor(app: App, plugin: LocalAiPlatformPlugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display(): void {
    const { containerEl } = this;
    containerEl.empty();

    containerEl.createEl("h2", { text: "AI Meeting Assistant" });

    new Setting(containerEl)
      .setName("API Base URL")
      .setDesc("Prefer HTTPS in production. http://127.0.0.1 is acceptable for local development.")
      .addText((text) =>
        text
          .setPlaceholder("https://ai.example.com")
          .setValue(this.plugin.settings.apiBaseUrl)
          .onChange(async (value) => {
            this.plugin.settings.apiBaseUrl = value.trim();
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("API Token")
      .setDesc("Stored in Obsidian plugin settings. Never paste a shared or public token here.")
      .addText((text) => {
        text.inputEl.type = "password";
        return text
          .setPlaceholder("obsai_live_...")
          .setValue(this.plugin.settings.apiToken)
          .onChange(async (value) => {
            this.plugin.settings.apiToken = value.trim();
            await this.plugin.saveSettings();
          });
      })
      .addExtraButton((button) =>
        button.setIcon("cross").setTooltip("Clear token").onClick(async () => {
          this.plugin.settings.apiToken = "";
          await this.plugin.saveSettings();
          this.display();
        }),
      );

    new Setting(containerEl)
      .setName("Test connection")
      .setDesc("Calls GET /v1/models with the configured token.")
      .addButton((button) =>
        button.setButtonText("Test connection").onClick(async () => {
          await this.plugin.testConnection();
        }),
      );

    new Setting(containerEl)
      .setName("Default model")
      .setDesc("Sent to POST /v1/notes/summarize. Leave the gateway allowlist in sync.")
      .addText((text) =>
        text
          .setPlaceholder(DEFAULT_MODEL)
          .setValue(this.plugin.settings.defaultModel)
          .onChange(async (value) => {
            this.plugin.settings.defaultModel = value.trim() || DEFAULT_MODEL;
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Templates folder")
      .setDesc(`Vault folder containing ${DEFAULT_TEMPLATE_FILE}. Falls back to the built-in template if the file is missing.`)
      .addText((text) =>
        text
          .setPlaceholder("Templates")
          .setValue(this.plugin.settings.templatesFolder)
          .onChange(async (value) => {
            this.plugin.settings.templatesFolder = value.trim();
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Output folder")
      .setDesc("Generated summaries are written here. Missing folders are created automatically.")
      .addText((text) =>
        text
          .setPlaceholder(DEFAULT_OUTPUT_FOLDER)
          .setValue(this.plugin.settings.outputFolder)
          .onChange(async (value) => {
            this.plugin.settings.outputFolder = value.trim() || DEFAULT_OUTPUT_FOLDER;
            await this.plugin.saveSettings();
          }),
      );
  }
}

function isSummarizeResponsePayload(value: unknown): value is SummarizeResponsePayload {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const candidate = value as Partial<SummarizeResponsePayload>;
  return (
    typeof candidate.model === "string" &&
    typeof candidate.title === "string" &&
    typeof candidate.summary_markdown === "string" &&
    typeof candidate.usage === "object" &&
    candidate.usage !== null &&
    typeof candidate.usage.prompt_chars === "number" &&
    typeof candidate.usage.template_chars === "number"
  );
}

function isModelsResponsePayload(value: unknown): value is ModelsResponsePayload {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const candidate = value as Partial<ModelsResponsePayload>;
  return Array.isArray(candidate.models) && candidate.models.every((item) => typeof item === "string");
}

function collectMarkdownFiles(file: TAbstractFile): TFile[] {
  if (file instanceof TFile) {
    return file.extension === "md" ? [file] : [];
  }

  if ("children" in file && Array.isArray(file.children)) {
    return file.children.flatMap((child: TAbstractFile) => collectMarkdownFiles(child));
  }

  return [];
}

async function ensureFolderExists(app: App, folderPath: string): Promise<void> {
  const normalized = normalizePath(folderPath);
  if (!normalized || normalized === "/") {
    return;
  }

  const existing = app.vault.getAbstractFileByPath(normalized);
  if (existing) {
    return;
  }

  const segments = normalized.split("/");
  let currentPath = "";

  for (const segment of segments) {
    currentPath = currentPath ? `${currentPath}/${segment}` : segment;
    if (!app.vault.getAbstractFileByPath(currentPath)) {
      await app.vault.createFolder(currentPath);
    }
  }
}

function formatDate(date: Date): string {
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function formatIsoTimestamp(date: Date): string {
  return date.toISOString();
}

function sanitizeFileName(input: string): string {
  return input.replace(/[\\/:*?"<>|]/g, "-").trim() || "Untitled";
}

function buildOutputNote(input: {
  title: string;
  generatedAt: Date;
  model: string;
  sourceLink: string;
  templateLabel: string;
  summaryMarkdown: string;
}): string {
  return `# AI Summary - ${input.title}

- Generated at: ${formatIsoTimestamp(input.generatedAt)}
- Model: ${input.model}
- Template: ${input.templateLabel}
- Source note: [[${input.sourceLink}]]

${input.summaryMarkdown}
`;
}

class TemplatePickerModal extends Modal {
  private readonly choices: TemplateChoice[];
  private readonly onChoose: (choice: TemplateChoice) => void;

  constructor(app: App, choices: TemplateChoice[], onChoose: (choice: TemplateChoice) => void) {
    super(app);
    this.choices = choices;
    this.onChoose = onChoose;
  }

  onOpen(): void {
    const { contentEl } = this;
    contentEl.empty();
    contentEl.createEl("h2", { text: "Choose a template" });
    contentEl.createEl("p", { text: "Pick the Markdown template to send with this summary request." });

    for (const choice of this.choices) {
      const setting = new Setting(contentEl).setName(choice.label);
      if (choice.sourcePath) {
        setting.setDesc(choice.sourcePath);
      } else {
        setting.setDesc("Uses the built-in fallback template.");
      }
      setting.addButton((button) =>
        button.setButtonText("Use template").setCta().onClick(() => {
          this.onChoose(choice);
          this.close();
        }),
      );
    }
  }

  onClose(): void {
    this.contentEl.empty();
  }
}

function chooseTemplateWithModal(app: App, choices: TemplateChoice[]): Promise<TemplateChoice> {
  return new Promise((resolve, reject) => {
    let resolved = false;
    const modal = new TemplatePickerModal(app, choices, (choice) => {
      resolved = true;
      resolve(choice);
    });

    const originalOnClose = modal.onClose.bind(modal);
    modal.onClose = () => {
      originalOnClose();
      if (!resolved) {
        reject(new UserFacingError("Summary cancelled."));
      }
    };

    modal.open();
  });
}
