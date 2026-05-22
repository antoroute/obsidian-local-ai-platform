import {
  App,
  Notice,
  Plugin,
  PluginSettingTab,
  Setting,
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
      const activeFile = this.app.workspace.getActiveFile();
      if (!activeFile) {
        throw new Error("Open a note before running the summarization command.");
      }

      const noteContent = await this.app.vault.read(activeFile);
      if (!noteContent.trim()) {
        throw new Error("The active note is empty.");
      }

      const apiBaseUrl = this.getApiBaseUrl();
      const apiToken = this.getApiToken();
      const templateContent = await this.loadDefaultTemplate();
      const payload: SummarizeRequestPayload = {
        title: activeFile.basename,
        note_content: noteContent,
        template: templateContent,
        model: this.settings.defaultModel.trim() || DEFAULT_MODEL,
      };

      new Notice("Generating AI summary...");
      const result = await this.requestSummary(apiBaseUrl, apiToken, payload);
      const outputFile = await this.writeSummaryNote(activeFile, result);

      new Notice(`AI summary created: ${outputFile.path}`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "An unexpected error occurred.";
      new Notice(message, 8000);
    }
  }

  getApiBaseUrl(): string {
    const value = this.settings.apiBaseUrl.trim().replace(/\/+$/, "");
    if (!value) {
      throw new Error("Set the AI Gateway API Base URL in the plugin settings before summarizing notes.");
    }
    return value;
  }

  getApiToken(): string {
    const value = this.settings.apiToken.trim();
    if (!value) {
      throw new Error("Set the AI Gateway API token in the plugin settings before summarizing notes.");
    }
    return value;
  }

  async loadDefaultTemplate(): Promise<string> {
    const templatesFolder = this.settings.templatesFolder.trim();
    if (!templatesFolder) {
      return FALLBACK_TEMPLATE;
    }

    const templatePath = normalizePath(`${templatesFolder}/${DEFAULT_TEMPLATE_FILE}`);
    const templateFile = this.app.vault.getAbstractFileByPath(templatePath);
    if (templateFile instanceof TFile) {
      const content = await this.app.vault.read(templateFile);
      return content.trim() ? content : FALLBACK_TEMPLATE;
    }

    return FALLBACK_TEMPLATE;
  }

  async requestSummary(
    apiBaseUrl: string,
    apiToken: string,
    payload: SummarizeRequestPayload,
  ): Promise<SummarizeResponsePayload> {
    let responseText = "";

    try {
      const response = await requestUrl({
        url: `${apiBaseUrl}/v1/notes/summarize`,
        method: "POST",
        headers: {
          Authorization: `Bearer ${apiToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(payload),
      });

      responseText = response.text;
      if (response.status >= 400) {
        this.throwApiError(response.status, responseText);
      }

      return this.parseSummaryResponse(responseText);
    } catch (error) {
      if (error instanceof UserFacingError) {
        throw error;
      }

      if (error instanceof Error && error.message) {
        if (error.message.includes("ECONNREFUSED") || error.message.includes("ENOTFOUND")) {
          throw new UserFacingError("The AI Gateway is unreachable. Check the API Base URL and server availability.");
        }

        if (error.message.includes("Certificate") || error.message.includes("SSL")) {
          throw new UserFacingError("TLS validation failed. Use HTTPS with a valid certificate, or http://127.0.0.1 only for local development.");
        }

        if (error.message.includes("Unexpected token") || error.message.includes("JSON")) {
          throw new UserFacingError("The AI Gateway returned an invalid JSON response.");
        }
      }

      if (typeof error === "object" && error !== null) {
        throw new UserFacingError("The AI Gateway request failed before a valid response was received.");
      }

      throw error;
    }
  }

  throwApiError(status: number, responseText: string): never {
    const detail = this.extractErrorDetail(responseText);

    if (status === 401) {
      throw new UserFacingError("The API token is invalid, expired, or missing the Bearer format expected by the AI Gateway.");
    }

    if (status === 403) {
      if (detail.includes("notes:summarize")) {
        throw new UserFacingError("The configured token does not have the notes:summarize scope.");
      }
      if (detail.includes("model")) {
        throw new UserFacingError("The selected model is not allowed by the AI Gateway.");
      }
      throw new UserFacingError("The AI Gateway refused this request.");
    }

    if (status === 413) {
      throw new UserFacingError("The note or template is too large for the AI Gateway limits.");
    }

    if (status === 422) {
      throw new UserFacingError(detail || "The AI Gateway rejected the note payload as invalid.");
    }

    if (status === 502 || status === 503) {
      throw new UserFacingError("The AI Gateway summarization service is currently unavailable.");
    }

    throw new UserFacingError(`HTTP ${status}: ${detail || "The AI Gateway returned an unexpected error."}`);
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

  async writeSummaryNote(sourceFile: TFile, response: SummarizeResponsePayload): Promise<TFile> {
    const outputFolder = normalizePath(this.settings.outputFolder.trim() || DEFAULT_OUTPUT_FOLDER);
    await ensureFolderExists(this.app, outputFolder);

    const date = formatDate(new Date());
    const safeTitle = sanitizeFileName(sourceFile.basename);
    const outputPath = normalizePath(`${outputFolder}/${date} - AI Summary - ${safeTitle}.md`);
    const sourceLink = this.app.metadataCache.fileToLinktext(sourceFile, "", true);
    const noteContent = buildOutputNote({
      title: response.title || sourceFile.basename,
      sourceLink,
      model: response.model,
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
  summaryMarkdown: string;
}): string {
  return `# AI Summary - ${input.title}

- Generated at: ${formatIsoTimestamp(input.generatedAt)}
- Model: ${input.model}
- Source note: [[${input.sourceLink}]]

${input.summaryMarkdown}
`;
}
