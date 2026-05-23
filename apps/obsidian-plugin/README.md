# Obsidian Plugin MVP

This plugin adds Obsidian commands that send notes or audio-driven meeting requests to the AI Gateway and create Markdown output notes in your vault.

## Development install

```powershell
cd apps/obsidian-plugin
npm install
npm run build
```

Copy these files into your Obsidian vault plugin folder:

- `manifest.json`
- `main.js`
- `styles.css` is not required for this MVP

Suggested vault path:

```text
<your-vault>/.obsidian/plugins/obsidian-local-ai-platform/
```

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

Use `Test connection` after configuration:

- it calls `GET /v1/models`
- it confirms the configured token works
- it shows the models returned by the gateway
- it helps detect a `401`, missing `models:list`, or an unavailable API early

Recommendations:

- Use `https://...` for shared or remote deployments
- `http://127.0.0.1:8000` is acceptable for local development only
- Place `compte-rendu-standard.md` in your configured templates folder if you want to override the built-in fallback template

Required API scopes across the current MVP:

- `models:list` for `Test connection`
- `notes:summarize` for note summaries
- `audio:transcribe` for audio upload jobs
- `meetings:generate` for meeting generation from completed audio jobs

## Templates

Before generation, the plugin opens a simple template picker.

- it reads `.md` files from the configured `Templates folder`
- if templates are found, you choose which one to send to the gateway
- if no template file is available, the plugin uses the built-in fallback template
- the generated note records which template was used

## Command

Use the command palette entry:

- `AI Meeting Assistant: Summarize current note`
- `AI Meeting Assistant: Generate meeting minutes from audio file`
- `AI Meeting Assistant: Start meeting recording`
- `AI Meeting Assistant: Stop recording and generate meeting minutes`

The plugin will:

1. read the active note
2. let you choose a template when template files are available
3. call `POST /v1/notes/summarize`
4. create or overwrite a note in the configured output folder

The audio workflow will:

1. prompt you to pick a local audio file
2. accept `.wav`, `.mp3`, `.m4a`, `.webm`, or `.ogg`
3. upload the file with `POST /v1/audio/transcribe`
4. poll `GET /v1/jobs/{job_id}` until the job completes or fails
5. prompt for a meeting title and optional manual notes
6. let you choose a Markdown template
7. call `POST /v1/meetings/generate-from-job`
8. create or overwrite a meeting minutes note in the configured output folder

The recording workflow will:

1. ask for a meeting title
2. request microphone permission through Obsidian
3. create a meeting note in `Meetings folder`
4. open that note so you can write manual notes during the meeting
5. record microphone audio only and keep it local in memory until stop
6. save the recording into `Recordings folder` inside the vault when you stop
7. update the meeting note with `Recording status: completed` and an audio link
8. reuse the full meeting note as `manual_notes`
9. upload the saved audio to `POST /v1/audio/transcribe`
10. poll the job until completion
11. call `POST /v1/meetings/generate-from-job`
12. create a final meeting minutes note in the configured output folder

Default output folder:

- `AI Summaries`

Default meetings folder:

- `Meetings`

Default recordings folder:

- `AI Recordings`

Output filename format:

- `YYYY-MM-DD - AI Summary - <note title>.md`
- `YYYY-MM-DD - AI Meeting Minutes - <meeting title>.md`
- `YYYY-MM-DD HH-mm - <meeting title>.md` for the in-progress meeting note
- `YYYY-MM-DD HH-mm - <meeting title>.webm` for the recorded microphone audio
- `YYYY-MM-DD HH-mm - Meeting Minutes - <meeting title>.md` for the final generated meeting minutes

## Local API test

1. start the gateway locally
2. create a token with `notes:summarize`
3. optionally create or reuse a token with `models:list` for `Test connection`
4. set `API Base URL` to your gateway base URL
5. run `Test connection`
6. run the summarize command on a non-empty note
7. for audio, use a token that also has `audio:transcribe` and `meetings:generate`
8. run the audio command and pick a supported file
9. for the recording workflow, start recording, write notes in the opened meeting note, then stop recording and generate minutes

Expected result:

- a summary note appears in your configured output folder
- an audio job is created, then a meeting minutes note appears after the job completes
- a live meeting note and a saved microphone recording remain in your vault for the recording workflow

## Recording notes

- the MVP captures microphone audio only, not system audio
- recorded audio stays in your Obsidian vault under `Recordings folder`
- the plugin does not send audio anywhere except the configured `API Base URL`
- the final meeting minutes note does not embed the full raw transcript
- tell participants the meeting is being recorded before you start

## Frequent errors

- Missing API URL: configure `API Base URL` in plugin settings
- Missing API token: configure `API Token` in plugin settings
- Missing default model: configure `Default model`
- Missing meetings folder: configure `Meetings folder`
- Missing recordings folder: configure `Recordings folder`
- Missing output folder: configure `Output folder`
- MediaRecorder not supported: this Obsidian environment cannot record audio
- Microphone permission denied: allow microphone access and retry
- No meeting recording active: start a recording before stopping it
- Empty recording: the microphone capture produced no audio data
- Failed to save the recorded audio: check vault write access and the configured recordings folder
- `401`: token invalid or expired
- `403` on `Test connection`: token missing `models:list`
- `403` on summarization: token missing `notes:summarize` or selected model refused by the gateway
- `403` on audio upload: token missing `audio:transcribe`
- `403` on meeting generation from audio: token missing `meetings:generate` or selected model refused by the gateway
- `413`: note or template exceeds the gateway limits
- `413` on audio upload: file too large for the gateway limit
- `422`: note is empty or payload rejected by the gateway
- Unsupported audio extension: use `.wav`, `.mp3`, `.m4a`, `.webm`, or `.ogg`
- Audio job not found: the job expired, failed, or does not belong to the current token
- `409` during audio workflow: the job is not ready yet, failed, or the stored transcript is unusable
- Audio transcription timed out: the plugin stopped polling after the configured timeout window
- `502` or `503`: gateway or Ollama backend unavailable
- Invalid JSON response: the gateway returned an unexpected payload
- No template files found: the plugin falls back to the built-in template
