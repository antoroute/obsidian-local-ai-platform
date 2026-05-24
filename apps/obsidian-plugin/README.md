# Notre Compagnon

Notre Compagnon is an Obsidian plugin for a private meeting workflow backed by the secured AI Gateway.

It can summarize the current note, upload an audio file for transcription, record microphone audio inside Obsidian, and create final Markdown minutes in your vault.

## Development install

```powershell
cd apps/obsidian-plugin
npm install
npm run build
```

Use the existing plugin ID folder so upgrades do not break an installed vault:

```text
<your-vault>/.obsidian/plugins/obsidian-local-ai-platform/
```

Place these files there:

- `manifest.json`
- `main.js`

## Build and typecheck

```powershell
npm run build
npm run check
```

## Configuration

Open Obsidian settings, then configure:

- `API Base URL`
- `API Token`
- `Default model`
- `Templates folder`
- `Meetings folder`
- `Recordings folder`
- `Output folder`
- `Transcription language`
- `Output language`

Default folders:

- `Meetings folder`: `Compagnon/Reunions`
- `Recordings folder`: `Compagnon/Enregistrements`
- `Output folder`: `Compagnon/Comptes rendus`
- `Templates folder`: `Compagnon/Templates`

Required API scopes:

- `models:list` for `Test connection`
- `notes:summarize` for note summaries
- `audio:transcribe` for audio upload jobs
- `meetings:generate` for meeting minutes from completed audio jobs

## Dashboard

Open the command palette and run:

```text
Notre Compagnon: Open dashboard
```

The dashboard shows the API configuration status, API Base URL, default model, output language, and quick actions:

- test connection
- start meeting recording
- stop recording and generate minutes
- summarize current note
- generate minutes from audio file
- open configured folders

The dashboard is usable even when the backend is offline. Backend-dependent buttons will show short actionable notices if the API cannot be reached.

## Templates

Notre Compagnon reads `.md` files from `Templates folder` and opens a picker before generation. If the folder is empty or missing, the built-in fallback template is used.

Templates can include simple YAML frontmatter:

```markdown
---
name: Standard meeting minutes
language: en
type: meeting
description: Full English meeting minutes.
---
# Meeting minutes
```

The picker displays `name` when present, plus language, type, description, and file path.

Example templates are available in `apps/obsidian-plugin/examples/templates`:

- `compte-rendu-standard-fr.md`
- `standard-meeting-minutes-en.md`
- `actions-only-fr.md`
- `actions-only-en.md`
- `reunion-technique-fr.md`
- `technical-meeting-en.md`

## French / English

`Output language` controls an instruction appended to the template sent to the gateway:

- `same_as_meeting`: detect the main meeting language and answer in that language
- `fr`: answer in French
- `en`: answer in English

If the meeting is bilingual, the instruction asks the model to preserve proper nouns, product names, and acronyms without abusive translation.

`Transcription language` is added as a hint in the generation instructions. The current public API keeps actual worker transcription configuration server-side.

## Commands

Command palette entries:

- `Notre Compagnon: Open dashboard`
- `Notre Compagnon: Start meeting recording`
- `Notre Compagnon: Stop recording and generate minutes`
- `Notre Compagnon: Summarize current note`
- `Notre Compagnon: Generate minutes from audio file`

## Recording workflow

1. Run `Notre Compagnon: Start meeting recording`.
2. Enter a meeting title.
3. Obsidian asks for microphone permission.
4. Notre Compagnon creates a meeting note in `Compagnon/Reunions`.
5. Write manual notes in that note during the meeting.
6. Run `Notre Compagnon: Stop recording and generate minutes`.
7. The audio is saved in `Compagnon/Enregistrements`.
8. The meeting note is marked completed and linked to the audio.
9. The audio is uploaded to `POST /v1/audio/transcribe`.
10. The plugin polls `GET /v1/jobs/{job_id}` until transcription completes.
11. The plugin calls `POST /v1/meetings/generate-from-job`.
12. The final minutes note is created in `Compagnon/Comptes rendus`.

The MVP records microphone audio only, not system audio. The raw transcript is not embedded in the final minutes note.

## Audio file workflow

Run:

```text
Notre Compagnon: Generate minutes from audio file
```

Supported extensions:

- `.wav`
- `.mp3`
- `.m4a`
- `.webm`
- `.ogg`

The plugin uploads only the selected file to the configured `API Base URL`.

## Note summary workflow

Run:

```text
Notre Compagnon: Summarize current note
```

The active note is sent to `POST /v1/notes/summarize`, with the chosen template and language instruction, then a summary note is created in the output folder.

## Frequent errors

- Missing API URL: configure `API Base URL`.
- Missing API token: configure `API Token`.
- Missing default model: configure `Default model`.
- Missing output folder: configure `Output folder`.
- `401`: token invalid or expired.
- `403` on test connection: token missing `models:list`.
- `403` on summarization: token missing `notes:summarize` or selected model refused by the gateway.
- `403` on audio upload: token missing `audio:transcribe`.
- `403` on meeting generation: token missing `meetings:generate` or selected model refused by the gateway.
- Unsupported audio extension: use `.wav`, `.mp3`, `.m4a`, `.webm`, or `.ogg`.
- Microphone permission denied: allow microphone access and retry.
- MediaRecorder not supported: this Obsidian environment cannot record audio.
- No recording active: start a recording before stopping it.
- Audio transcription timed out: the plugin stopped polling after the configured timeout.
- `502` or `503`: the gateway, Ollama, or transcription backend is unavailable.
- Invalid JSON response: the gateway returned an unexpected payload.

## Privacy notes

- The plugin never logs or displays the API token.
- The audio is sent only to the configured `API Base URL`.
- Recorded audio remains in the vault under `Recordings folder`.
- Tell participants before recording a meeting.
