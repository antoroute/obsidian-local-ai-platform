import {
  App,
  Editor,
  ItemView,
  MarkdownRenderer,
  MarkdownView,
  Menu,
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
const DEFAULT_TEMPLATES_FOLDER = "Note Compagnon/Templates";
const DEFAULT_OUTPUT_FOLDER = "Note Compagnon/Comptes rendus";
const DEFAULT_MEETINGS_FOLDER = "Note Compagnon/Reunions";
const DEFAULT_RECORDINGS_FOLDER = "Note Compagnon/Enregistrements";
const DEFAULT_MODEL = "qwen2.5:14b";
const AUDIO_POLL_INTERVAL_MS = 3_000;
const AUDIO_POLL_TIMEOUT_MS = 30 * 60 * 1_000;
const DEFAULT_RECORDING_EXTENSION = ".webm";
const DEFAULT_RECORDING_MIME_TYPE = "audio/webm";
const SUPPORTED_AUDIO_EXTENSIONS = new Set([".wav", ".mp3", ".m4a", ".webm", ".ogg"]);
type TemplateInstallSet = "minimal" | "fr" | "en" | "all";
type TemplateGroup = "meeting_note" | "meeting_summary" | "actions" | "technical" | "client" | "other";
type AssistantResponseMode = "simple" | "current_note" | "vault";
type RecordingSource = "microphone_only" | "computer_audio_only" | "microphone_plus_computer_audio" | "experimental_system_capture" | "selected_audio_input";
type RecordingSourceUsed =
  | "microphone_only"
  | "computer_audio_only"
  | "microphone_plus_computer_audio"
  | "experimental_system_capture"
  | "experimental_system_audio_unavailable"
  | "selected_audio_input";
const MINIMAL_RECOMMENDED_TEMPLATE_FILES = new Set(["meeting-note-fr.md", "compte-rendu-reunion-fr.md", "meeting-note-en.md", "meeting-minutes-en.md"]);
const RECOMMENDED_TEMPLATES: Array<{ fileName: string; content: string; language: "fr" | "en"; minimal: boolean }> = [
  {
    fileName: "meeting-note-fr.md",
    language: "fr",
    minimal: true,
    content: `---
name: Note de reunion
language: fr
type: meeting
description: Note source pour une reunion avec notes manuelles.
---
---
type: meeting
created: {{date}}
date: {{date}}
org:
location:
tags: [meeting]
---
# {{title}}

## Resume

## Personnes rencontrees

## Notes manuelles

## Actions
`,
  },
  {
    fileName: "compte-rendu-reunion-fr.md",
    language: "fr",
    minimal: true,
    content: `---
name: Compte rendu de reunion
language: fr
type: meeting_summary
description: Compte rendu complet avec decisions, actions et suggestions de notes liees.
---
---
type: meeting_summary
source_meeting:
source_audio:
model:
transcription_language:
output_language:
tags: [meeting, compte-rendu]
---
# Compte rendu de reunion

Respecter strictement les sources fournies. Ne pas inventer.
Suggere les personnes, organisations, roles et topics a creer en notes separees, uniquement si les sources les mentionnent clairement.

## Resume executif

## Contexte

## Participants / personnes mentionnees

## Sujets abordes

## Decisions prises

## Actions a suivre

## Points ouverts

## Risques / blocages

## Incertitudes ou contradictions

## Notes complementaires

## Liens utiles
`,
  },
  {
    fileName: "meeting-note-en.md",
    language: "en",
    minimal: true,
    content: `---
name: Meeting note
language: en
type: meeting
description: Source note for a meeting with manual notes.
---
---
type: meeting
created: {{date}}
date: {{date}}
org:
location:
tags: [meeting]
---
# {{title}}

## Summary

## People met

## Manual notes

## Actions
`,
  },
  {
    fileName: "meeting-minutes-en.md",
    language: "en",
    minimal: true,
    content: `---
name: Meeting minutes
language: en
type: meeting_summary
description: Full meeting minutes with decisions, actions, and linked-note suggestions.
---
---
type: meeting_summary
source_meeting:
source_audio:
model:
transcription_language:
output_language:
tags: [meeting, minutes]
---
# Meeting minutes

Use only the provided sources. Do not invent.
Suggest people, organizations, roles, and topics to create as separate notes only when clearly supported by the sources.

## Executive summary

## Context

## Participants / mentioned people

## Topics discussed

## Decisions made

## Follow-up actions

## Open points

## Risks / blockers

## Uncertainties or contradictions

## Additional notes

## Useful links
`,
  },
  {
    fileName: "actions-only-fr.md",
    language: "fr",
    minimal: false,
    content: `---
name: Actions uniquement
language: fr
type: meeting_summary
description: Extrait uniquement les actions, responsables et echeances.
---
# Actions a suivre

Extraire uniquement les actions confirmees par les sources.
Ne pas inventer de responsable ou d'echeance.

## Actions

| Action | Responsable | Echeance | Source / incertitude |
| --- | --- | --- | --- |
`,
  },
  {
    fileName: "actions-only-en.md",
    language: "en",
    minimal: false,
    content: `---
name: Actions only
language: en
type: meeting_summary
description: Extracts only actions, owners, and due dates.
---
# Follow-up actions

Extract only actions confirmed by the sources.
Do not invent owners or due dates.

## Actions

| Action | Owner | Due date | Source / uncertainty |
| --- | --- | --- | --- |
`,
  },
];
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

interface AssistantChatRequestPayload {
  message: string;
  context: string;
  mode: "chat" | "correct" | "rewrite" | "summarize";
  output_language: "same_as_input" | "fr" | "en";
  response_style?: "direct" | "detailed";
  model: string;
}

interface AssistantChatResponsePayload {
  model: string;
  mode: "chat" | "correct" | "rewrite" | "summarize";
  answer_markdown: string;
  usage: {
    message_chars: number;
    context_chars: number;
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
  output_language: PluginSettings["outputLanguage"];
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
  group: TemplateGroup;
}

interface AudioInputDeviceChoice {
  deviceId: string;
  label: string;
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
  extraStreams: MediaStream[];
  audioContext: AudioContext | null;
  recordingSourceRequested: RecordingSource;
  recordingSourceUsed: RecordingSourceUsed;
  microphoneInputDeviceLabel: string;
  computerAudioInputDeviceLabel: string;
  chunks: BlobPart[];
}

interface VaultIndexNotePayload {
  vault_id: string;
  path: string;
  title: string;
  content: string;
  modified_at: string;
  tags: string[];
  frontmatter: Record<string, unknown>;
  metadata: Record<string, unknown>;
}

interface VaultIndexNoteResponsePayload {
  status: "indexed" | "skipped";
  document_id: string;
  path: string;
  chunks_indexed: number;
  content_hash: string;
}

interface VaultSourcePayload {
  path: string;
  title: string | null;
  heading_path: string | null;
  chunk_index: number;
  score: number;
}

interface VaultAskResponsePayload {
  model: string;
  answer_markdown: string;
  sources: VaultSourcePayload[];
}

interface VaultStatsResponsePayload {
  vault_id: string;
  documents: number;
  chunks: number;
  last_indexed_at: string | null;
}

interface VaultDeleteResponsePayload {
  vault_id: string;
  deleted_documents: number;
  deleted_chunks: number;
}

interface VaultIndexProgress {
  total: number;
  indexed: number;
  skipped: number;
  errors: number;
}

interface RecordingStopResult {
  blob: Blob;
  title: string;
  notePath: string;
  startedAt: Date;
  mimeType: string;
  fileExtension: string;
  recordingSourceRequested: RecordingSource;
  recordingSourceUsed: RecordingSourceUsed;
  microphoneInputDeviceLabel: string;
  computerAudioInputDeviceLabel: string;
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
  recordingSource: RecordingSource;
  preferredTemplateLanguage: "auto" | "fr" | "en";
  microphoneInputDeviceId: string;
  microphoneInputDeviceLabel: string;
  computerAudioInputDeviceId: string;
  computerAudioInputDeviceLabel: string;
  audioInputDeviceId: string;
  audioInputDeviceLabel: string;
  quickActionsLanguage: "same_as_input" | "fr" | "en";
  chatUseCurrentNoteContext: boolean;
  assistantResponseMode: AssistantResponseMode;
  ragEnabled: boolean;
  vaultId: string;
  ragExcludedFolders: string;
  ragExcludedTags: string;
  ragMaxFileChars: number;
  dashboardVaultExpanded: boolean;
  dashboardTemplatesExpanded: boolean;
  dashboardStatusExpanded: boolean;
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
  recordingSource: "microphone_only",
  preferredTemplateLanguage: "auto",
  microphoneInputDeviceId: "",
  microphoneInputDeviceLabel: "Default microphone",
  computerAudioInputDeviceId: "",
  computerAudioInputDeviceLabel: "Not configured",
  audioInputDeviceId: "",
  audioInputDeviceLabel: "Default microphone",
  quickActionsLanguage: "same_as_input",
  chatUseCurrentNoteContext: false,
  assistantResponseMode: "simple",
  ragEnabled: true,
  vaultId: "default",
  ragExcludedFolders: ".obsidian,Templates,Archives,Private",
  ragExcludedTags: "noai,private",
  ragMaxFileChars: 500000,
  dashboardVaultExpanded: false,
  dashboardTemplatesExpanded: false,
  dashboardStatusExpanded: false,
};

export default class LocalAiPlatformPlugin extends Plugin {
  settings: PluginSettings = DEFAULT_SETTINGS;
  activeRecording: ActiveRecordingSession | null = null;
  audioInputDevices: AudioInputDeviceChoice[] = [{ deviceId: "", label: "Default microphone" }];

  async onload(): Promise<void> {
    await this.loadSettings();

    this.registerView(DASHBOARD_VIEW_TYPE, (leaf) => new NoteCompagnonDashboardView(leaf, this));
    this.addSettingTab(new LocalAiPlatformSettingTab(this.app, this));
    this.registerEvent(
      this.app.workspace.on("editor-menu", (menu: Menu, editor: Editor) => {
        this.addEditorContextMenuItems(menu, editor);
      }),
    );
    this.addCommand({
      id: "open-dashboard",
      name: "Note Compagnon: Open dashboard",
      callback: async () => {
        await this.openDashboard();
      },
    });
    this.addCommand({
      id: "summarize-current-note",
      name: "Note Compagnon: Summarize current note",
      callback: async () => {
        await this.summarizeCurrentNote();
      },
    });
    this.addCommand({
      id: "generate-meeting-minutes-from-audio-file",
      name: "Note Compagnon: Generate minutes from audio file",
      callback: async () => {
        await this.generateMeetingMinutesFromAudioFile();
      },
    });
    this.addCommand({
      id: "start-meeting-recording",
      name: "Note Compagnon: Start meeting recording",
      callback: async () => {
        await this.startMeetingRecording();
      },
    });
    this.addCommand({
      id: "stop-recording-and-generate-meeting-minutes",
      name: "Note Compagnon: Stop recording and generate minutes",
      callback: async () => {
        await this.stopRecordingAndGenerateMeetingMinutes();
      },
    });
    this.addCommand({
      id: "correct-selected-text",
      name: "Note Compagnon: Correct selected text",
      editorCallback: async (editor) => {
        await this.runAssistantOnSelection(editor, "correct");
      },
    });
    this.addCommand({
      id: "rewrite-selected-text",
      name: "Note Compagnon: Rewrite selected text",
      editorCallback: async (editor) => {
        await this.runAssistantOnSelection(editor, "rewrite");
      },
    });
    this.addCommand({
      id: "summarize-selected-text",
      name: "Note Compagnon: Summarize selected text",
      editorCallback: async (editor) => {
        await this.runAssistantOnSelection(editor, "summarize");
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
    if (this.settings.recordingSource === ("system_audio_only" as RecordingSource)) {
      this.settings.recordingSource = "experimental_system_capture";
    }
    if (this.settings.recordingSource === ("microphone_and_system_audio" as RecordingSource)) {
      this.settings.recordingSource = "microphone_plus_computer_audio";
    }
    if (this.settings.recordingSource === "selected_audio_input") {
      this.settings.recordingSource = "computer_audio_only";
    }
    if (!this.settings.microphoneInputDeviceId && this.settings.audioInputDeviceId) {
      this.settings.microphoneInputDeviceId = this.settings.audioInputDeviceId;
      this.settings.microphoneInputDeviceLabel = this.settings.audioInputDeviceLabel || "Selected microphone";
    }
  }

  async saveSettings(): Promise<void> {
    await this.saveData(this.settings);
  }

  addEditorContextMenuItems(menu: Menu, editor: Editor): void {
    if (!editor.getSelection().trim()) {
      return;
    }
    menu.addSeparator();
    menu.addItem((item) =>
      item.setTitle("Note Compagnon: Corriger").setIcon("checkmark").onClick(() => {
        void this.runAssistantOnSelection(editor, "correct");
      }),
    );
    menu.addItem((item) =>
      item.setTitle("Note Compagnon: Reecrire").setIcon("pencil").onClick(() => {
        void this.runAssistantOnSelection(editor, "rewrite");
      }),
    );
    menu.addItem((item) =>
      item.setTitle("Note Compagnon: Resumer").setIcon("list").onClick(() => {
        void this.runAssistantOnSelection(editor, "summarize");
      }),
    );
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

      new Notice("Note Compagnon is generating the summary...");
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
        output_language: this.getOutputLanguage(),
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

      const recordingStream = await this.requestRecordingStream();
      try {
        const recordingOptions = pickRecordingOptions();
        const recorder = new MediaRecorder(recordingStream.stream, recordingOptions.mimeType ? { mimeType: recordingOptions.mimeType } : undefined);
        const meetingsFolder = this.getMeetingsFolder();
        await ensureFolderExists(this.app, meetingsFolder);

        const startedAt = new Date();
        const fileBaseName = `${formatDateTimeForFile(startedAt)} - ${sanitizeFileName(metadata.title)}`;
        const notePath = normalizePath(`${meetingsFolder}/${fileBaseName}.md`);
        const noteContent = buildMeetingSourceNote({
          title: metadata.title,
          startedAt,
          recordingStatus: "in progress",
          recordingSourceRequested: this.getRecordingSource(),
          recordingSourceUsed: recordingStream.recordingSourceUsed,
          microphoneInputDeviceLabel: this.getSelectedMicrophoneInputLabel(),
          computerAudioInputDeviceLabel: this.getSelectedComputerAudioInputLabel(),
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
          stream: recordingStream.stream,
          extraStreams: recordingStream.extraStreams,
          audioContext: recordingStream.audioContext,
          recordingSourceRequested: this.getRecordingSource(),
          recordingSourceUsed: recordingStream.recordingSourceUsed,
          microphoneInputDeviceLabel: this.getSelectedMicrophoneInputLabel(),
          computerAudioInputDeviceLabel: this.getSelectedComputerAudioInputLabel(),
          chunks,
        };

        new Notice(`Recording started: ${metadata.title}`, 6000);
      } catch (error) {
        cleanupRecordingResources(recordingStream.stream, recordingStream.extraStreams, recordingStream.audioContext);
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
        output_language: this.getOutputLanguage(),
      });

      const outputFile = await this.writeMeetingNote({
        response: result,
        templateChoice,
        sourceAudioName: savedAudio.file.name,
        sourceNoteFile: sourceNote,
        sourceAudioFile: savedAudio.file,
        recordingSourceUsed: recording.recordingSourceUsed,
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

  getAssistantOutputLanguage(): "same_as_input" | "fr" | "en" {
    const outputLanguage = this.getOutputLanguage();
    return outputLanguage === "same_as_meeting" ? "same_as_input" : outputLanguage;
  }

  getQuickActionsLanguage(): "same_as_input" | "fr" | "en" {
    return this.settings.quickActionsLanguage || "same_as_input";
  }

  getVaultId(): string {
    return this.settings.vaultId.trim() || "default";
  }

  getRagExcludedFolders(): string[] {
    return parseCommaSeparatedList(this.settings.ragExcludedFolders || DEFAULT_SETTINGS.ragExcludedFolders);
  }

  getRagExcludedTags(): string[] {
    return parseCommaSeparatedList(this.settings.ragExcludedTags || DEFAULT_SETTINGS.ragExcludedTags).map((tag) => tag.replace(/^#/, ""));
  }

  getRagMaxFileChars(): number {
    const value = Number(this.settings.ragMaxFileChars);
    return Number.isFinite(value) && value > 0 ? value : DEFAULT_SETTINGS.ragMaxFileChars;
  }

  getTranscriptionLanguage(): "auto" | "fr" | "en" {
    return this.settings.transcriptionLanguage || "auto";
  }

  getRecordingSource(): RecordingSource {
    return this.settings.recordingSource || "microphone_only";
  }

  getSelectedMicrophoneInputLabel(): string {
    if (!this.settings.microphoneInputDeviceId) {
      return "Default microphone";
    }
    return this.settings.microphoneInputDeviceLabel || "Selected microphone";
  }

  getSelectedComputerAudioInputLabel(): string {
    if (!this.settings.computerAudioInputDeviceId) {
      return "Not configured";
    }
    return this.settings.computerAudioInputDeviceLabel || "Selected computer audio input";
  }

  getAudioInputDeviceChoices(): AudioInputDeviceChoice[] {
    const choices = [...this.audioInputDevices];
    const addMissingChoice = (deviceId: string, label: string): void => {
      if (deviceId && !choices.some((choice) => choice.deviceId === deviceId)) {
        choices.push({ deviceId, label });
      }
    };
    addMissingChoice(this.settings.microphoneInputDeviceId, this.settings.microphoneInputDeviceLabel || "Previously selected microphone");
    addMissingChoice(this.settings.computerAudioInputDeviceId, this.settings.computerAudioInputDeviceLabel || "Previously selected computer audio input");
    if (this.settings.audioInputDeviceId && !choices.some((choice) => choice.deviceId === this.settings.audioInputDeviceId)) {
      choices.push({
        deviceId: this.settings.audioInputDeviceId,
        label: this.settings.audioInputDeviceLabel || "Previously selected audio input",
      });
    }
    return choices;
  }

  getComputerAudioInputDeviceChoices(): AudioInputDeviceChoice[] {
    const choices = this.getAudioInputDeviceChoices().map((choice) =>
      choice.deviceId ? { ...choice, label: formatComputerAudioInputLabel(choice.label) } : { deviceId: "", label: "Non configure" },
    );
    if (this.settings.computerAudioInputDeviceId && !choices.some((choice) => choice.deviceId === this.settings.computerAudioInputDeviceId)) {
      choices.push({
        deviceId: this.settings.computerAudioInputDeviceId,
        label: this.settings.computerAudioInputDeviceLabel || "Entree son ordinateur precedente",
      });
    }
    return choices;
  }

  getPreferredTemplateLanguage(): "auto" | "fr" | "en" {
    return this.settings.preferredTemplateLanguage || "auto";
  }

  getTemplateFilterLanguage(): "all" | "fr" | "en" {
    const outputLanguage = this.getOutputLanguage();
    if (outputLanguage === "fr" || outputLanguage === "en") {
      return outputLanguage;
    }
    const preferred = this.getPreferredTemplateLanguage();
    return preferred === "auto" ? "all" : preferred;
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

  async refreshAudioInputDevices(requestPermission = true): Promise<void> {
    if (!navigator.mediaDevices?.enumerateDevices) {
      throw new UserFacingError("Audio device enumeration is not available in this Obsidian environment.");
    }

    let permissionStream: MediaStream | null = null;
    if (requestPermission && navigator.mediaDevices.getUserMedia) {
      try {
        permissionStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      } catch {
        new Notice("Microphone permission was not granted. Device labels may be hidden.", 6000);
      } finally {
        if (permissionStream) {
          stopMediaStream(permissionStream);
        }
      }
    }

    const devices = await navigator.mediaDevices.enumerateDevices();
    const audioInputs = devices.filter((device) => device.kind === "audioinput");
    this.audioInputDevices = [
      { deviceId: "", label: "Default microphone" },
      ...audioInputs.map((device, index) => ({
        deviceId: device.deviceId,
        label: device.label || `Microphone ${index + 1}`,
      })),
    ];

    const syncSelectedDevice = (
      deviceId: string,
      fallbackLabel: string,
      update: (nextDeviceId: string, nextLabel: string) => void,
    ): void => {
      if (!deviceId) {
        return;
      }
      const selected = this.audioInputDevices.find((device) => device.deviceId === deviceId);
      if (selected) {
        update(deviceId, selected.label);
      } else {
        update("", fallbackLabel);
      }
    };

    const likelySystemInput = findLikelySystemAudioInput(this.audioInputDevices);
    syncSelectedDevice(this.settings.microphoneInputDeviceId, "Default microphone", (deviceId, label) => {
      this.settings.microphoneInputDeviceId = deviceId;
      this.settings.microphoneInputDeviceLabel = label;
    });
    syncSelectedDevice(this.settings.computerAudioInputDeviceId, "Not configured", (deviceId, label) => {
      this.settings.computerAudioInputDeviceId = deviceId;
      this.settings.computerAudioInputDeviceLabel = label;
    });
    if (!this.settings.computerAudioInputDeviceId && likelySystemInput) {
      this.settings.computerAudioInputDeviceId = likelySystemInput.deviceId;
      this.settings.computerAudioInputDeviceLabel = likelySystemInput.label;
    }
    await this.saveSettings();

    if (likelySystemInput) {
      new Notice(`Entree son ordinateur detectee : ${likelySystemInput.label}`, 9000);
      return;
    }

    new Notice("Aucune entree ordinateur native detectee. Verifie Mixage stereo / Stereo Mix dans Windows.", 10000);
  }

  async testAudioInput(kind: "microphone" | "computer"): Promise<void> {
    let stream: MediaStream | null = null;
    let audioContext: AudioContext | null = null;
    try {
      if (kind === "computer") {
        new Notice("Lance un son sur l'ordinateur pendant le test.", 5000);
      }
      stream = kind === "microphone" ? await this.requestMicrophoneStream() : await this.requestComputerAudioStream();
      audioContext = new AudioContext();
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 2048;
      audioContext.createMediaStreamSource(stream).connect(analyser);

      const samples = new Uint8Array(analyser.fftSize);
      const startedAt = Date.now();
      let audioDetected = false;
      while (Date.now() - startedAt < 4_000) {
        analyser.getByteTimeDomainData(samples);
        const peak = samples.reduce((max, sample) => Math.max(max, Math.abs(sample - 128)), 0);
        if (peak > 8) {
          audioDetected = true;
          break;
        }
        await sleep(150);
      }

      if (audioDetected) {
        new Notice("Audio detected.", 6000);
      } else if (kind === "computer") {
        new Notice("No audio detected. Lance un son sur l'ordinateur ou verifie le peripherique selectionne.", 8000);
      } else {
        new Notice("No audio detected.", 6000);
      }
    } catch (error) {
      if (error instanceof UserFacingError) {
        throw error;
      }
      throw new UserFacingError("Audio input test failed.");
    } finally {
      if (stream) {
        stopMediaStream(stream);
      }
      if (audioContext) {
        void audioContext.close();
      }
    }
  }

  getAudioInputConstraints(deviceId: string): boolean | MediaTrackConstraints {
    const selectedDeviceId = deviceId.trim();
    if (!selectedDeviceId) {
      return true;
    }
    return { deviceId: { exact: selectedDeviceId } };
  }

  async requestMicrophoneStream(): Promise<MediaStream> {
    try {
      return await navigator.mediaDevices.getUserMedia({ audio: this.getAudioInputConstraints(this.settings.microphoneInputDeviceId) });
    } catch {
      throw new UserFacingError("Microphone permission was denied or unavailable.");
    }
  }

  async requestComputerAudioStream(): Promise<MediaStream> {
    if (!this.settings.computerAudioInputDeviceId.trim()) {
      throw new UserFacingError("Selectionne Mixage stereo / Stereo Mix dans Son ordinateur.");
    }
    try {
      return await navigator.mediaDevices.getUserMedia({ audio: this.getAudioInputConstraints(this.settings.computerAudioInputDeviceId) });
    } catch {
      throw new UserFacingError("L'entree son ordinateur est refusee ou indisponible.");
    }
  }

  async requestRecordingStream(): Promise<{
    stream: MediaStream;
    extraStreams: MediaStream[];
    audioContext: AudioContext | null;
    recordingSourceUsed: RecordingSourceUsed;
  }> {
    const recordingSource = this.getRecordingSource();
    if (recordingSource === "microphone_only") {
      return {
        stream: await this.requestMicrophoneStream(),
        extraStreams: [],
        audioContext: null,
        recordingSourceUsed: "microphone_only",
      };
    }

    if (recordingSource === "computer_audio_only" || recordingSource === "selected_audio_input") {
      return {
        stream: await this.requestComputerAudioStream(),
        extraStreams: [],
        audioContext: null,
        recordingSourceUsed: "computer_audio_only",
      };
    }

    if (recordingSource === "microphone_plus_computer_audio") {
      const microphoneStream = await this.requestMicrophoneStream();
      try {
        const computerStream = await this.requestComputerAudioStream();
        const mixed = createMixedAudioStream(microphoneStream, computerStream);
        return {
          stream: mixed.stream,
          extraStreams: [microphoneStream, computerStream],
          audioContext: mixed.audioContext,
          recordingSourceUsed: "microphone_plus_computer_audio",
        };
      } catch (error) {
        stopMediaStream(microphoneStream);
        throw error;
      }
    }

    const shouldContinue = await confirmSystemAudioCapture(this.app);
    if (!shouldContinue) {
      throw new UserFacingError("Recording cancelled.");
    }

    const systemAudioStream = await captureSystemAudio();
    if (!systemAudioStream) {
      throw new UserFacingError("Capture systeme indisponible dans Obsidian. Utilise une entree audio Windows comme Mixage stereo si disponible.");
    }

    new Notice("Capture systeme detectee.", 5000);
    return { stream: systemAudioStream, extraStreams: [], audioContext: null, recordingSourceUsed: "experimental_system_capture" };
  }

  async chooseTemplate(): Promise<TemplateChoice> {
    const availableTemplates = await this.listTemplateChoices();
    return chooseTemplateWithModal(this.app, availableTemplates, this.getTemplateFilterLanguage());
  }

  async listTemplateChoices(): Promise<TemplateChoice[]> {
    const choices: TemplateChoice[] = [
      {
        label: "Built-in default template",
        templateContent: FALLBACK_TEMPLATE,
        sourcePath: null,
        description: "Fallback template bundled with Note Compagnon.",
        language: "fr",
        type: "meeting",
        group: "meeting_summary",
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
        group: inferTemplateGroup(parsedTemplate.metadata.type, file.basename),
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

  async requestAssistantChat(
    apiBaseUrl: string,
    apiToken: string,
    payload: AssistantChatRequestPayload,
  ): Promise<AssistantChatResponsePayload> {
    console.debug("Note Compagnon assistant request", {
      endpoint: "/v1/assistant/chat",
      mode: payload.mode,
      output_language: payload.output_language,
    });
    const responseText = await this.performJsonRequest({
      apiBaseUrl,
      apiToken,
      path: "/v1/assistant/chat",
      method: "POST",
      body: JSON.stringify(payload),
      errorMap: {
        401: "Token assistant absent, invalide ou expire.",
        403: "Le token n'a pas le scope assistant:chat, ou le modele est refuse.",
        404: "Endpoint assistant introuvable. Mets a jour l'AI Gateway.",
        413: "The assistant request is too large for the AI Gateway limits.",
        422: "The assistant request is invalid.",
        502: "Modele ou backend IA indisponible.",
        503: "Modele ou backend IA indisponible.",
      },
      unavailableMessage: "The AI Gateway is unreachable.",
      invalidJsonMessage: "The AI Gateway returned an invalid assistant response.",
    });

    return this.parseAssistantChatResponse(responseText);
  }

  async requestVaultIndexNote(apiBaseUrl: string, apiToken: string, payload: VaultIndexNotePayload): Promise<VaultIndexNoteResponsePayload> {
    const responseText = await this.performJsonRequest({
      apiBaseUrl,
      apiToken,
      path: "/v1/vault/index-note",
      method: "POST",
      body: JSON.stringify(payload),
      errorMap: {
        401: "Token invalide ou expire.",
        403: "Le token n'a pas le droit vault:index.",
        413: "La note est trop volumineuse pour l'indexation.",
        422: "La note ne peut pas etre indexee.",
        503: "La connaissance du vault n'est pas activee cote serveur, ou le modele d'embedding est absent.",
      },
      unavailableMessage: "AI Gateway inaccessible.",
      invalidJsonMessage: "The AI Gateway returned an invalid vault indexing response.",
    });
    return this.parseVaultIndexNoteResponse(responseText);
  }

  async askVault(question: string): Promise<VaultAskResponsePayload> {
    if (!this.settings.ragEnabled) {
      throw new UserFacingError("La connaissance du vault est desactivee dans les reglages.");
    }
    const responseText = await this.performJsonRequest({
      apiBaseUrl: this.getApiBaseUrl(),
      apiToken: this.getApiToken(),
      path: "/v1/vault/ask",
      method: "POST",
      body: JSON.stringify({
        vault_id: this.getVaultId(),
        question,
        model: this.getDefaultModel(),
        top_k: 8,
        answer_language: "same_as_input",
      }),
      errorMap: {
        401: "Token invalide ou expire.",
        403: "Le token n'a pas le droit vault:ask, ou le modele est refuse.",
        422: "La question RAG est invalide.",
        502: "Le backend RAG a retourne une reponse invalide.",
        503: "La connaissance du vault n'est pas activee cote serveur, ou le modele d'embedding est absent.",
      },
      unavailableMessage: "AI Gateway inaccessible.",
      invalidJsonMessage: "The AI Gateway returned an invalid vault answer.",
    });
    const parsed = this.parseVaultAskResponse(responseText);
    if (parsed.sources.length === 0) {
      new Notice("Aucune note indexee ou pas assez d'informations trouvees pour ce vault.", 8000);
    }
    return parsed;
  }

  async getVaultStats(): Promise<VaultStatsResponsePayload> {
    const responseText = await this.performJsonRequest({
      apiBaseUrl: this.getApiBaseUrl(),
      apiToken: this.getApiToken(),
      path: `/v1/vault/stats?vault_id=${encodeURIComponent(this.getVaultId())}`,
      method: "GET",
      errorMap: {
        401: "Token invalide ou expire.",
        403: "Le token n'a pas le droit vault:search.",
        503: "La connaissance du vault n'est pas activee cote serveur.",
      },
      unavailableMessage: "AI Gateway inaccessible.",
      invalidJsonMessage: "The AI Gateway returned an invalid vault stats response.",
    });
    return this.parseVaultStatsResponse(responseText);
  }

  async deleteVaultIndex(): Promise<VaultDeleteResponsePayload> {
    const responseText = await this.performJsonRequest({
      apiBaseUrl: this.getApiBaseUrl(),
      apiToken: this.getApiToken(),
      path: `/v1/vault/index?vault_id=${encodeURIComponent(this.getVaultId())}`,
      method: "DELETE",
      errorMap: {
        401: "Token invalide ou expire.",
        403: "Le token n'a pas le droit vault:admin.",
        503: "La connaissance du vault n'est pas activee cote serveur.",
      },
      unavailableMessage: "AI Gateway inaccessible.",
      invalidJsonMessage: "The AI Gateway returned an invalid vault delete response.",
    });
    return this.parseVaultDeleteResponse(responseText);
  }

  async indexVaultNote(file: TFile): Promise<VaultIndexNoteResponsePayload | null> {
    if (!this.settings.ragEnabled) {
      throw new UserFacingError("La connaissance du vault est desactivee dans les reglages.");
    }
    const candidate = await this.buildVaultIndexPayload(file);
    if (!candidate) {
      return null;
    }
    return this.requestVaultIndexNote(this.getApiBaseUrl(), this.getApiToken(), candidate);
  }

  async indexCurrentNote(): Promise<void> {
    const activeFile = this.app.workspace.getActiveFile();
    if (!activeFile) {
      throw new UserFacingError("Ouvre une note avant de l'indexer.");
    }
    const result = await this.indexVaultNote(activeFile);
    if (!result) {
      new Notice("Note ignoree par les exclusions RAG.");
      return;
    }
    new Notice(`Note ${result.status}: ${result.chunks_indexed} chunks.`);
  }

  async indexCurrentFolder(): Promise<void> {
    const activeFile = this.app.workspace.getActiveFile();
    if (!activeFile?.parent) {
      throw new UserFacingError("Ouvre une note dans le dossier a indexer.");
    }
    const files = collectMarkdownFiles(activeFile.parent);
    const progress = await this.indexVaultFiles(files, "Indexation du dossier");
    new Notice(`Dossier indexe. Indexed: ${progress.indexed}, skipped: ${progress.skipped}, errors: ${progress.errors}.`, 10000);
  }

  async indexWholeVault(): Promise<void> {
    const files = this.app.vault.getMarkdownFiles();
    const progress = await this.indexVaultFiles(files, "Indexation du vault");
    new Notice(`Vault indexe. Indexed: ${progress.indexed}, skipped: ${progress.skipped}, errors: ${progress.errors}.`, 10000);
  }

  async indexVaultFiles(files: TFile[], label: string): Promise<VaultIndexProgress> {
    this.validateCoreSettings();
    const progress: VaultIndexProgress = { total: files.length, indexed: 0, skipped: 0, errors: 0 };
    for (let index = 0; index < files.length; index += 1) {
      if (index % 10 === 0) {
        new Notice(`${label}: ${index}/${files.length}`, 1500);
        await sleep(25);
      }
      try {
        const result = await this.indexVaultNote(files[index]);
        if (!result || result.status === "skipped") {
          progress.skipped += 1;
        } else {
          progress.indexed += 1;
        }
      } catch (error) {
        progress.errors += 1;
        console.warn("Note Compagnon vault indexing error", {
          path: files[index].path,
          message: error instanceof Error ? error.message : "unknown",
        });
      }
    }
    return progress;
  }

  async buildVaultIndexPayload(file: TFile): Promise<VaultIndexNotePayload | null> {
    if (file.extension !== "md" || this.isPathExcludedFromRag(file.path)) {
      return null;
    }
    const content = await this.app.vault.read(file);
    if (content.length > this.getRagMaxFileChars()) {
      return null;
    }
    const parsed = parseSimpleFrontmatter(content);
    if (parsed.frontmatter.ai_index === false) {
      return null;
    }
    const tags = extractTagsFromFrontmatter(parsed.frontmatter);
    const excludedTags = new Set(this.getRagExcludedTags().map((tag) => tag.toLowerCase()));
    if (tags.some((tag) => excludedTags.has(tag.toLowerCase().replace(/^#/, "")))) {
      return null;
    }
    const title = typeof parsed.frontmatter.title === "string" && parsed.frontmatter.title.trim()
      ? parsed.frontmatter.title.trim()
      : file.basename;
    return {
      vault_id: this.getVaultId(),
      path: file.path,
      title,
      content,
      modified_at: new Date(file.stat.mtime).toISOString(),
      tags,
      frontmatter: parsed.frontmatter,
      metadata: {
        indexed_by: "note-compagnon",
      },
    };
  }

  isPathExcludedFromRag(path: string): boolean {
    const normalizedPath = normalizePath(path);
    return this.getRagExcludedFolders().some((folder) => {
      const normalizedFolder = normalizePath(folder);
      return normalizedPath === normalizedFolder || normalizedPath.startsWith(`${normalizedFolder}/`);
    });
  }

  async askAssistant(message: string, useCurrentNoteContext: boolean): Promise<AssistantChatResponsePayload> {
    this.validateCoreSettings();
    let context = "";
    if (useCurrentNoteContext) {
      const activeFile = this.app.workspace.getActiveFile();
      if (!activeFile) {
        new Notice("Aucune note active. La question est envoyee sans contexte.", 6000);
      } else {
        context = await this.app.vault.read(activeFile);
      }
    }
    return this.requestAssistantChat(this.getApiBaseUrl(), this.getApiToken(), {
      message,
      context,
      mode: "chat",
      output_language: this.getAssistantOutputLanguage(),
      model: this.getDefaultModel(),
    });
  }

  async askDashboardAssistant(message: string, mode: AssistantResponseMode): Promise<{ answerMarkdown: string; sources: VaultSourcePayload[] }> {
    this.validateCoreSettings();
    if (mode === "vault") {
      const response = await this.askVault(message);
      return {
        answerMarkdown: cleanGeneratedMarkdown(response.answer_markdown),
        sources: response.sources,
      };
    }

    const useCurrentNoteContext = mode === "current_note";
    const response = await this.askAssistant(message, useCurrentNoteContext);
    return {
      answerMarkdown: cleanGeneratedMarkdown(response.answer_markdown),
      sources: [],
    };
  }

  async runAssistantOnSelection(editor: Editor, mode: "correct" | "rewrite" | "summarize"): Promise<void> {
    try {
      this.validateCoreSettings();
      const selection = editor.getSelection();
      if (!selection.trim()) {
        throw new UserFacingError("Selectionne un texte dans une note, puis lance la commande depuis la palette ou le clic droit.");
      }

      new Notice("Note Compagnon prepare une proposition...");
      const response = await this.requestAssistantChat(this.getApiBaseUrl(), this.getApiToken(), {
        message: "",
        context: selection,
        mode,
        output_language: this.getQuickActionsLanguage(),
        response_style: "direct",
        model: this.getDefaultModel(),
      });
      const cleanedAnswer = cleanGeneratedMarkdown(response.answer_markdown);

      await previewAssistantReplacement(this.app, cleanedAnswer, (action) => {
        if (action === "replace") {
          editor.replaceSelection(cleanedAnswer);
        } else if (action === "insert") {
          editor.replaceSelection(`${selection}\n\n${cleanedAnswer}`);
        } else if (action === "copy") {
          void copyToClipboard(cleanedAnswer);
          new Notice("Assistant response copied.");
        }
      });
    } catch (error) {
      this.showUserFacingError(error);
    }
  }

  async installRecommendedTemplates(installSet: TemplateInstallSet = "minimal"): Promise<number> {
    const templatesFolder = this.getTemplatesFolder();
    await ensureFolderExists(this.app, templatesFolder);
    let created = 0;
    const templatesToInstall = filterRecommendedTemplates(installSet);

    for (const template of templatesToInstall) {
      const path = normalizePath(`${templatesFolder}/${template.fileName}`);
      if (this.app.vault.getAbstractFileByPath(path)) {
        continue;
      }
      await this.app.vault.create(path, template.content);
      created += 1;
    }

    new Notice(`Templates installed: ${created}`);
    return created;
  }

  async uploadAudio(apiBaseUrl: string, apiToken: string, audioFile: File): Promise<AudioJobQueuedResponsePayload> {
    try {
      const formData = new FormData();
      formData.append("file", audioFile);
      formData.append("transcription_language", this.getTranscriptionLanguage());

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
    method: "GET" | "POST" | "DELETE";
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
        this.logHttpError(input.path, response.status, "AI Gateway returned an error response.");
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

  logHttpError(path: string, statusCode: number, message: string): void {
    console.warn("Note Compagnon API error", {
      endpoint: path,
      status: statusCode,
      message,
    });
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
        cleanupRecordingResources(session.stream, session.extraStreams, session.audioContext);
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
      recordingSourceRequested: session.recordingSourceRequested,
      recordingSourceUsed: session.recordingSourceUsed,
      microphoneInputDeviceLabel: session.microphoneInputDeviceLabel,
      computerAudioInputDeviceLabel: session.computerAudioInputDeviceLabel,
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
    const outputPath = normalizePath(`${outputFolder}/${date} - Note Compagnon Summary - ${safeTitle}.md`);
    const sourceLink = this.app.metadataCache.fileToLinktext(sourceFile, "", true);
    const noteContent = buildSummaryNote({
      title: response.title || sourceFile.basename,
      sourceLink,
      model: response.model,
      templateLabel: templateChoice.label,
      outputLanguage: this.getOutputLanguage(),
      summaryMarkdown: cleanGeneratedMarkdown(response.summary_markdown),
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
    recordingSourceUsed?: string;
    generatedAt?: Date;
  }): Promise<TFile> {
    const outputFolder = this.getOutputFolder();
    await ensureFolderExists(this.app, outputFolder);

    const generatedAt = input.generatedAt ?? new Date();
    const outputPath = normalizePath(
      `${outputFolder}/${formatDateTimeForFile(generatedAt)} - Compte rendu - ${sanitizeFileName(input.response.title)}.md`,
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
      transcriptionLanguage: this.getTranscriptionLanguage(),
      jobId: input.response.job_id,
      audioFileName: input.sourceAudioName,
      sourceMeetingLink,
      sourceAudioLink,
      recordingSourceUsed: input.recordingSourceUsed ?? "external_audio_file",
      meetingMarkdown: cleanGeneratedMarkdown(input.response.meeting_markdown),
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

  parseAssistantChatResponse(responseText: string): AssistantChatResponsePayload {
    const parsed = this.parseJson(responseText, "The AI Gateway returned an invalid assistant response.");
    if (!isAssistantChatResponsePayload(parsed)) {
      throw new UserFacingError("The AI Gateway returned an invalid assistant payload.");
    }
    return parsed;
  }

  parseVaultIndexNoteResponse(responseText: string): VaultIndexNoteResponsePayload {
    const parsed = this.parseJson(responseText, "The AI Gateway returned an invalid vault indexing response.");
    if (!isVaultIndexNoteResponsePayload(parsed)) {
      throw new UserFacingError("The AI Gateway returned an invalid vault indexing payload.");
    }
    return parsed;
  }

  parseVaultAskResponse(responseText: string): VaultAskResponsePayload {
    const parsed = this.parseJson(responseText, "The AI Gateway returned an invalid vault answer response.");
    if (!isVaultAskResponsePayload(parsed)) {
      throw new UserFacingError("The AI Gateway returned an invalid vault answer payload.");
    }
    return parsed;
  }

  parseVaultStatsResponse(responseText: string): VaultStatsResponsePayload {
    const parsed = this.parseJson(responseText, "The AI Gateway returned an invalid vault stats response.");
    if (!isVaultStatsResponsePayload(parsed)) {
      throw new UserFacingError("The AI Gateway returned an invalid vault stats payload.");
    }
    return parsed;
  }

  parseVaultDeleteResponse(responseText: string): VaultDeleteResponsePayload {
    const parsed = this.parseJson(responseText, "The AI Gateway returned an invalid vault delete response.");
    if (!isVaultDeleteResponsePayload(parsed)) {
      throw new UserFacingError("The AI Gateway returned an invalid vault delete payload.");
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

class NoteCompagnonDashboardView extends ItemView {
  private readonly plugin: LocalAiPlatformPlugin;
  private chatResponseEl: HTMLElement | null = null;
  private chatActionsEl: HTMLElement | null = null;
  private lastAssistantAnswer = "";
  private lastVaultSources: VaultSourcePayload[] = [];
  private lastVaultStats: VaultStatsResponsePayload | null = null;
  private assistantResponseMode: AssistantResponseMode = "simple";

  constructor(leaf: WorkspaceLeaf, plugin: LocalAiPlatformPlugin) {
    super(leaf);
    this.plugin = plugin;
  }

  getViewType(): string {
    return DASHBOARD_VIEW_TYPE;
  }

  getDisplayText(): string {
    return "Note Compagnon";
  }

  async onOpen(): Promise<void> {
    await this.render();
  }

  async onClose(): Promise<void> {
    this.contentEl.empty();
  }

  async render(): Promise<void> {
    const container = this.contentEl;
    container.empty();
    container.addClass("notre-compagnon-dashboard");
    container.createEl("h2", { text: "Note Compagnon" });

    container.createEl("h3", { text: "Reunion" });
    this.addActionGrid(container, [
      ["Demarrer reunion", async () => this.plugin.startMeetingRecording()],
      ["Arreter + CR", async () => this.plugin.stopRecordingAndGenerateMeetingMinutes()],
      ["Depuis audio", async () => this.plugin.generateMeetingMinutesFromAudioFile()],
      ["Resumer note", async () => this.plugin.summarizeCurrentNote()],
      ["Ouvrir reunions", async () => this.plugin.openConfiguredFolder(this.plugin.getMeetingsFolder())],
      ["Ouvrir comptes rendus", async () => this.plugin.openConfiguredFolder(this.plugin.getOutputFolder())],
    ]);

    container.createEl("h3", { text: "Assistant" });
    const modeSetting = new Setting(container)
      .setName("Mode de reponse")
      .setDesc("Choisis explicitement si Note Compagnon utilise seulement la question, la note courante ou l'index du vault.")
      .addDropdown((dropdown) =>
        dropdown
          .addOption("simple", "Assistant simple")
          .addOption("current_note", "Avec la note courante")
          .addOption("vault", "Avec le vault")
          .setValue(this.plugin.settings.assistantResponseMode || "simple")
          .onChange(async (value) => {
            this.assistantResponseMode = value as AssistantResponseMode;
            this.plugin.settings.assistantResponseMode = this.assistantResponseMode;
            await this.plugin.saveSettings();
          }),
      );
    modeSetting.settingEl.style.marginBottom = "6px";
    this.assistantResponseMode = this.plugin.settings.assistantResponseMode || "simple";

    const chatInput = container.createEl("textarea");
    chatInput.placeholder = "Pose une question a Note Compagnon...";
    chatInput.rows = 4;
    chatInput.style.width = "100%";
    const responseBox = container.createDiv();
    responseBox.addClass("notre-compagnon-assistant-answer");
    responseBox.style.display = this.lastAssistantAnswer ? "block" : "none";
    this.chatResponseEl = responseBox;
    await this.renderAssistantAnswer();

    const chatControls = container.createDiv({ cls: "notre-compagnon-chat-controls" });
    chatControls.style.display = "flex";
    chatControls.style.flexWrap = "wrap";
    chatControls.style.gap = "6px";
    chatControls.style.margin = "8px 0";
    const sendButton = chatControls.createEl("button", { text: "Envoyer" });
    sendButton.style.padding = "4px 10px";
    sendButton.addEventListener("click", async () => {
      await this.sendChatMessage(chatInput.value);
    });
    const answerControls = container.createDiv({ cls: "notre-compagnon-answer-controls" });
    answerControls.style.display = this.lastAssistantAnswer ? "flex" : "none";
    answerControls.style.flexWrap = "wrap";
    answerControls.style.gap = "6px";
    answerControls.style.margin = "6px 0";
    this.chatActionsEl = answerControls;

    const insertButton = answerControls.createEl("button", { text: "Inserer" });
    insertButton.style.padding = "4px 10px";
    insertButton.addEventListener("click", async () => {
      try {
        await this.insertLastAssistantAnswer();
      } catch (error) {
        this.plugin.showUserFacingError(error);
      }
    });

    const copyButton = answerControls.createEl("button", { text: "Copier" });
    copyButton.style.padding = "4px 10px";
    copyButton.addEventListener("click", async () => {
      try {
        await copyToClipboard(this.lastAssistantAnswer);
        new Notice("Assistant response copied.");
      } catch (error) {
        this.plugin.showUserFacingError(error);
      }
    });

    container.createEl("h3", { text: "Templates" });
    this.addActionGrid(container, [
      ["Installer templates", async () => {
        const installSet = await chooseTemplateInstallSet(this.app);
        await this.plugin.installRecommendedTemplates(installSet);
      }],
      ["Ouvrir templates", async () => this.plugin.openConfiguredFolder(this.plugin.getTemplatesFolder())],
      [this.plugin.settings.dashboardTemplatesExpanded ? "Masquer les templates" : "Afficher les templates", async () => {
        this.plugin.settings.dashboardTemplatesExpanded = !this.plugin.settings.dashboardTemplatesExpanded;
        await this.plugin.saveSettings();
        await this.render();
      }],
    ]);
    if (this.plugin.settings.dashboardTemplatesExpanded) {
      await this.renderTemplateList(container);
    }

    container.createEl("h3", { text: "Connaissance du vault" });
    new Setting(container)
      .setName("Resume")
      .setDesc(`Vault ID: ${this.plugin.getVaultId()} | RAG: ${this.plugin.settings.ragEnabled ? "active" : "desactive"}${this.lastVaultStats ? ` | ${this.lastVaultStats.documents} documents / ${this.lastVaultStats.chunks} chunks` : ""}`);
    this.addActionGrid(container, [[this.plugin.settings.dashboardVaultExpanded ? "Masquer" : "Afficher", async () => {
      this.plugin.settings.dashboardVaultExpanded = !this.plugin.settings.dashboardVaultExpanded;
      await this.plugin.saveSettings();
      await this.render();
    }]]);
    if (this.plugin.settings.dashboardVaultExpanded) {
      this.addActionGrid(container, [
        ["Indexer note", async () => this.plugin.indexCurrentNote()],
        ["Indexer dossier", async () => this.plugin.indexCurrentFolder()],
        ["Indexer vault", async () => this.plugin.indexWholeVault()],
        ["Statistiques", async () => {
          this.lastVaultStats = await this.plugin.getVaultStats();
          new Notice(`Vault: ${this.lastVaultStats.documents} documents, ${this.lastVaultStats.chunks} chunks.`);
          await this.render();
        }],
        ["Supprimer index", async () => this.confirmAndDeleteVaultIndex()],
      ]);
    }

    container.createEl("h3", { text: "Etat" });
    const status = this.plugin.getConfigurationStatus();
    this.addActionGrid(container, [["Tester", async () => this.plugin.testConnection()]]);
    new Setting(container).setName("Backend").setDesc(status.isReady ? "Configuration prete" : status.label);
    this.addActionGrid(container, [[this.plugin.settings.dashboardStatusExpanded ? "Masquer les details" : "Afficher les details", async () => {
      this.plugin.settings.dashboardStatusExpanded = !this.plugin.settings.dashboardStatusExpanded;
      await this.plugin.saveSettings();
      await this.render();
    }]]);
    if (this.plugin.settings.dashboardStatusExpanded) {
      new Setting(container).setName("API Base URL").setDesc(this.plugin.settings.apiBaseUrl || "Not configured");
      new Setting(container).setName("Modele actif").setDesc(this.plugin.settings.defaultModel || "Not configured");
      new Setting(container).setName("Transcription").setDesc(formatTranscriptionLanguageLabel(this.plugin.getTranscriptionLanguage()));
      new Setting(container).setName("Sortie").setDesc(formatOutputLanguageLabel(this.plugin.getOutputLanguage()));
      new Setting(container).setName("Vault ID").setDesc(this.plugin.getVaultId());
      new Setting(container).setName("Connaissance du vault").setDesc(this.plugin.settings.ragEnabled ? "activee" : "desactivee");
      new Setting(container)
        .setName("Mode audio")
        .setDesc(formatRecordingSourceLabel(this.plugin.getRecordingSource(), this.plugin.getSelectedMicrophoneInputLabel(), this.plugin.getSelectedComputerAudioInputLabel()));
      new Setting(container).setName("Microphone").setDesc(this.plugin.getSelectedMicrophoneInputLabel());
      new Setting(container).setName("Son ordinateur").setDesc(this.plugin.getSelectedComputerAudioInputLabel());
    }
  }

  private addActionGrid(container: HTMLElement, actions: Array<[string, () => Promise<void>]>): void {
    const grid = container.createDiv({ cls: "notre-compagnon-action-grid" });
    grid.style.display = "grid";
    grid.style.gridTemplateColumns = "repeat(auto-fit, minmax(120px, 1fr))";
    grid.style.gap = "6px";
    grid.style.margin = "8px 0";
    for (const [label, action] of actions) {
      const button = grid.createEl("button", { text: label });
      button.style.padding = "4px 8px";
      button.addEventListener("click", async () => {
        try {
          await action();
        } catch (error) {
          this.plugin.showUserFacingError(error);
        }
      });
    }
  }

  private async sendChatMessage(message: string): Promise<void> {
    try {
      if (!message.trim()) {
        throw new UserFacingError("Ecris un message avant d'envoyer.");
      }
      new Notice("Note Compagnon reflechit...");
      const response = await this.plugin.askDashboardAssistant(message, this.assistantResponseMode);
      this.lastAssistantAnswer = response.answerMarkdown;
      this.lastVaultSources = response.sources;
      await this.renderAssistantAnswer();
      if (this.chatActionsEl) {
        this.chatActionsEl.style.display = "flex";
      }
    } catch (error) {
      this.plugin.showUserFacingError(error);
    }
  }

  private async insertLastAssistantAnswer(): Promise<void> {
    if (!this.lastAssistantAnswer.trim()) {
      throw new UserFacingError("Aucune reponse assistant a inserer.");
    }
    const view = this.app.workspace.getActiveViewOfType(MarkdownView);
    if (!view) {
      throw new UserFacingError("Ouvre une note avant d'inserer la reponse.");
    }
    view.editor.replaceSelection(this.lastAssistantAnswer);
  }

  private async renderAssistantAnswer(): Promise<void> {
    if (!this.chatResponseEl) {
      return;
    }
    this.chatResponseEl.empty();
    if (!this.lastAssistantAnswer.trim()) {
      this.chatResponseEl.style.display = "none";
      return;
    }
    this.chatResponseEl.style.display = "block";
    await MarkdownRenderer.renderMarkdown(this.lastAssistantAnswer, this.chatResponseEl, "", this);
    if (this.lastVaultSources.length > 0) {
      this.renderVaultSources(this.chatResponseEl, this.lastVaultSources);
    } else if (this.assistantResponseMode === "vault") {
      const empty = this.chatResponseEl.createEl("p");
      empty.textContent = "Je n'ai pas trouve assez d'informations dans l'index du vault. Essaie d'indexer le vault ou de reformuler la question.";
    }
    styleAssistantAnswer(this.chatResponseEl);
  }

  private renderVaultSources(container: HTMLElement, sources: VaultSourcePayload[]): void {
    const details = container.createEl("details");
    details.open = true;
    details.createEl("summary", { text: "Sources utilisees" });
    const list = details.createEl("ul");
    for (const source of sources) {
      const item = list.createEl("li");
      const file = this.app.vault.getAbstractFileByPath(source.path);
      const label = `${source.title || source.path}${source.heading_path ? ` - ${source.heading_path}` : ""}`;
      if (file instanceof TFile) {
        const link = item.createEl("a", { text: label });
        link.href = "#";
        link.addEventListener("click", async (event) => {
          event.preventDefault();
          await this.app.workspace.getLeaf(true).openFile(file);
        });
      } else {
        item.createSpan({ text: label });
      }
      item.createSpan({ text: ` (score ${source.score.toFixed(2)}, chunk ${source.chunk_index})` });
    }
  }

  private async confirmAndDeleteVaultIndex(): Promise<void> {
    const confirmed = window.confirm(`Supprimer l'index RAG du vault '${this.plugin.getVaultId()}' ? Aucune note Obsidian ne sera supprimee.`);
    if (!confirmed) {
      return;
    }
    const result = await this.plugin.deleteVaultIndex();
    this.lastVaultStats = null;
    new Notice(`Index supprime: ${result.deleted_documents} documents, ${result.deleted_chunks} chunks.`);
    await this.render();
  }

  private async runSelectionAction(mode: "correct" | "rewrite" | "summarize"): Promise<void> {
    const view = this.app.workspace.getActiveViewOfType(MarkdownView);
    if (!view) {
      throw new UserFacingError("Ouvre une note et selectionne un texte.");
    }
    await this.plugin.runAssistantOnSelection(view.editor, mode);
  }

  private async renderTemplateList(container: HTMLElement): Promise<void> {
    const choices = await this.plugin.listTemplateChoices();
    for (const choice of choices.filter((item) => item.sourcePath !== null)) {
      const details = [
        choice.language ? `language: ${choice.language}` : null,
        choice.type ? `type: ${choice.type}` : null,
        choice.description,
        choice.sourcePath,
      ].filter((item): item is string => Boolean(item));
      new Setting(container).setName(choice.label).setDesc(details.join(" | "));
    }
  }

  private addActionButton(container: HTMLElement, label: string, action: () => Promise<void>): void {
    new Setting(container).setName(label).addButton((button) =>
      button.setButtonText(label).onClick(async () => {
        try {
          await action();
        } catch (error) {
          this.plugin.showUserFacingError(error);
        } finally {
          await this.render();
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

    containerEl.createEl("h2", { text: "Note Compagnon" });

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
      .setDesc("Sent to the gateway for each audio transcription job.")
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
      .setDesc("Sent to the gateway for meeting generation prompts.")
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
      .setName("Quick actions language")
      .setDesc("Used for correction, rewriting, and selected-text summaries. Same as input keeps French text in French and English text in English.")
      .addDropdown((dropdown) =>
        dropdown
          .addOption("same_as_input", "Same as input")
          .addOption("fr", "French")
          .addOption("en", "English")
          .setValue(this.plugin.settings.quickActionsLanguage)
          .onChange(async (value) => {
            this.plugin.settings.quickActionsLanguage = value as PluginSettings["quickActionsLanguage"];
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Preferred template language")
      .setDesc("Used to reduce template noise when output language is Same as meeting.")
      .addDropdown((dropdown) =>
        dropdown
          .addOption("auto", "Auto / all")
          .addOption("fr", "French")
          .addOption("en", "English")
          .setValue(this.plugin.settings.preferredTemplateLanguage)
          .onChange(async (value) => {
            this.plugin.settings.preferredTemplateLanguage = value as PluginSettings["preferredTemplateLanguage"];
            await this.plugin.saveSettings();
          }),
      );

    containerEl.createEl("h3", { text: "Connaissance du vault" });
    new Setting(containerEl)
      .setName("Activer la connaissance du vault")
      .setDesc("Active les boutons d'indexation et le mode Assistant Avec le vault. Aucun index automatique n'est lance.")
      .addToggle((toggle) =>
        toggle.setValue(this.plugin.settings.ragEnabled).onChange(async (value) => {
          this.plugin.settings.ragEnabled = value;
          await this.plugin.saveSettings();
        }),
      );

    new Setting(containerEl)
      .setName("Identifiant du vault")
      .setDesc("Envoye au backend RAG pour isoler l'index.")
      .addText((text) =>
        text
          .setPlaceholder("default")
          .setValue(this.plugin.settings.vaultId)
          .onChange(async (value) => {
            this.plugin.settings.vaultId = value.trim() || "default";
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Dossiers exclus de l'index")
      .setDesc("Liste separee par virgules. Les notes dans ces dossiers ne sont jamais envoyees au RAG.")
      .addText((text) =>
        text
          .setPlaceholder(DEFAULT_SETTINGS.ragExcludedFolders)
          .setValue(this.plugin.settings.ragExcludedFolders)
          .onChange(async (value) => {
            this.plugin.settings.ragExcludedFolders = value.trim() || DEFAULT_SETTINGS.ragExcludedFolders;
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Tags exclus de l'index")
      .setDesc("Liste separee par virgules. Une note avec un de ces tags est ignoree.")
      .addText((text) =>
        text
          .setPlaceholder(DEFAULT_SETTINGS.ragExcludedTags)
          .setValue(this.plugin.settings.ragExcludedTags)
          .onChange(async (value) => {
            this.plugin.settings.ragExcludedTags = value.trim() || DEFAULT_SETTINGS.ragExcludedTags;
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Taille maximale d'une note a indexer")
      .setDesc("Nombre maximal de caracteres par note envoyee a /v1/vault/index-note.")
      .addText((text) =>
        text
          .setPlaceholder(String(DEFAULT_SETTINGS.ragMaxFileChars))
          .setValue(String(this.plugin.settings.ragMaxFileChars))
          .onChange(async (value) => {
            const parsed = Number(value);
            this.plugin.settings.ragMaxFileChars = Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_SETTINGS.ragMaxFileChars;
            await this.plugin.saveSettings();
          }),
      );

    containerEl.createEl("h3", { text: "Audio de reunion" });
    containerEl.createEl("h4", { text: "Mode d'enregistrement" });
    new Setting(containerEl)
      .setName("Mode d'enregistrement")
      .setDesc("Pour Teams, utilise de preference Micro + son ordinateur avec Microphone = ton micro et Son ordinateur = Mixage stereo.")
      .addDropdown((dropdown) =>
        dropdown
          .addOption("microphone_only", "Micro seul")
          .addOption("computer_audio_only", "Son ordinateur seul")
          .addOption("microphone_plus_computer_audio", "Micro + son ordinateur")
          .addOption("experimental_system_capture", "Capture systeme experimentale (avance)")
          .setValue(this.plugin.settings.recordingSource)
          .onChange(async (value) => {
            this.plugin.settings.recordingSource = value as RecordingSource;
            await this.plugin.saveSettings();
            this.display();
          }),
      );

    if (this.plugin.getRecordingSource() === "experimental_system_capture") {
      new Setting(containerEl)
        .setName("Option avancee")
        .setDesc("Avance : depend d'Obsidian/Electron, peut ne pas fonctionner.");
    }

    containerEl.createEl("h4", { text: "Sources audio" });
    new Setting(containerEl)
      .setName("Microphone")
      .setDesc("Ta voix. Selectionne ton micro physique.")
      .addDropdown((dropdown) => {
        for (const device of this.plugin.getAudioInputDeviceChoices()) {
          dropdown.addOption(device.deviceId, device.label);
        }
        return dropdown.setValue(this.plugin.settings.microphoneInputDeviceId).onChange(async (value) => {
          const selected = this.plugin.getAudioInputDeviceChoices().find((device) => device.deviceId === value);
          this.plugin.settings.microphoneInputDeviceId = value;
          this.plugin.settings.microphoneInputDeviceLabel = selected?.label ?? "Selected microphone";
          await this.plugin.saveSettings();
        });
      });

    new Setting(containerEl)
      .setName("Son ordinateur")
      .setDesc(this.plugin.getRecordingSource() === "microphone_only"
        ? "Son de l'ordinateur. Non utilise en mode Micro seul."
        : "Son de l'ordinateur. Selectionne Mixage stereo / Stereo Mix si disponible.")
      .addDropdown((dropdown) => {
        for (const device of this.plugin.getComputerAudioInputDeviceChoices()) {
          dropdown.addOption(device.deviceId, device.label);
        }
        return dropdown.setValue(this.plugin.settings.computerAudioInputDeviceId).onChange(async (value) => {
          const selected = this.plugin.getComputerAudioInputDeviceChoices().find((device) => device.deviceId === value);
          this.plugin.settings.computerAudioInputDeviceId = value;
          this.plugin.settings.computerAudioInputDeviceLabel = value ? stripRecommendedSuffix(selected?.label ?? "Selected computer audio input") : "Not configured";
          await this.plugin.saveSettings();
        });
      });

    containerEl.createEl("h4", { text: "Tests" });
    const audioTestsSetting = new Setting(containerEl)
      .setName("Peripheriques audio")
      .setDesc("Actualise les listes, puis teste chaque source avant une reunion.")
      .addButton((button) =>
        button.setButtonText("Actualiser les peripheriques").onClick(async () => {
          try {
            await this.plugin.refreshAudioInputDevices(true);
            this.display();
          } catch (error) {
            this.plugin.showUserFacingError(error);
          }
        }),
      )
      .addButton((button) =>
        button.setButtonText("Tester le micro").onClick(async () => {
          try {
            await this.plugin.testAudioInput("microphone");
          } catch (error) {
            this.plugin.showUserFacingError(error);
          }
        }),
      )
      .addButton((button) =>
        button.setButtonText("Tester le son ordinateur").onClick(async () => {
          try {
            await this.plugin.testAudioInput("computer");
          } catch (error) {
            this.plugin.showUserFacingError(error);
          }
        }),
      );
    audioTestsSetting.controlEl.style.flexWrap = "wrap";
    audioTestsSetting.controlEl.style.gap = "6px";

    new Setting(containerEl)
      .setName("Configuration recommandee pour Teams")
      .setDesc(createAudioHelpFragment());

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
  private languageFilter: "all" | "fr" | "en";

  constructor(app: App, choices: TemplateChoice[], languageFilter: "all" | "fr" | "en", onChoose: (choice: TemplateChoice) => void) {
    super(app);
    this.choices = choices;
    this.languageFilter = languageFilter;
    this.onChoose = onChoose;
  }

  onOpen(): void {
    const { contentEl } = this;
    contentEl.empty();
    contentEl.createEl("h2", { text: "Choose a template" });
    contentEl.createEl("p", { text: "Pick the Markdown template to send with this request." });
    new Setting(contentEl)
      .setName("Language filter")
      .addDropdown((dropdown) =>
        dropdown
          .addOption("all", "Auto / Tous")
          .addOption("fr", "Francais")
          .addOption("en", "English")
          .setValue(this.languageFilter)
          .onChange((value) => {
            this.languageFilter = value as "all" | "fr" | "en";
            this.onOpen();
          }),
      );

    const visibleChoices = this.choices.filter((choice) => this.languageFilter === "all" || !choice.language || choice.language === this.languageFilter);
    const groups = groupTemplateChoices(visibleChoices);
    for (const [group, choices] of groups) {
      contentEl.createEl("h3", { text: formatTemplateGroup(group) });
      for (const choice of choices) {
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

class AssistantPreviewModal extends Modal {
  private readonly markdown: string;
  private readonly onAction: (action: "replace" | "insert" | "copy") => void;

  constructor(app: App, markdown: string, onAction: (action: "replace" | "insert" | "copy") => void) {
    super(app);
    this.markdown = markdown;
    this.onAction = onAction;
  }

  onOpen(): void {
    const { contentEl } = this;
    contentEl.empty();
    contentEl.createEl("h2", { text: "Note Compagnon - Preview" });
    const preview = contentEl.createEl("pre");
    preview.setText(this.markdown);
    preview.style.whiteSpace = "pre-wrap";

    new Setting(contentEl)
      .addButton((button) =>
        button.setButtonText("Remplacer la selection").setCta().onClick(() => {
          this.onAction("replace");
          this.close();
        }),
      )
      .addButton((button) =>
        button.setButtonText("Inserer sous la selection").onClick(() => {
          this.onAction("insert");
          this.close();
        }),
      )
      .addButton((button) =>
        button.setButtonText("Copier").onClick(() => {
          this.onAction("copy");
          this.close();
        }),
      )
      .addButton((button) => button.setButtonText("Annuler").onClick(() => this.close()));
  }

  onClose(): void {
    this.contentEl.empty();
  }
}

class SystemAudioExplanationModal extends Modal {
  private readonly onChoose: (shouldContinue: boolean) => void;

  constructor(app: App, onChoose: (shouldContinue: boolean) => void) {
    super(app);
    this.onChoose = onChoose;
  }

  onOpen(): void {
    const { contentEl } = this;
    contentEl.empty();
    contentEl.createEl("h2", { text: "Capture du son ordinateur" });
    contentEl.createEl("p", {
      text: "Note Compagnon va tenter une capture systeme experimentale. Selon Obsidian/Electron, une fenetre de selection peut apparaitre.",
    });
    contentEl.createEl("p", {
      text: "Si aucune piste audio n'est fournie, cette option sera indisponible. Cherche d'abord une entree Windows comme Mixage stereo / Stereo Mix.",
    });
    new Setting(contentEl)
      .addButton((button) =>
        button.setButtonText("Continuer").setCta().onClick(() => {
          this.onChoose(true);
          this.close();
        }),
      )
      .addButton((button) =>
        button.setButtonText("Annuler").onClick(() => {
          this.onChoose(false);
          this.close();
        }),
      );
  }
}

class TemplateInstallModal extends Modal {
  private readonly onChoose: (installSet: TemplateInstallSet) => void;

  constructor(app: App, onChoose: (installSet: TemplateInstallSet) => void) {
    super(app);
    this.onChoose = onChoose;
  }

  onOpen(): void {
    const { contentEl } = this;
    contentEl.empty();
    contentEl.createEl("h2", { text: "Installer les templates recommandes" });
    const options: Array<[TemplateInstallSet, string, string]> = [
      ["minimal", "Minimal recommande", "Meeting note FR/EN et compte rendu FR/EN."],
      ["fr", "Seulement francais", "Templates recommandes en francais."],
      ["en", "Seulement anglais", "Recommended English templates."],
      ["all", "Tous les templates", "Installe toutes les variantes fournies."],
    ];
    for (const [installSet, label, description] of options) {
      new Setting(contentEl).setName(label).setDesc(description).addButton((button) =>
        button.setButtonText("Installer").onClick(() => {
          this.onChoose(installSet);
          this.close();
        }),
      );
    }
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

function isAssistantChatResponsePayload(value: unknown): value is AssistantChatResponsePayload {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const candidate = value as Partial<AssistantChatResponsePayload>;
  return (
    typeof candidate.model === "string" &&
    typeof candidate.mode === "string" &&
    typeof candidate.answer_markdown === "string" &&
    typeof candidate.usage === "object" &&
    candidate.usage !== null &&
    typeof candidate.usage.message_chars === "number" &&
    typeof candidate.usage.context_chars === "number"
  );
}

function isVaultIndexNoteResponsePayload(value: unknown): value is VaultIndexNoteResponsePayload {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Partial<VaultIndexNoteResponsePayload>;
  return (
    (candidate.status === "indexed" || candidate.status === "skipped") &&
    typeof candidate.document_id === "string" &&
    typeof candidate.path === "string" &&
    typeof candidate.chunks_indexed === "number" &&
    typeof candidate.content_hash === "string"
  );
}

function isVaultSourcePayload(value: unknown): value is VaultSourcePayload {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Partial<VaultSourcePayload>;
  return (
    typeof candidate.path === "string" &&
    (typeof candidate.title === "string" || candidate.title === null) &&
    (typeof candidate.heading_path === "string" || candidate.heading_path === null) &&
    typeof candidate.chunk_index === "number" &&
    typeof candidate.score === "number"
  );
}

function isVaultAskResponsePayload(value: unknown): value is VaultAskResponsePayload {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Partial<VaultAskResponsePayload>;
  return (
    typeof candidate.model === "string" &&
    typeof candidate.answer_markdown === "string" &&
    Array.isArray(candidate.sources) &&
    candidate.sources.every(isVaultSourcePayload)
  );
}

function isVaultStatsResponsePayload(value: unknown): value is VaultStatsResponsePayload {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Partial<VaultStatsResponsePayload>;
  return (
    typeof candidate.vault_id === "string" &&
    typeof candidate.documents === "number" &&
    typeof candidate.chunks === "number" &&
    (typeof candidate.last_indexed_at === "string" || candidate.last_indexed_at === null)
  );
}

function isVaultDeleteResponsePayload(value: unknown): value is VaultDeleteResponsePayload {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Partial<VaultDeleteResponsePayload>;
  return typeof candidate.vault_id === "string" && typeof candidate.deleted_documents === "number" && typeof candidate.deleted_chunks === "number";
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

function parseSimpleFrontmatter(content: string): { frontmatter: Record<string, unknown>; body: string } {
  const normalized = content.replace(/^\uFEFF/, "");
  if (!normalized.startsWith("---\n")) {
    return { frontmatter: {}, body: normalized };
  }
  const closingIndex = normalized.indexOf("\n---", 4);
  if (closingIndex < 0) {
    return { frontmatter: {}, body: normalized };
  }
  const rawFrontmatter = normalized.slice(4, closingIndex);
  const frontmatter: Record<string, unknown> = {};
  let currentListKey: string | null = null;
  try {
    for (const line of rawFrontmatter.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) {
        continue;
      }
      if (currentListKey && trimmed.startsWith("- ")) {
        const list = frontmatter[currentListKey];
        if (Array.isArray(list)) {
          list.push(parseFrontmatterScalar(trimmed.slice(2).trim()));
        }
        continue;
      }
      currentListKey = null;
      const separatorIndex = line.indexOf(":");
      if (separatorIndex < 0) {
        continue;
      }
      const key = line.slice(0, separatorIndex).trim();
      const rawValue = line.slice(separatorIndex + 1).trim();
      if (!key) {
        continue;
      }
      if (!rawValue) {
        frontmatter[key] = [];
        currentListKey = key;
      } else {
        frontmatter[key] = parseFrontmatterScalar(rawValue);
      }
    }
  } catch {
    return { frontmatter: {}, body: normalized };
  }
  return {
    frontmatter,
    body: normalized.slice(closingIndex + 4).replace(/^\r?\n/, ""),
  };
}

function parseFrontmatterScalar(value: string): unknown {
  const cleaned = value.trim().replace(/^["']|["']$/g, "");
  if (cleaned === "false") return false;
  if (cleaned === "true") return true;
  if (cleaned.startsWith("[") && cleaned.endsWith("]")) {
    return cleaned
      .slice(1, -1)
      .split(",")
      .map((item) => item.trim().replace(/^["']|["']$/g, ""))
      .filter(Boolean);
  }
  return cleaned;
}

function extractTagsFromFrontmatter(frontmatter: Record<string, unknown>): string[] {
  const rawTags = frontmatter.tags ?? frontmatter.tag;
  if (Array.isArray(rawTags)) {
    return rawTags.filter((tag): tag is string => typeof tag === "string").map((tag) => tag.replace(/^#/, ""));
  }
  if (typeof rawTags === "string") {
    return rawTags
      .split(/[,\s]+/)
      .map((tag) => tag.trim().replace(/^#/, ""))
      .filter(Boolean);
  }
  return [];
}

function parseCommaSeparatedList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function filterRecommendedTemplates(installSet: TemplateInstallSet): Array<{ fileName: string; content: string; language: "fr" | "en"; minimal: boolean }> {
  if (installSet === "minimal") {
    return RECOMMENDED_TEMPLATES.filter((template) => template.minimal || MINIMAL_RECOMMENDED_TEMPLATE_FILES.has(template.fileName));
  }
  if (installSet === "fr" || installSet === "en") {
    return RECOMMENDED_TEMPLATES.filter((template) => template.language === installSet);
  }
  return RECOMMENDED_TEMPLATES;
}

function inferTemplateGroup(type: string | null, fileName: string): TemplateGroup {
  const normalized = `${type ?? ""} ${fileName}`.toLowerCase();
  if (normalized.includes("meeting_note") || normalized.includes("meeting-note") || normalized.includes("type: meeting")) {
    return "meeting_note";
  }
  if (normalized.includes("action")) {
    return "actions";
  }
  if (normalized.includes("technical") || normalized.includes("technique")) {
    return "technical";
  }
  if (normalized.includes("client")) {
    return "client";
  }
  if (normalized.includes("meeting_summary") || normalized.includes("minutes") || normalized.includes("compte-rendu")) {
    return "meeting_summary";
  }
  return "other";
}

function groupTemplateChoices(choices: TemplateChoice[]): Array<[TemplateGroup, TemplateChoice[]]> {
  const order: TemplateGroup[] = ["meeting_note", "meeting_summary", "actions", "technical", "client", "other"];
  return order
    .map((group): [TemplateGroup, TemplateChoice[]] => [group, choices.filter((choice) => choice.group === group)])
    .filter(([, groupedChoices]) => groupedChoices.length > 0);
}

function formatTemplateGroup(group: TemplateGroup): string {
  if (group === "meeting_note") return "Meeting notes";
  if (group === "meeting_summary") return "Meeting summaries";
  if (group === "actions") return "Actions";
  if (group === "technical") return "Technical";
  if (group === "client") return "Client";
  return "Other";
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

function cleanGeneratedMarkdown(markdown: string): string {
  let cleaned = markdown.replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
  let removedGlobalFence = false;
  let removedPromptSections = false;

  const fencedMatch = cleaned.match(/^```(?:markdown|md)?[ \t]*\n([\s\S]*?)\n```[ \t]*$/i);
  if (fencedMatch) {
    cleaned = fencedMatch[1].trim();
    removedGlobalFence = true;
  }

  const sectionResult = removePromptLeakSections(cleaned);
  cleaned = sectionResult.markdown.trim();
  removedPromptSections = sectionResult.removed;

  if (removedGlobalFence || removedPromptSections) {
    console.warn("Note Compagnon cleaned generated markdown.", {
      removedGlobalFence,
      removedPromptSections,
    });
  }

  return cleaned.replace(/\n{3,}/g, "\n\n").trim();
}

function removePromptLeakSections(markdown: string): { markdown: string; removed: boolean } {
  const lines = markdown.split("\n");
  const kept: string[] = [];
  let removed = false;
  let skipping = false;
  let skipHeadingLevel = 0;

  for (const line of lines) {
    const heading = line.match(/^(#{1,6})\s+(.+?)\s*$/);
    if (skipping) {
      if (heading && heading[1].length <= skipHeadingLevel && !isPromptLeakTitle(heading[2])) {
        skipping = false;
      } else {
        removed = true;
        continue;
      }
    }

    if (heading && isPromptLeakTitle(heading[2])) {
      skipping = true;
      skipHeadingLevel = heading[1].length;
      removed = true;
      continue;
    }

    if (isPromptLeakLine(line)) {
      skipping = true;
      skipHeadingLevel = 6;
      removed = true;
      continue;
    }

    kept.push(line);
  }

  return { markdown: kept.join("\n"), removed };
}

function isPromptLeakTitle(title: string): boolean {
  const normalized = title.trim().replace(/:$/, "").toLowerCase();
  return normalized === "language instruction" || normalized === "manual notes" || normalized === "transcript";
}

function isPromptLeakLine(line: string): boolean {
  const normalized = line.trim().toLowerCase();
  return (
    normalized === "language instruction" ||
    normalized.startsWith("manual notes (priority source") ||
    normalized.startsWith("transcript (primary source")
  );
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

function formatRecordingSourceLabel(recordingSource: RecordingSource, microphoneLabel: string, computerAudioLabel: string): string {
  if (recordingSource === "computer_audio_only" || recordingSource === "selected_audio_input") {
    return `Son ordinateur seul (${computerAudioLabel})`;
  }
  if (recordingSource === "microphone_plus_computer_audio") {
    return `Micro + son ordinateur (${microphoneLabel} + ${computerAudioLabel})`;
  }
  if (recordingSource === "experimental_system_capture") {
    return "Capture systeme experimentale";
  }
  return `Micro seul (${microphoneLabel})`;
}

function findLikelySystemAudioInput(devices: AudioInputDeviceChoice[]): AudioInputDeviceChoice | null {
  const patterns = ["mixage stereo", "stereo mix", "what u hear", "loopback", "monitor", "mix"];
  return (
    devices.find((device) => {
      const label = normalizeDeviceLabelForMatch(device.label);
      return patterns.some((pattern) => label.includes(pattern));
    }) ?? null
  );
}

function formatComputerAudioInputLabel(label: string): string {
  if (!label || label === "Default microphone") {
    return "Non configure";
  }
  return isLikelySystemAudioInputLabel(label) ? `${label} (recommande)` : label;
}

function stripRecommendedSuffix(label: string): string {
  return label.replace(/\s+\(recommande\)$/i, "");
}

function isLikelySystemAudioInputLabel(label: string): boolean {
  const patterns = ["mixage stereo", "stereo mix", "what u hear", "loopback", "monitor", "mix"];
  const normalized = normalizeDeviceLabelForMatch(label);
  return patterns.some((pattern) => normalized.includes(pattern));
}

function normalizeDeviceLabelForMatch(label: string): string {
  return label
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
}

function styleAssistantAnswer(container: HTMLElement): void {
  container.style.whiteSpace = "normal";
  container.style.lineHeight = "1.55";
  container.style.margin = "10px 0";
  container.style.padding = "10px 12px";
  container.style.border = "1px solid var(--background-modifier-border)";
  container.style.borderRadius = "8px";
  container.style.backgroundColor = "var(--background-secondary)";

  for (const heading of Array.from(container.querySelectorAll("h1, h2, h3, h4"))) {
    (heading as HTMLElement).style.margin = "0.8em 0 0.35em";
    (heading as HTMLElement).style.lineHeight = "1.25";
  }
  for (const paragraph of Array.from(container.querySelectorAll("p"))) {
    (paragraph as HTMLElement).style.margin = "0.45em 0";
  }
  for (const list of Array.from(container.querySelectorAll("ul, ol"))) {
    (list as HTMLElement).style.margin = "0.45em 0 0.45em 1.2em";
  }
  for (const codeBlock of Array.from(container.querySelectorAll("pre"))) {
    (codeBlock as HTMLElement).style.whiteSpace = "pre-wrap";
  }
}

function createAudioHelpFragment(): DocumentFragment {
  const fragment = document.createDocumentFragment();
  const list = document.createElement("ol");
  for (const item of [
    "Mode d'enregistrement : Micro + son ordinateur",
    "Microphone : ton micro physique",
    "Son ordinateur : Mixage stereo / Stereo Mix",
    "Clique sur Tester le micro, puis Tester le son ordinateur",
  ]) {
    const li = document.createElement("li");
    li.textContent = item;
    list.appendChild(li);
  }
  fragment.appendChild(list);

  const listenNote = document.createElement("p");
  listenNote.textContent = "Il n'est pas necessaire d'activer 'Ecouter ce peripherique' dans Windows. Note Compagnon mixe les deux sources lui-meme.";
  fragment.appendChild(listenNote);

  const driverNote = document.createElement("p");
  driverNote.textContent = "Si Mixage stereo n'apparait pas ou ne recoit aucun son, le probleme vient du pilote audio ou de la sortie utilisee.";
  fragment.appendChild(driverNote);

  return fragment;
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
  return `# Note Compagnon - Summary - ${input.title}

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
  recordingSourceRequested: string;
  recordingSourceUsed: string;
  microphoneInputDeviceLabel: string;
  computerAudioInputDeviceLabel: string;
}): string {
  return `# ${input.title}

Date: ${formatIsoTimestamp(input.startedAt)}
Recording status: ${input.recordingStatus}
recording_source_requested: ${input.recordingSourceRequested}
recording_source_used: ${input.recordingSourceUsed}
microphone_input_device_label: ${input.microphoneInputDeviceLabel}
computer_audio_input_device_label: ${input.computerAudioInputDeviceLabel}

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
  transcriptionLanguage: string;
  jobId: string;
  audioFileName: string;
  sourceMeetingLink: string | null;
  sourceAudioLink: string | null;
  recordingSourceUsed: string;
  meetingMarkdown: string;
}): string {
  const sourceMeeting = input.sourceMeetingLink ? `[[${input.sourceMeetingLink}]]` : "";
  const sourceAudio = input.sourceAudioLink ? `[[${input.sourceAudioLink}]]` : input.audioFileName;
  const linksSection = buildUsefulLinksSection(sourceMeeting, sourceAudio);

  return `---
type: meeting_summary
source_meeting: ${yamlQuote(sourceMeeting)}
source_audio: ${yamlQuote(sourceAudio)}
model: ${yamlQuote(input.model)}
template: ${yamlQuote(input.templateLabel)}
transcription_language: ${yamlQuote(input.transcriptionLanguage)}
output_language: ${yamlQuote(input.outputLanguage)}
recording_source_used: ${yamlQuote(input.recordingSourceUsed)}
job_id: ${yamlQuote(input.jobId)}
created: ${formatDate(input.generatedAt)}
tags:
  - meeting
  - compte-rendu
---
# Note Compagnon - Compte rendu - ${input.title}

${input.meetingMarkdown.trim()}

${linksSection}
`;
}

function buildUsefulLinksSection(sourceMeeting: string, sourceAudio: string): string {
  const links = [
    sourceMeeting ? `- Note source : ${sourceMeeting}` : null,
    sourceAudio ? `- Audio : ${sourceAudio.startsWith("[[") ? sourceAudio : sourceAudio}` : null,
  ].filter((line): line is string => line !== null);

  return ["## Liens utiles", ...links].join("\n");
}

function yamlQuote(value: string): string {
  return `"${value.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

function chooseTemplateWithModal(app: App, choices: TemplateChoice[], languageFilter: "all" | "fr" | "en"): Promise<TemplateChoice> {
  return new Promise((resolve, reject) => {
    let resolved = false;
    const modal = new TemplatePickerModal(app, choices, languageFilter, (choice) => {
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

function chooseTemplateInstallSet(app: App): Promise<TemplateInstallSet> {
  return new Promise((resolve, reject) => {
    let resolved = false;
    const modal = new TemplateInstallModal(app, (installSet) => {
      resolved = true;
      resolve(installSet);
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

function confirmSystemAudioCapture(app: App): Promise<boolean> {
  return new Promise((resolve) => {
    new SystemAudioExplanationModal(app, resolve).open();
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

async function captureSystemAudio(): Promise<MediaStream | null> {
  if (!navigator.mediaDevices?.getDisplayMedia) {
    console.warn("Note Compagnon system audio capture unavailable: getDisplayMedia missing.");
    return null;
  }

  const strategies: Array<() => Promise<MediaStream>> = [
    () => navigator.mediaDevices.getDisplayMedia({ video: true, audio: true }),
    () =>
      navigator.mediaDevices.getDisplayMedia({
        video: true,
        audio: { systemAudio: "include" },
      } as unknown as DisplayMediaStreamOptions),
    () => captureSystemAudioWithElectronDesktopCapturer(),
  ];

  for (const strategy of strategies) {
    try {
      const stream = await strategy();
      const audioOnlyStream = extractAudioOnlyStream(stream);
      if (audioOnlyStream) {
        return audioOnlyStream;
      }
    } catch (error) {
      if (isDomExceptionName(error, "NotAllowedError")) {
        throw new UserFacingError("Autorisation refusee pour la capture systeme.");
      }
      if (isDomExceptionName(error, "NotSupportedError")) {
        console.warn("Note Compagnon system audio capture unsupported by this Obsidian environment.");
        continue;
      }
      console.warn("Note Compagnon system audio capture strategy failed.", {
        errorName: error instanceof Error ? error.name : "unknown",
      });
    }
  }

  return null;
}

async function captureSystemAudioWithElectronDesktopCapturer(): Promise<MediaStream> {
  const maybeWindow = window as unknown as {
    require?: (moduleName: string) => unknown;
  };
  if (typeof maybeWindow.require !== "function") {
    throw new Error("Electron require unavailable.");
  }

  const electron = maybeWindow.require("electron") as {
    desktopCapturer?: {
      getSources: (options: { types: string[] }) => Promise<Array<{ id: string; name: string }>>;
    };
  };
  if (!electron.desktopCapturer) {
    throw new Error("Electron desktopCapturer unavailable.");
  }

  const sources = await electron.desktopCapturer.getSources({ types: ["screen"] });
  const source = sources[0];
  if (!source) {
    throw new Error("No desktop capture source available.");
  }

  return navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: "desktop",
        chromeMediaSourceId: source.id,
      },
    },
    video: {
      mandatory: {
        chromeMediaSource: "desktop",
        chromeMediaSourceId: source.id,
      },
    },
  } as unknown as MediaStreamConstraints);
}

function extractAudioOnlyStream(stream: MediaStream): MediaStream | null {
  for (const videoTrack of stream.getVideoTracks()) {
    videoTrack.stop();
  }

  const audioTracks = stream.getAudioTracks();
  if (audioTracks.length === 0) {
    stopMediaStream(stream);
    return null;
  }

  return new MediaStream(audioTracks);
}

function createMixedAudioStream(
  microphoneStream: MediaStream,
  computerAudioStream: MediaStream,
): { stream: MediaStream; audioContext: AudioContext; cleanup: () => Promise<void> } {
  const audioContext = new AudioContext();
  const destination = audioContext.createMediaStreamDestination();

  const microphoneSource = audioContext.createMediaStreamSource(microphoneStream);
  const microphoneGain = audioContext.createGain();
  microphoneGain.gain.value = 1.0;
  microphoneSource.connect(microphoneGain).connect(destination);

  const computerSource = audioContext.createMediaStreamSource(computerAudioStream);
  const computerGain = audioContext.createGain();
  computerGain.gain.value = 1.0;
  computerSource.connect(computerGain).connect(destination);

  return {
    stream: destination.stream,
    audioContext,
    cleanup: async () => {
      await audioContext.close();
    },
  };
}

function isDomExceptionName(error: unknown, name: string): boolean {
  return error instanceof DOMException && error.name === name;
}

function cleanupRecordingResources(stream: MediaStream, extraStreams: MediaStream[], audioContext: AudioContext | null): void {
  stopMediaStream(stream);
  for (const extraStream of extraStreams) {
    stopMediaStream(extraStream);
  }
  if (audioContext) {
    void audioContext.close();
  }
}

function createFileFromBlob(blob: Blob, name: string, mimeType: string): File {
  return new File([blob], name, { type: mimeType });
}

function previewAssistantReplacement(
  app: App,
  markdown: string,
  onAction: (action: "replace" | "insert" | "copy") => void,
): Promise<void> {
  return new Promise((resolve) => {
    const modal = new AssistantPreviewModal(app, markdown, (action) => {
      onAction(action);
      resolve();
    });
    const originalOnClose = modal.onClose.bind(modal);
    modal.onClose = () => {
      originalOnClose();
      resolve();
    };
    modal.open();
  });
}

async function copyToClipboard(text: string): Promise<void> {
  if (!text.trim()) {
    throw new UserFacingError("Aucune reponse a copier.");
  }
  await navigator.clipboard.writeText(text);
}
