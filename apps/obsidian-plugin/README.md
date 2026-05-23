# Obsidian Plugin MVP

This plugin adds a minimal Obsidian command that sends the active note to the AI Gateway and creates a Markdown summary note in your vault.

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

## Templates

Before generation, the plugin opens a simple template picker.

- it reads `.md` files from the configured `Templates folder`
- if templates are found, you choose which one to send to the gateway
- if no template file is available, the plugin uses the built-in fallback template
- the generated note records which template was used

## Command

Use the command palette entry:

- `AI Meeting Assistant: Summarize current note`

The plugin will:

1. read the active note
2. let you choose a template when template files are available
3. call `POST /v1/notes/summarize`
4. create or overwrite a note in the configured output folder

Default output folder:

- `AI Summaries`

Output filename format:

- `YYYY-MM-DD - AI Summary - <note title>.md`

## Local API test

1. start the gateway locally
2. create a token with `notes:summarize`
3. optionally create or reuse a token with `models:list` for `Test connection`
4. set `API Base URL` to your gateway base URL
5. run `Test connection`
6. run the summarize command on a non-empty note

Expected result:

- a summary note appears in your configured output folder

## Frequent errors

- Missing API URL: configure `API Base URL` in plugin settings
- Missing API token: configure `API Token` in plugin settings
- Missing default model: configure `Default model`
- Missing output folder: configure `Output folder`
- `401`: token invalid or expired
- `403` on `Test connection`: token missing `models:list`
- `403` on summarization: token missing `notes:summarize` or selected model refused by the gateway
- `413`: note or template exceeds the gateway limits
- `422`: note is empty or payload rejected by the gateway
- `502` or `503`: gateway or Ollama backend unavailable
- Invalid JSON response: the gateway returned an unexpected payload
- No template files found: the plugin falls back to the built-in template
