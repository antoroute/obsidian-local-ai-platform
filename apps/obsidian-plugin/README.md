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

Recommendations:

- Use `https://...` for shared or remote deployments
- `http://127.0.0.1:8000` is acceptable for local development only
- Place `compte-rendu-standard.md` in your configured templates folder if you want to override the built-in fallback template

## Command

Use the command palette entry:

- `AI Meeting Assistant: Summarize current note`

The plugin will:

1. read the active note
2. load the default template
3. call `POST /v1/notes/summarize`
4. create or overwrite a note in the configured output folder

Default output folder:

- `AI Summaries`

Output filename format:

- `YYYY-MM-DD - AI Summary - <note title>.md`

## Local API test

1. start the gateway locally
2. create a token with the `notes:summarize` scope
3. set `API Base URL` to your gateway base URL
4. run the summarize command on a non-empty note

Expected result:

- a summary note appears in your configured output folder

## Frequent errors

- Missing API URL: configure `API Base URL` in plugin settings
- Missing API token: configure `API Token` in plugin settings
- `401`: token invalid or expired
- `403`: token missing `notes:summarize` or selected model refused by the gateway
- `413`: note or template exceeds the gateway limits
- `422`: note is empty or payload rejected by the gateway
- `502` or `503`: gateway or Ollama backend unavailable
- Invalid JSON response: the gateway returned an unexpected payload
