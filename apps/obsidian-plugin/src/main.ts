import {
  App,
  ItemView,
  Modal,
  Notice,
  Plugin,
  PluginSettingTab,
  Setting,
  TAbstractFile,
  TFile,
  WorkspaceLeaf,
  normalizePath,
  requestUrl,
} from "obsidian";

const DASHBOARD_VIEW_TYPE = "notre-compagnon-dashboard";
const DEFAULT_TEMPLATE_FILE = "compte-rendu-standard-fr.md";
const DEFAULT_TEMPLATES_FOLDER = "Compagnon/Templates";
const DEFAULT_OUTPUT_FOLDER = "Compagnon/Comptes rendus";
const DEFAULT_MEETINGS_FOLDER = "Compagnon/Reunions";
const DEFAULT_RECORDINGS_FOLDER = "Compagnon/Enregistrements";
const DEFAULT_MODEL = "qwen2.5:14b";
const AUDIO_POLL_INTERVAL_MS = 3_000;
const AUDIO_POLL_TIMEOUT_MS = 30 * 60 * 1_000;
const DEFAULT_RECORDING_EXTENSION = ".webm";
const DEFAULT_RECORDING_MIME_TYPE = "audio/webm";
const SUPPORTED_AUDIO_EXTENSIONS = new Set([".wav", ".mp3", ".m4a", ".webm", ".ogg"]);
const FALLBACK_TEMPLATE = `# Compte rendu

## Resume executif

- 

## Decisions

- 

## Actions a suivre

- 

## Incertitudes

- Signaler les points flous ou manquants.
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

interface AudioJobQueuedResponsePayload {
  job_id: string;
  status: "queued";
}

interface JobStatusResponsePayload {
  job_id: string;
  status: "queued" | "processing" | "completed" | "failed";
  created_at: string;
  updated_at: string;
  error: string | null;
}

interface MeetingGenerateFromJobPayload {
  job_id: string;
  title: string;
  manual_notes: string;
  participants: string[];
  template: string;
  model: string;
}

interface MeetingGenerateFromJobResponsePayload {
  job_id: string;
  model: string;
  title: string;
  meeting_markdown: string;
  usage: {
    transcript_chars: number;
    manual_notes_chars: number;
    template_chars: number;
    participants_count: number;
  };
}

interface TemplateChoice {
  label: string;
  templateContent: string;
  sourcePath: string | null;
  description: string | null;
  language: string | null;
  type: string | null;
}

interface MeetingMetadata {
  title: string;
  manualNotes: string;
}

interface RecordingStartMetadata {
  title: string;
}

interface ActiveRecordingSession {
  title: string;
  notePath: string;
  startedAt: Date;
  mimeType: string;
  fileExtension: string;
  recorder: MediaRecorder;
  stream: MediaStream;
  chunks: BlobPart[];
}

interface RecordingStopResult {
  blob: Blob;
  title: string;
  notePath: string;
  startedAt: Date;
  mimeType: string;
  fileExtension: string;
}

interface PluginSettings {
  apiBaseUrl: string;
  apiToken: string;
  defaultModel: string;
  templatesFolder: string;
  outputFolder: string;
  meetingsFolder: string;
  recordingsFolder: string;
  transcriptionLanguage: "auto" | "fr" | "en";
  outputLanguage: "same_as_meeting" | "fr" | "en";
}

const DEFAULT_SETTINGS: PluginSettings = {
  apiBaseUrl: "",
  apiToken: "",
  defaultModel: DEFAULT_MODEL,
  templatesFolder: DEFAULT_TEMPLATES_FOLDER,
  outputFolder: DEFAULT_OUTPUT_FOLDER,
  meetingsFolder: DEFAULT_MEETINGS_FOLDER,
  recordingsFolder: DEFAULT_RECORDINGS_FOLDER,
  transcriptionLanguage: "auto",
  outputLanguage: "same_as_meeting",
};

export default class LocalAiPlatformPlugin extends Plugin {
  settings: PluginSettings = DEFAULT_SETTINGS;
  activeRecording: ActiveRecordingSession | null = null;

  async onload(): Promise<void> {
    await this.loadSettings();

    this.registerView(DASHBOARD_VIEW_TYPE, (leaf) => new NotreCompagnonDashboardView(leaf, this));
    this.addSettingTab(new LocalAiPlatformSettingTab(this.app, this));
    this.addCommand({
      id: "open-dashboard",
      name: "Notre Compagnon: Open dashboard",
      callback: async () => {
        await this.openDashboard();
      },
    });
    this.addCommand({
      id: "summarize-current-note",
      name: "Notre Compagnon: Summarize current note",
      callback: async () => {
        await this.summarizeCurrentNote();
      },
    });
    this.addCommand({
      id: "generate-meeting-minutes-from-audio-file",
      name: "Notre Compagnon: Generate minutes from audio file",
      callback: async () => {
        await this.generateMeetingMinutesFromAudioFile();
      },
    });
    this.addCommand({
      id: "start-meeting-recording",
      name: "Notre Compagnon: Start meeting recording",
      callback: async () => {
        await this.startMeetingRecording();
      },
    });
    this.addCommand({
      id: "stop-recording-and-generate-meeting-minutes",
      name: "Notre Compagnon: Stop recording and generate minutes",
      callback: async () => {
        await this.stopRecordingAndGenerateMeetingMinutes();
      },
    });
  }

  onunload(): void {
    this.app.workspace.detachLeavesOfType(DASHBOARD_VIEW_TYPE);
  }

  async openDashboard(): Promise<void> {
    const existingLeaf = this.app.workspace.getLeavesOfType(DASHBOARD_VIEW_TYPE)[0];
    if (existingLeaf) {
      this.app.workspace.revealLeaf(existingLeaf);
      return;
    }

    const leaf = this.app.workspace.getRightLeaf(false) ?? this.app.workspace.getLeaf(true);
    await leaf.setViewState({ type: DASHBOARD_VIEW_TYPE, active: true });
    this.app.workspace.revealLeaf(leaf);
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
      this.validateCoreSettings();

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
        template: this.prepareTemplateForRequest(templateChoice),
        model: this.getDefaultModel(),
      };

      new Notice("Notre Compagnon is generating the summary...");
      const result = await this.requestSummary(apiBaseUrl, apiToken, payload);
      const outputFile = await this.writeSummaryNote(activeFile, result, templateChoice);

      new Notice(`Summary created: ${outputFile.path}`);
    } catch (error) {
      this.showUserFacingError(error);
    }
  }

  async generateMeetingMinutesFromAudioFile(): Promise<void> {
    try {
      this.validateCoreSettings();

      const apiBaseUrl = this.getApiBaseUrl();
      const apiToken = this.getApiToken();
      const audioFile = await pickAudioFile();
      if (!audioFile) {
        throw new UserFacingError("No audio file selected.");
      }

      ensureSupportedAudioFile(audioFile.name);
      new Notice(`Uploading audio: ${audioFile.name}`);
      const queuedJob = await this.uploadAudio(apiBaseUrl, apiToken, audioFile);
      new Notice("Audio uploaded.");
      const completedJob = await this.pollAudioJob(apiBaseUrl, apiToken, queuedJob.job_id);
      if (completedJob.status !== "completed") {
        throw new UserFacingError("The audio job did not complete successfully.");
      }

      const metadata = await promptForMeetingMetadata(this.app, stripFileExtension(audioFile.name));
      const templateChoice = await this.chooseTemplate();
      new Notice("Generating minutes...");
      const result = await this.requestMeetingFromJob(apiBaseUrl, apiToken, {
        job_id: queuedJob.job_id,
        title: metadata.title,
        manual_notes: metadata.manualNotes,
        participants: [],
        template: this.prepareTemplateForRequest(templateChoice),
        model: this.getDefaultModel(),
      });

      const outputFile = await this.writeMeetingNote({
        response: result,
        templateChoice,
        sourceAudioName: audioFile.name,
      });
      new Notice(`Minutes created: ${outputFile.path}`);
    } catch (error) {
      this.showUserFacingError(error);
    }
  }

  async startMeetingRecording(): Promise<void> {
    try {
      this.validateRecordingSettings();
      this.ensureMediaRecorderAvailable();

      if (this.activeRecording) {
        throw new UserFacingError("A recording is already in progress.");
      }

      const metadata = await promptForRecordingTitle(this.app);
      new Notice("Reminder: inform participants before recording the meeting.", 8000);

      const stream = await this.requestMicrophoneStream();
      try {
        const recordingOptions = pickRecordingOptions();
        const recorder = new MediaRecorder(stream, recordingOptions.mimeType ? { mimeType: recordingOptions.mimeType } : undefined);
        const meetingsFolder = this.getMeetingsFolder();
        await ensureFolderExists(this.app, meetingsFolder);

        const startedAt = new Date();
        const fileBaseName = `${formatDateTimeForFile(startedAt)} - ${sanitizeFileName(metadata.title)}`;
        const notePath = normalizePath(`${meetingsFolder}/${fileBaseName}.md`);
        const noteContent = buildMeetingSourceNote({
          title: metadata.title,
          startedAt,
          recordingStatus: "in progress",
        });
        const noteFile = await createOrReplaceFile(this.app, notePath, noteContent);
        await this.app.workspace.getLeaf(true).openFile(noteFile);

        const chunks: BlobPart[] = [];
        recorder.addEventListener("dataavailable", (event: BlobEvent) => {
          if (event.data.size > 0) {
            chunks.push(event.data);
          }
        });

        recorder.start();
        this.activeRecording = {
          title: metadata.title,
          notePath: noteFile.path,
          startedAt,
          mimeType: recorder.mimeType || recordingOptions.mimeType || DEFAULT_RECORDING_MIME_TYPE,
          fileExtension: recordingOptions.fileExtension,
          recorder,
          stream,
          chunks,
        };

        new Notice(`Recording started: ${metadata.title}`, 6000);
      } catch (error) {
        stopMediaStream(stream);
        throw error;
      }
    } catch (error) {
      this.showUserFacingError(error);
    }
  }

  async stopRecordingAndGenerateMeetingMinutes(): Promise<void> {
    try {
      this.validateCoreSettings();
      this.getMeetingsFolder();
      this.getRecordingsFolder();

      if (!this.activeRecording) {
        throw new UserFacingError("No meeting recording is active.");
      }

      new Notice("Stopping recording...");
      const recording = await this.finishActiveRecording();
      if (recording.blob.size === 0) {
        throw new UserFacingError("The recording is empty.");
      }

      const savedAudio = await this.saveRecordingToVault(recording);
      new Notice(`Recording saved: ${savedAudio.file.path}`);
      const sourceNote = await this.completeMeetingSourceNote(recording, savedAudio.file);
      const manualNotes = await this.app.vault.read(sourceNote);
      const templateChoice = await this.chooseTemplate();

      const apiBaseUrl = this.getApiBaseUrl();
      const apiToken = this.getApiToken();
      new Notice(`Uploading audio: ${savedAudio.file.name}`);
      const uploadFile = createFileFromBlob(savedAudio.blob, savedAudio.file.name, recording.mimeType);
      const queuedJob = await this.uploadAudio(apiBaseUrl, apiToken, uploadFile);
      new Notice("Audio uploaded.");
      await this.pollAudioJob(apiBaseUrl, apiToken, queuedJob.job_id);

      new Notice("Generating minutes...");
      const result = await this.requestMeetingFromJob(apiBaseUrl, apiToken, {
        job_id: queuedJob.job_id,
        title: recording.title,
        manual_notes: manualNotes,
        participants: [],
        template: this.prepareTemplateForRequest(templateChoice),
        model: this.getDefaultModel(),
      });

      const outputFile = await this.writeMeetingNote({
        response: result,
        templateChoice,
        sourceAudioName: savedAudio.file.name,
        sourceNoteFile: sourceNote,
        sourceAudioFile: savedAudio.file,
        generatedAt: new Date(),
      });

      new Notice(`Minutes created: ${outputFile.path}`);
    } catch (error) {
      this.showUserFacingError(error);
    }
  }

  validateCoreSettings(): void {
    this.getApiBaseUrl();
    this.getApiToken();
    this.getDefaultModel();
    this.getOutputFolder();
  }

  validateRecordingSettings(): void {
    this.validateCoreSettings();
    this.getMeetingsFolder();
    this.getRecordingsFolder();
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

  getTemplatesFolder(): string {
    return normalizePath(this.settings.templatesFolder.trim() || DEFAULT_TEMPLATES_FOLDER);
  }

  getOutputFolder(): string {
    const value = this.settings.outputFolder.trim();
    if (!value) {
      throw new UserFacingError("Missing output folder.");
    }
    return normalizePath(value);
  }

  getMeetingsFolder(): string {
    const value = this.settings.meetingsFolder.trim();
    if (!value) {
      throw new UserFacingError("Missing meetings folder.");
    }
    return normalizePath(value);
  }

  getRecordingsFolder(): string {
    const value = this.settings.recordingsFolder.trim();
    if (!value) {
      throw new UserFacingError("Missing recordings folder.");
    }
    return normalizePath(value);
  }

  getOutputLanguage(): "same_as_meeting" | "fr" | "en" {
    return this.settings.outputLanguage || "same_as_meeting";
  }

  getTranscriptionLanguage(): "auto" | "fr" | "en" {
    return this.settings.transcriptionLanguage || "auto";
  }

  prepareTemplateForRequest(templateChoice: TemplateChoice): string {
    return [
      templateChoice.templateContent.trim(),
      buildTranscriptionLanguageHint(this.getTranscriptionLanguage()),
      buildLanguageInstruction(this.getOutputLanguage()),
    ].join("\n\n").trim();
  }

  getConfigurationStatus(): { label: string; isReady: boolean } {
    const missing: string[] = [];
    if (!this.settings.apiBaseUrl.trim()) missing.push("API Base URL");
    if (!this.settings.apiToken.trim()) missing.push("API Token");
    if (!this.settings.defaultModel.trim()) missing.push("Default model");
    if (!this.settings.outputFolder.trim()) missing.push("Output folder");

    if (missing.length > 0) {
      return { label: `Missing: ${missing.join(", ")}`, isReady: false };
    }
    return { label: "Ready", isReady: true };
  }

  async openConfiguredFolder(folderPath: string): Promise<void> {
    const normalized = normalizePath(folderPath);
    await ensureFolderExists(this.app, normalized);
    const folder = this.app.vault.getAbstractFileByPath(normalized);
    if (!folder) {
      throw new UserFacingError(`Folder unavailable: ${normalized}`);
    }

    const firstMarkdownFile = collectMarkdownFiles(folder).sort((left, right) => left.path.localeCompare(right.path))[0];
    if (firstMarkdownFile) {
      await this.app.workspace.getLeaf(true).openFile(firstMarkdownFile);
      return;
    }

    new Notice(`Folder ready: ${normalized}`);
  }

  ensureMediaRecorderAvailable(): void {
    if (typeof MediaRecorder === "undefined") {
      throw new UserFacingError("MediaRecorder is not supported in this Obsidian environment.");
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new UserFacingError("Microphone access is not available in this Obsidian environment.");
    }
  }

  async requestMicrophoneStream(): Promise<MediaStream> {
    try {
      return await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      throw new UserFacingError("Microphone permission was denied or unavailable.");
    }
  }

  async chooseTemplate(): Promise<TemplateChoice> {
    const availableTemplates = await this.listTemplateChoices();
    return chooseTemplateWithModal(this.app, availableTemplates);
  }

  async listTemplateChoices(): Promise<TemplateChoice[]> {
    const choices: TemplateChoice[] = [
      {
        label: "Built-in default template",
        templateContent: FALLBACK_TEMPLATE,
        sourcePath: null,
        description: "Fallback template bundled with Notre Compagnon.",
        language: "fr",
        type: "meeting",
      },
    ];
    const templatesFolder = this.getTemplatesFolder();
    if (!templatesFolder) {
      return choices;
    }

    const folder = this.app.vault.getAbstractFileByPath(templatesFolder);
    if (!folder) {
      return choices;
    }

    const markdownFiles = collectMarkdownFiles(folder).sort((left, right) => left.path.localeCompare(right.path));
    for (const file of markdownFiles) {
      const content = await this.app.vault.read(file);
      const parsedTemplate = parseTemplateContent(content);
      choices.push({
        label: parsedTemplate.metadata.name || file.basename,
        templateContent: parsedTemplate.body.trim() ? parsedTemplate.body : FALLBACK_TEMPLATE,
        sourcePath: file.path,
        description: parsedTemplate.metadata.description,
        language: parsedTemplate.metadata.language,
        type: parsedTemplate.metadata.type,
      });
    }

    return choices;
  }

  async testConnection(): Promise<void> {
    try {
      const apiBaseUrl = this.getApiBaseUrl();
      const apiToken = this.getApiToken();
      new Notice("Testing AI Gateway connection...");

      const responseText = await this.performJsonRequest({
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
      this.showUserFacingError(error);
    }
  }

  async requestSummary(apiBaseUrl: string, apiToken: string, payload: SummarizeRequestPayload): Promise<SummarizeResponsePayload> {
    const responseText = await this.performJsonRequest({
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

  async requestMeetingFromJob(
    apiBaseUrl: string,
    apiToken: string,
    payload: MeetingGenerateFromJobPayload,
  ): Promise<MeetingGenerateFromJobResponsePayload> {
    const responseText = await this.performJsonRequest({
      apiBaseUrl,
      apiToken,
      path: "/v1/meetings/generate-from-job",
      method: "POST",
      body: JSON.stringify(payload),
      errorMap: {
        401: "The API token is invalid or expired.",
        403: "The token is missing meetings:generate or the model is not allowed.",
        404: "The transcription job was not found.",
        409: "The transcription job is not ready or failed.",
        413: "The notes, participants, or template exceed the AI Gateway limits.",
        422: "The meeting request is invalid.",
        500: "The stored transcription result is invalid.",
        502: "The AI Gateway returned an invalid upstream response.",
        503: "The AI Gateway meeting generation service is unavailable.",
      },
      unavailableMessage: "The AI Gateway is unreachable.",
      invalidJsonMessage: "The AI Gateway returned an invalid meeting response.",
    });

    return this.parseMeetingGenerateFromJobResponse(responseText);
  }

  async uploadAudio(apiBaseUrl: string, apiToken: string, audioFile: File): Promise<AudioJobQueuedResponsePayload> {
    try {
      const formData = new FormData();
      formData.append("file", audioFile);

      const response = await fetch(`${apiBaseUrl}/v1/audio/transcribe`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${apiToken}`,
        },
        body: formData,
      });

      const responseText = await response.text();
      if (!response.ok) {
        this.throwApiError(response.status, responseText, {
          400: "The audio upload was rejected.",
          401: "The API token is invalid or expired.",
          403: "The token is missing the audio:transcribe scope.",
          409: "The audio job could not be created.",
          413: "The audio file is too large for the AI Gateway limits.",
          422: "The audio file extension is not supported or the upload is invalid.",
          502: "The AI Gateway returned an invalid upstream response.",
          503: "The AI Gateway is unavailable.",
        });
      }

      return this.parseAudioJobQueuedResponse(responseText);
    } catch (error) {
      if (error instanceof UserFacingError) {
        throw error;
      }
      throw this.normalizeNetworkError(error, "The audio upload failed.");
    }
  }

  async pollAudioJob(apiBaseUrl: string, apiToken: string, jobId: string): Promise<JobStatusResponsePayload> {
    const startedAt = Date.now();
    let lastStatus: JobStatusResponsePayload["status"] | null = null;

    while (Date.now() - startedAt < AUDIO_POLL_TIMEOUT_MS) {
      const responseText = await this.performJsonRequest({
        apiBaseUrl,
        apiToken,
        path: `/v1/jobs/${jobId}`,
        method: "GET",
        errorMap: {
          401: "The API token is invalid or expired.",
          403: "The token is not allowed to access this job.",
          404: "The audio job was not found.",
          503: "The AI Gateway is unavailable.",
        },
        unavailableMessage: "The AI Gateway is unreachable.",
        invalidJsonMessage: "The AI Gateway returned an invalid job status response.",
      });

      const payload = this.parseJobStatusResponse(responseText);
      if (payload.status !== lastStatus) {
        lastStatus = payload.status;
        new Notice(formatJobStatusNotice(payload.status), 4000);
      }

      if (payload.status === "completed") {
        return payload;
      }
      if (payload.status === "failed") {
        throw new UserFacingError(payload.error || "The audio transcription job failed.");
      }

      await sleep(AUDIO_POLL_INTERVAL_MS);
    }

    throw new UserFacingError("Audio transcription timed out.");
  }

  async performJsonRequest(input: {
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
      throw this.normalizeNetworkError(error, input.unavailableMessage, input.invalidJsonMessage);
    }
  }

  async finishActiveRecording(): Promise<RecordingStopResult> {
    const session = this.activeRecording;
    if (!session) {
      throw new UserFacingError("No meeting recording is active.");
    }
    this.activeRecording = null;

    const recorder = session.recorder;
    const blob = await new Promise<Blob>((resolve, reject) => {
      const handleStop = (): void => {
        cleanup();
        resolve(new Blob(session.chunks, { type: session.mimeType }));
      };
      const handleError = (): void => {
        cleanup();
        reject(new UserFacingError("The recording could not be stopped cleanly."));
      };
      const cleanup = (): void => {
        recorder.removeEventListener("stop", handleStop);
        recorder.removeEventListener("error", handleError);
        stopMediaStream(session.stream);
      };

      recorder.addEventListener("stop", handleStop, { once: true });
      recorder.addEventListener("error", handleError, { once: true });
      recorder.stop();
    });

    return {
      blob,
      title: session.title,
      notePath: session.notePath,
      startedAt: session.startedAt,
      mimeType: session.mimeType,
      fileExtension: session.fileExtension,
    };
  }

  async saveRecordingToVault(recording: RecordingStopResult): Promise<{ file: TFile; blob: Blob }> {
    const recordingsFolder = this.getRecordingsFolder();
    await ensureFolderExists(this.app, recordingsFolder);

    const fileBaseName = `${formatDateTimeForFile(recording.startedAt)} - ${sanitizeFileName(recording.title)}`;
    const filePath = normalizePath(`${recordingsFolder}/${fileBaseName}${recording.fileExtension}`);
    const arrayBuffer = await recording.blob.arrayBuffer();

    try {
      await this.app.vault.adapter.writeBinary(filePath, arrayBuffer);
    } catch {
      throw new UserFacingError("Failed to save the recorded audio in the vault.");
    }

    const savedFile = this.app.vault.getAbstractFileByPath(filePath);
    if (!(savedFile instanceof TFile)) {
      throw new UserFacingError("The recorded audio could not be found in the vault after saving.");
    }

    return { file: savedFile, blob: recording.blob };
  }

  async completeMeetingSourceNote(recording: RecordingStopResult, audioFile: TFile): Promise<TFile> {
    const sourceNote = this.app.vault.getAbstractFileByPath(recording.notePath);
    if (!(sourceNote instanceof TFile)) {
      throw new UserFacingError("The meeting note could not be found.");
    }

    const currentContent = await this.app.vault.read(sourceNote);
    const audioLink = this.app.metadataCache.fileToLinktext(audioFile, sourceNote.path, true);
    const nextContent = markMeetingSourceNoteCompleted(currentContent, audioLink);
    await this.app.vault.modify(sourceNote, nextContent);
    return sourceNote;
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
    const outputPath = normalizePath(`${outputFolder}/${date} - Notre Compagnon Summary - ${safeTitle}.md`);
    const sourceLink = this.app.metadataCache.fileToLinktext(sourceFile, "", true);
    const noteContent = buildSummaryNote({
      title: response.title || sourceFile.basename,
      sourceLink,
      model: response.model,
      templateLabel: templateChoice.label,
      outputLanguage: this.getOutputLanguage(),
      summaryMarkdown: response.summary_markdown,
      generatedAt: new Date(),
    });

    return createOrReplaceFile(this.app, outputPath, noteContent);
  }

  async writeMeetingNote(input: {
    response: MeetingGenerateFromJobResponsePayload;
    templateChoice: TemplateChoice;
    sourceAudioName: string;
    sourceNoteFile?: TFile;
    sourceAudioFile?: TFile;
    generatedAt?: Date;
  }): Promise<TFile> {
    const outputFolder = this.getOutputFolder();
    await ensureFolderExists(this.app, outputFolder);

    const generatedAt = input.generatedAt ?? new Date();
    const outputPath = normalizePath(
      `${outputFolder}/${formatDateTimeForFile(generatedAt)} - Meeting Minutes - ${sanitizeFileName(input.response.title)}.md`,
    );

    const sourceMeetingLink = input.sourceNoteFile
      ? this.app.metadataCache.fileToLinktext(input.sourceNoteFile, "", true)
      : null;
    const sourceAudioLink = input.sourceAudioFile
      ? this.app.metadataCache.fileToLinktext(input.sourceAudioFile, "", true)
      : null;
    const noteContent = buildMeetingNote({
      title: input.response.title,
      generatedAt,
      model: input.response.model,
      templateLabel: input.templateChoice.label,
      outputLanguage: this.getOutputLanguage(),
      jobId: input.response.job_id,
      audioFileName: input.sourceAudioName,
      sourceMeetingLink,
      sourceAudioLink,
      meetingMarkdown: input.response.meeting_markdown,
    });

    return createOrReplaceFile(this.app, outputPath, noteContent);
  }

  normalizeNetworkError(error: unknown, unavailableMessage: string, invalidJsonMessage = "The AI Gateway returned invalid JSON."): UserFacingError {
    if (error instanceof Error && error.message) {
      if (error.message.includes("ECONNREFUSED") || error.message.includes("ENOTFOUND")) {
        return new UserFacingError(`${unavailableMessage} Check the API Base URL and server availability.`);
      }
      if (error.message.includes("Certificate") || error.message.includes("SSL")) {
        return new UserFacingError("TLS validation failed. Use HTTPS with a valid certificate, or http://127.0.0.1 only for local development.");
      }
      if (error.message.includes("Unexpected token") || error.message.includes("JSON")) {
        return new UserFacingError(invalidJsonMessage);
      }
    }
    return new UserFacingError(`${unavailableMessage} The request failed before a valid response was received.`);
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
    const parsed = this.parseJson(responseText, "The AI Gateway returned an invalid JSON response.");
    if (!isSummarizeResponsePayload(parsed)) {
      throw new UserFacingError("The AI Gateway returned an invalid summary payload.");
    }
    return parsed;
  }

  parseModelsResponse(responseText: string): ModelsResponsePayload {
    const parsed = this.parseJson(responseText, "The AI Gateway returned an invalid models response.");
    if (!isModelsResponsePayload(parsed)) {
      throw new UserFacingError("The AI Gateway returned an invalid models response.");
    }
    return parsed;
  }

  parseAudioJobQueuedResponse(responseText: string): AudioJobQueuedResponsePayload {
    const parsed = this.parseJson(responseText, "The AI Gateway returned an invalid audio job response.");
    if (!isAudioJobQueuedResponsePayload(parsed)) {
      throw new UserFacingError("The AI Gateway returned an invalid audio job response.");
    }
    return parsed;
  }

  parseJobStatusResponse(responseText: string): JobStatusResponsePayload {
    const parsed = this.parseJson(responseText, "The AI Gateway returned an invalid job status response.");
    if (!isJobStatusResponsePayload(parsed)) {
      throw new UserFacingError("The AI Gateway returned an invalid job status response.");
    }
    return parsed;
  }

  parseMeetingGenerateFromJobResponse(responseText: string): MeetingGenerateFromJobResponsePayload {
    const parsed = this.parseJson(responseText, "The AI Gateway returned an invalid meeting response.");
    if (!isMeetingGenerateFromJobResponsePayload(parsed)) {
      throw new UserFacingError("The AI Gateway returned an invalid meeting response.");
    }
    return parsed;
  }

  parseJson<T>(responseText: string, invalidMessage: string): T {
    try {
      return JSON.parse(responseText) as T;
    } catch {
      throw new UserFacingError(invalidMessage);
    }
  }

  showUserFacingError(error: unknown): void {
    const message = error instanceof Error ? error.message : "An unexpected error occurred.";
    new Notice(message, 8000);
  }
}

class NotreCompagnonDashboardView extends ItemView {
  private readonly plugin: LocalAiPlatformPlugin;

  constructor(leaf: WorkspaceLeaf, plugin: LocalAiPlatformPlugin) {
    super(leaf);
    this.plugin = plugin;
  }

  getViewType(): string {
    return DASHBOARD_VIEW_TYPE;
  }

  getDisplayText(): string {
    return "Notre Compagnon";
  }

  async onOpen(): Promise<void> {
    this.render();
  }

  async onClose(): Promise<void> {
    this.contentEl.empty();
  }

  render(): void {
    const container = this.contentEl;
    container.empty();
    container.addClass("notre-compagnon-dashboard");
    container.createEl("h2", { text: "Notre Compagnon" });

    const status = this.plugin.getConfigurationStatus();
    new Setting(container)
      .setName("Configuration")
      .setDesc(status.label)
      .addButton((button) =>
        button.setButtonText("Test connection").onClick(async () => {
          await this.plugin.testConnection();
        }),
      );

    new Setting(container).setName("API Base URL").setDesc(this.plugin.settings.apiBaseUrl || "Not configured");
    new Setting(container).setName("Default model").setDesc(this.plugin.settings.defaultModel || "Not configured");
    new Setting(container).setName("Transcription language").setDesc(formatTranscriptionLanguageLabel(this.plugin.getTranscriptionLanguage()));
    new Setting(container).setName("Output language").setDesc(formatOutputLanguageLabel(this.plugin.getOutputLanguage()));

    container.createEl("h3", { text: "Workflows" });
    this.addActionButton(container, "Start meeting recording", async () => this.plugin.startMeetingRecording());
    this.addActionButton(container, "Stop recording and generate minutes", async () => this.plugin.stopRecordingAndGenerateMeetingMinutes());
    this.addActionButton(container, "Summarize current note", async () => this.plugin.summarizeCurrentNote());
    this.addActionButton(container, "Generate minutes from audio file", async () => this.plugin.generateMeetingMinutesFromAudioFile());

    container.createEl("h3", { text: "Folders" });
    this.addActionButton(container, "Open meetings", async () => this.plugin.openConfiguredFolder(this.plugin.getMeetingsFolder()));
    this.addActionButton(container, "Open recordings", async () => this.plugin.openConfiguredFolder(this.plugin.getRecordingsFolder()));
    this.addActionButton(container, "Open summaries", async () => this.plugin.openConfiguredFolder(this.plugin.getOutputFolder()));
    this.addActionButton(container, "Open templates", async () => this.plugin.openConfiguredFolder(this.plugin.getTemplatesFolder()));
  }

  private addActionButton(container: HTMLElement, label: string, action: () => Promise<void>): void {
    new Setting(container).setName(label).addButton((button) =>
      button.setButtonText(label).onClick(async () => {
        try {
          await action();
        } catch (error) {
          this.plugin.showUserFacingError(error);
        } finally {
          this.render();
        }
      }),
    );
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

    containerEl.createEl("h2", { text: "Notre Compagnon" });

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
      .setDesc("Used for note summaries and meeting generation.")
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
      .setDesc(`Vault folder containing Markdown templates such as ${DEFAULT_TEMPLATE_FILE}. Falls back to the built-in template if the folder is empty.`)
      .addText((text) =>
        text
          .setPlaceholder(DEFAULT_TEMPLATES_FOLDER)
          .setValue(this.plugin.settings.templatesFolder)
          .onChange(async (value) => {
            this.plugin.settings.templatesFolder = value.trim() || DEFAULT_TEMPLATES_FOLDER;
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Transcription language")
      .setDesc("Language hint for the meeting workflow. Current gateway transcription is configured server-side; this setting documents the intended meeting language.")
      .addDropdown((dropdown) =>
        dropdown
          .addOption("auto", "Auto")
          .addOption("fr", "French")
          .addOption("en", "English")
          .setValue(this.plugin.settings.transcriptionLanguage)
          .onChange(async (value) => {
            this.plugin.settings.transcriptionLanguage = value as PluginSettings["transcriptionLanguage"];
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Output language")
      .setDesc("Adds a language instruction to the template sent to the gateway.")
      .addDropdown((dropdown) =>
        dropdown
          .addOption("same_as_meeting", "Same as meeting")
          .addOption("fr", "French")
          .addOption("en", "English")
          .setValue(this.plugin.settings.outputLanguage)
          .onChange(async (value) => {
            this.plugin.settings.outputLanguage = value as PluginSettings["outputLanguage"];
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Meetings folder")
      .setDesc("Meeting notes created during microphone recording are stored here.")
      .addText((text) =>
        text
          .setPlaceholder(DEFAULT_MEETINGS_FOLDER)
          .setValue(this.plugin.settings.meetingsFolder)
          .onChange(async (value) => {
            this.plugin.settings.meetingsFolder = value.trim() || DEFAULT_MEETINGS_FOLDER;
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Recordings folder")
      .setDesc("Recorded microphone audio files are saved in the vault here.")
      .addText((text) =>
        text
          .setPlaceholder(DEFAULT_RECORDINGS_FOLDER)
          .setValue(this.plugin.settings.recordingsFolder)
          .onChange(async (value) => {
            this.plugin.settings.recordingsFolder = value.trim() || DEFAULT_RECORDINGS_FOLDER;
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Output folder")
      .setDesc("Generated summaries and meeting minutes are written here.")
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
    contentEl.createEl("p", { text: "Pick the Markdown template to send with this request." });

    for (const choice of this.choices) {
      const setting = new Setting(contentEl).setName(choice.label);
      const details = [
        choice.description,
        choice.language ? `Language: ${choice.language}` : null,
        choice.type ? `Type: ${choice.type}` : null,
        choice.sourcePath ?? "Uses the built-in fallback template.",
      ].filter((item): item is string => item !== null && item.trim().length > 0);
      setting.setDesc(details.join(" | "));
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

class MeetingMetadataModal extends Modal {
  private readonly onSubmit: (value: MeetingMetadata) => void;
  private titleValue: string;
  private manualNotesValue = "";

  constructor(app: App, initialTitle: string, onSubmit: (value: MeetingMetadata) => void) {
    super(app);
    this.titleValue = initialTitle;
    this.onSubmit = onSubmit;
  }

  onOpen(): void {
    const { contentEl } = this;
    contentEl.empty();
    contentEl.createEl("h2", { text: "Meeting details" });

    new Setting(contentEl)
      .setName("Meeting title")
      .setDesc("Required for the generated meeting minutes.")
      .addText((text) =>
        text.setValue(this.titleValue).onChange((value) => {
          this.titleValue = value;
        }),
      );

    contentEl.createEl("p", { text: "Manual notes (optional)" });
    const textarea = contentEl.createEl("textarea");
    textarea.rows = 10;
    textarea.style.width = "100%";
    textarea.addEventListener("input", () => {
      this.manualNotesValue = textarea.value;
    });

    new Setting(contentEl).addButton((button) =>
      button.setButtonText("Generate minutes").setCta().onClick(() => {
        const trimmedTitle = this.titleValue.trim();
        if (!trimmedTitle) {
          new Notice("Meeting title is required.", 5000);
          return;
        }
        this.onSubmit({
          title: trimmedTitle,
          manualNotes: this.manualNotesValue.trim(),
        });
        this.close();
      }),
    );
  }

  onClose(): void {
    this.contentEl.empty();
  }
}

class RecordingTitleModal extends Modal {
  private readonly onSubmit: (value: RecordingStartMetadata) => void;
  private titleValue = "";

  constructor(app: App, onSubmit: (value: RecordingStartMetadata) => void) {
    super(app);
    this.onSubmit = onSubmit;
  }

  onOpen(): void {
    const { contentEl } = this;
    contentEl.empty();
    contentEl.createEl("h2", { text: "Start meeting recording" });
    contentEl.createEl("p", {
      text: "This MVP records microphone audio only. Make sure participants know the meeting is being recorded.",
    });

    new Setting(contentEl)
      .setName("Meeting title")
      .setDesc("Used for the meeting note, audio file, and final minutes.")
      .addText((text) =>
        text.setPlaceholder("Project sync").onChange((value) => {
          this.titleValue = value;
        }),
      );

    new Setting(contentEl).addButton((button) =>
      button.setButtonText("Start recording").setCta().onClick(() => {
        const trimmedTitle = this.titleValue.trim();
        if (!trimmedTitle) {
          new Notice("Meeting title is required.", 5000);
          return;
        }
        this.onSubmit({ title: trimmedTitle });
        this.close();
      }),
    );
  }

  onClose(): void {
    this.contentEl.empty();
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

function isAudioJobQueuedResponsePayload(value: unknown): value is AudioJobQueuedResponsePayload {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const candidate = value as Partial<AudioJobQueuedResponsePayload>;
  return typeof candidate.job_id === "string" && candidate.status === "queued";
}

function isJobStatusResponsePayload(value: unknown): value is JobStatusResponsePayload {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const candidate = value as Partial<JobStatusResponsePayload>;
  return (
    typeof candidate.job_id === "string" &&
    typeof candidate.status === "string" &&
    typeof candidate.created_at === "string" &&
    typeof candidate.updated_at === "string" &&
    (typeof candidate.error === "string" || candidate.error === null)
  );
}

function isMeetingGenerateFromJobResponsePayload(value: unknown): value is MeetingGenerateFromJobResponsePayload {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const candidate = value as Partial<MeetingGenerateFromJobResponsePayload>;
  return (
    typeof candidate.job_id === "string" &&
    typeof candidate.model === "string" &&
    typeof candidate.title === "string" &&
    typeof candidate.meeting_markdown === "string" &&
    typeof candidate.usage === "object" &&
    candidate.usage !== null &&
    typeof candidate.usage.transcript_chars === "number" &&
    typeof candidate.usage.manual_notes_chars === "number" &&
    typeof candidate.usage.template_chars === "number" &&
    typeof candidate.usage.participants_count === "number"
  );
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

async function createOrReplaceFile(app: App, outputPath: string, contents: string): Promise<TFile> {
  const existing = app.vault.getAbstractFileByPath(outputPath);
  if (existing instanceof TFile) {
    await app.vault.modify(existing, contents);
    return existing;
  }
  return app.vault.create(outputPath, contents);
}

function formatDate(date: Date): string {
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function formatDateTimeForFile(date: Date): string {
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  const hours = `${date.getHours()}`.padStart(2, "0");
  const minutes = `${date.getMinutes()}`.padStart(2, "0");
  return `${year}-${month}-${day} ${hours}-${minutes}`;
}

function formatIsoTimestamp(date: Date): string {
  return date.toISOString();
}

function sanitizeFileName(input: string): string {
  return input.replace(/[\\/:*?"<>|]/g, "-").trim() || "Untitled";
}

function parseTemplateContent(content: string): {
  metadata: { name: string | null; language: string | null; type: string | null; description: string | null };
  body: string;
} {
  const normalized = content.replace(/^\uFEFF/, "");
  if (!normalized.startsWith("---\n")) {
    return {
      metadata: { name: null, language: null, type: null, description: null },
      body: normalized,
    };
  }

  const closingIndex = normalized.indexOf("\n---", 4);
  if (closingIndex < 0) {
    return {
      metadata: { name: null, language: null, type: null, description: null },
      body: normalized,
    };
  }

  const frontmatter = normalized.slice(4, closingIndex);
  const body = normalized.slice(closingIndex + 4).replace(/^\r?\n/, "");
  const metadata = { name: null, language: null, type: null, description: null } as {
    name: string | null;
    language: string | null;
    type: string | null;
    description: string | null;
  };

  for (const line of frontmatter.split(/\r?\n/)) {
    const separatorIndex = line.indexOf(":");
    if (separatorIndex < 0) {
      continue;
    }
    const key = line.slice(0, separatorIndex).trim();
    const value = line.slice(separatorIndex + 1).trim().replace(/^["']|["']$/g, "");
    if (key === "name" || key === "language" || key === "type" || key === "description") {
      metadata[key] = value || null;
    }
  }

  return { metadata, body };
}

function buildLanguageInstruction(outputLanguage: PluginSettings["outputLanguage"]): string {
  if (outputLanguage === "fr") {
    return [
      "## Consigne de langue",
      "Repondre en francais.",
      "Conserver les noms propres, noms de produits et acronymes sans traduction abusive.",
    ].join("\n");
  }
  if (outputLanguage === "en") {
    return [
      "## Language instruction",
      "Answer in English.",
      "Keep proper nouns, product names, and acronyms unchanged unless a standard translation is obvious.",
    ].join("\n");
  }
  return [
    "## Language instruction",
    "Detect the main meeting language and answer in that language.",
    "If the source is bilingual, preserve proper nouns and acronyms without abusive translation.",
  ].join("\n");
}

function buildTranscriptionLanguageHint(transcriptionLanguage: PluginSettings["transcriptionLanguage"]): string {
  if (transcriptionLanguage === "fr") {
    return "## Transcription language hint\nThe meeting transcription is expected to be mainly in French.";
  }
  if (transcriptionLanguage === "en") {
    return "## Transcription language hint\nThe meeting transcription is expected to be mainly in English.";
  }
  return "## Transcription language hint\nDetect the transcription language from the provided meeting material.";
}

function formatOutputLanguageLabel(outputLanguage: string): string {
  if (outputLanguage === "fr") return "French";
  if (outputLanguage === "en") return "English";
  return "Same as meeting";
}

function formatTranscriptionLanguageLabel(transcriptionLanguage: string): string {
  if (transcriptionLanguage === "fr") return "French";
  if (transcriptionLanguage === "en") return "English";
  return "Auto";
}

function formatJobStatusNotice(status: JobStatusResponsePayload["status"]): string {
  if (status === "queued") return "Transcription queued.";
  if (status === "processing") return "Transcription processing.";
  if (status === "completed") return "Transcription completed.";
  return "Transcription failed.";
}

function buildSummaryNote(input: {
  title: string;
  generatedAt: Date;
  model: string;
  sourceLink: string;
  templateLabel: string;
  outputLanguage: string;
  summaryMarkdown: string;
}): string {
  return `# Notre Compagnon - Summary - ${input.title}

- Generated at: ${formatIsoTimestamp(input.generatedAt)}
- Model: ${input.model}
- Template: ${input.templateLabel}
- Output language: ${formatOutputLanguageLabel(input.outputLanguage)}
- Source note: [[${input.sourceLink}]]

${input.summaryMarkdown}
`;
}

function buildMeetingSourceNote(input: {
  title: string;
  startedAt: Date;
  recordingStatus: "in progress" | "completed";
}): string {
  return `# ${input.title}

Date: ${formatIsoTimestamp(input.startedAt)}
Recording status: ${input.recordingStatus}

## Notes manuelles

`;
}

function markMeetingSourceNoteCompleted(currentContent: string, audioLink: string): string {
  const updatedStatus = currentContent.includes("Recording status: in progress")
    ? currentContent.replace("Recording status: in progress", "Recording status: completed")
    : `${currentContent.trimEnd()}\nRecording status: completed\n`;

  if (updatedStatus.includes("Audio file:")) {
    return updatedStatus;
  }

  const insertion = `Audio file: [[${audioLink}]]`;
  const lines = updatedStatus.split("\n");
  const statusIndex = lines.findIndex((line) => line.startsWith("Recording status:"));
  if (statusIndex >= 0) {
    lines.splice(statusIndex + 1, 0, insertion);
    return `${lines.join("\n").trimEnd()}\n`;
  }

  return `${updatedStatus.trimEnd()}\n${insertion}\n`;
}

function buildMeetingNote(input: {
  title: string;
  generatedAt: Date;
  model: string;
  templateLabel: string;
  outputLanguage: string;
  jobId: string;
  audioFileName: string;
  sourceMeetingLink: string | null;
  sourceAudioLink: string | null;
  meetingMarkdown: string;
}): string {
  const metadataLines = [
    `- Generated at: ${formatIsoTimestamp(input.generatedAt)}`,
    `- Model: ${input.model}`,
    `- Template: ${input.templateLabel}`,
    `- Output language: ${formatOutputLanguageLabel(input.outputLanguage)}`,
    `- Job ID: ${input.jobId}`,
    input.sourceMeetingLink ? `- Source meeting note: [[${input.sourceMeetingLink}]]` : null,
    input.sourceAudioLink ? `- Source audio file: [[${input.sourceAudioLink}]]` : `- Source audio file: ${input.audioFileName}`,
  ].filter((line): line is string => line !== null);

  return `# Notre Compagnon - Meeting Minutes - ${input.title}

${metadataLines.join("\n")}

${input.meetingMarkdown}
`;
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
        reject(new UserFacingError("Action cancelled."));
      }
    };

    modal.open();
  });
}

function promptForMeetingMetadata(app: App, initialTitle: string): Promise<MeetingMetadata> {
  return new Promise((resolve, reject) => {
    let submitted = false;
    const modal = new MeetingMetadataModal(app, initialTitle, (value) => {
      submitted = true;
      resolve(value);
    });

    const originalOnClose = modal.onClose.bind(modal);
    modal.onClose = () => {
      originalOnClose();
      if (!submitted) {
        reject(new UserFacingError("Action cancelled."));
      }
    };
    modal.open();
  });
}

function promptForRecordingTitle(app: App): Promise<RecordingStartMetadata> {
  return new Promise((resolve, reject) => {
    let submitted = false;
    const modal = new RecordingTitleModal(app, (value) => {
      submitted = true;
      resolve(value);
    });

    const originalOnClose = modal.onClose.bind(modal);
    modal.onClose = () => {
      originalOnClose();
      if (!submitted) {
        reject(new UserFacingError("Action cancelled."));
      }
    };
    modal.open();
  });
}

function pickAudioFile(): Promise<File | null> {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = Array.from(SUPPORTED_AUDIO_EXTENSIONS).join(",");
    input.onchange = () => {
      resolve(input.files?.[0] ?? null);
    };
    input.click();
  });
}

function ensureSupportedAudioFile(fileName: string): void {
  const extension = getFileExtension(fileName);
  if (!SUPPORTED_AUDIO_EXTENSIONS.has(extension)) {
    throw new UserFacingError("Unsupported audio extension. Use .wav, .mp3, .m4a, .webm, or .ogg.");
  }
}

function getFileExtension(fileName: string): string {
  const dotIndex = fileName.lastIndexOf(".");
  return dotIndex >= 0 ? fileName.slice(dotIndex).toLowerCase() : "";
}

function stripFileExtension(fileName: string): string {
  const dotIndex = fileName.lastIndexOf(".");
  return dotIndex > 0 ? fileName.slice(0, dotIndex) : fileName;
}

function sleep(durationMs: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, durationMs));
}

function pickRecordingOptions(): { mimeType: string; fileExtension: string } {
  const candidates = [
    { mimeType: "audio/webm;codecs=opus", fileExtension: ".webm" },
    { mimeType: "audio/webm", fileExtension: ".webm" },
    { mimeType: "audio/ogg;codecs=opus", fileExtension: ".ogg" },
    { mimeType: "audio/ogg", fileExtension: ".ogg" },
    { mimeType: DEFAULT_RECORDING_MIME_TYPE, fileExtension: DEFAULT_RECORDING_EXTENSION },
  ];

  for (const candidate of candidates) {
    if (typeof MediaRecorder.isTypeSupported !== "function" || MediaRecorder.isTypeSupported(candidate.mimeType)) {
      return candidate;
    }
  }

  return {
    mimeType: DEFAULT_RECORDING_MIME_TYPE,
    fileExtension: DEFAULT_RECORDING_EXTENSION,
  };
}

function stopMediaStream(stream: MediaStream): void {
  for (const track of stream.getTracks()) {
    track.stop();
  }
}

function createFileFromBlob(blob: Blob, name: string, mimeType: string): File {
  return new File([blob], name, { type: mimeType });
}
