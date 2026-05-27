# Note Compagnon

Note Compagnon is an Obsidian plugin for a private meeting workflow backed by the secured AI Gateway.

It can chat with the current note as context, correct or rewrite selected text, summarize notes, record meetings, upload audio for transcription, and create final Markdown minutes in your vault.

## Development Install

```powershell
cd apps/obsidian-plugin
npm install
npm run build
npm run check
```

Use the existing plugin ID folder so upgrades do not break an installed vault:

```text
<your-vault>/.obsidian/plugins/obsidian-local-ai-platform/
```

Place `manifest.json` and `main.js` there.

## Configuration

Configure these settings in Obsidian:

- `General`: API Base URL, API Token, default model, connection test
- `Assistant`: quick actions language
- `Reunions`: meeting, recording and output folders, transcription language, output language
- `Audio`: recording mode, microphone, computer audio input, refresh and test buttons
- `Connaissance du vault`: RAG enabled, vault ID, indexing mode, exclusions, local indexing state
- `Templates`: templates folder, preferred template language, recommended template installer
- `Avance`: technical information

Required API scopes:

- `models:list` for connection tests
- `assistant:chat` for chat, correction, rewriting, and selected-text summarization
- `notes:summarize` for note summaries
- `audio:transcribe` for audio upload jobs
- `meetings:generate` for meeting minutes from completed audio jobs
- `vault:index` for indexing local notes into the RAG backend
- `vault:search` for vault stats/search features
- `vault:ask` for asking questions to the indexed vault
- `vault:admin` for deleting the vault index

Recommended full token command from the repository root:

```powershell
.\scripts\prod\create-token-full.ps1 -Mode gpu -Name "note-compagnon-full"
```

The token is shown once only. If a token lacks a vault scope, Note Compagnon shows a `403` error such as missing `vault:index`, `vault:ask`, or `vault:admin`.

## Dashboard

Run:

```text
Note Compagnon: Open dashboard
```

The dashboard is an action page with compact sections:

- `Reunion`: start recording, stop and generate minutes, generate from an audio file, summarize the current note, and open the useful folders
- `Assistant`: chat with Note Compagnon, then insert or copy a real Markdown-rendered answer
- `Connaissance du vault`: explicit RAG indexing, stats, deletion, and vault question support
- `Templates`: install/open templates, with detailed template metadata hidden by default
- `Etat`: backend status and test button, with details hidden by default

The `Reunion` section keeps these daily actions visible: `Demarrer reunion`, `Arreter + CR`, `Depuis audio`, `Resumer note`, `Ouvrir reunions`, and `Ouvrir comptes rendus`.

## Assistant

The dashboard has an explicit `Mode de reponse` selector:

1. `Assistant simple`
   - calls `POST /v1/assistant/chat`
   - sends only the question typed in the textarea
   - sends no current note, no template, and no vault context
2. `Avec la note courante`
   - calls `POST /v1/assistant/chat`
   - sends only the active note as `context`
   - does not use RAG
3. `Avec le vault`
   - calls `POST /v1/vault/ask`
   - uses the RAG index and displays sources
   - displays safe RAG diagnostics when available: vector score, keyword bonus, matched terms, and selected paths
   - does not send the full current note

For chat, `Output language = same_as_meeting` maps to `same_as_input`, so a French question should receive a French answer and an English question should receive an English answer unless you explicitly force French or English.
Assistant answers are rendered with Obsidian Markdown in the dashboard, so headings, lists, paragraphs, and code blocks should remain readable. The `Inserer` and `Copier` buttons use the original Markdown text.

The dashboard does not expose selected-text actions because clicking outside the editor can clear the active selection.

## Connaissance Du Vault

The RAG workflow is explicit. Note Compagnon never searches the vault implicitly from the simple assistant mode.

Dashboard actions:

- `Indexer note`: indexes the active Markdown note
- `Indexer dossier`: indexes Markdown notes in the active note's folder
- `Indexer vault`: indexes all admissible Markdown notes in the vault
- `Forcer reindex`: queues all Markdown notes again
- `Reindex erreurs`: queues notes that failed during automatic or manual indexing
- `Tester la recherche RAG`: calls `/v1/vault/search` and shows candidate chunks before generation
- `Annuler`: cancels the current indexing queue
- `Statistiques`: calls `GET /v1/vault/stats`
- `Reinitialiser index`: asks for confirmation, then calls `DELETE /v1/vault/index`

During folder/vault indexing, the dashboard shows a progress panel with total notes, processed notes, indexed/skipped/ignored/error counts, the current note, remaining notes and elapsed time.

RAG enabled and indexing mode are separate settings:

- `Activer la connaissance du vault` controls whether the dashboard can ask questions with `Avec le vault`.
- `Mode d'indexation = Manuelle` never indexes automatically; use the dashboard buttons.
- `Mode d'indexation = Automatique` indexes created/modified Markdown notes after a debounce, can periodically scan changed notes, and can optionally scan on startup.
- Automatic indexing only runs while Obsidian is open. LiveSync can synchronize the vault, but Note Compagnon still indexes only local decrypted notes.
- `Workspace RAG` is a stable namespace for the vault index. Keep it identical when you regenerate tokens so the new token sees the same indexed notes.

Exclusions are applied before any note is sent to the gateway:

- non-Markdown files are ignored
- folders listed in `Dossiers exclus de l'index` are ignored
- notes with frontmatter `ai_index: false` are ignored
- notes with excluded tags are ignored
- notes larger than `Taille maximale d'une note a indexer` are ignored
- attachments and binary files are never indexed

Indexed payloads include the note path, title, Markdown content, frontmatter when it can be parsed simply, tags, and `modified_at`. If frontmatter is invalid, Note Compagnon keeps indexing with empty frontmatter rather than blocking the workflow.

Security model:

- the backend does not read CouchDB or LiveSync directly
- LiveSync E2EE remains respected because only the local Obsidian plugin sees decrypted notes
- only notes sent by manual actions or optional automatic plugin indexing are stored in the RAG index
- do not index private folders or notes; use excluded folders, excluded tags, or `ai_index: false`
- reinitializing the RAG index does not delete any Obsidian note
- `Reinitialiser mon index actuel` removes only the selected workspace
- `Reinitialiser tout l'index du vault` removes all indexed rows for that vault across historical token/user spaces and requires `vault:admin`

When using `Avec le vault`, Note Compagnon renders the answer as Markdown and shows sources. If a source path exists locally, clicking it opens the note. If no source is returned, the dashboard suggests indexing the vault or reformulating the question.

Use `Tester la recherche RAG` when an answer seems weak. It displays the retrieved paths, snippets, final score, vector score, keyword bonus and matched terms before the LLM writes an answer, which helps distinguish an indexing problem from a retrieval or generation problem. If no chunk appears, check that the vault was indexed with the same `Workspace RAG`, that the note is not excluded by folder/tag/frontmatter, and try exact keywords such as `CouchDB LiveSync HTTPS`.

If several generated tokens created separate RAG spaces, use a full/admin token, run `Reinitialiser tout l'index du vault`, then reindex once with `Workspace RAG = default`.

### Tester la synchronisation RAG automatique

1. Active `Activer la connaissance du vault`.
2. Mets `Mode d'indexation = Automatique`.
3. Cree `Inbox/Test RAG Update.md` avec `ALPHA-RAG-001`.
4. Attends la fin de l'indexation et verifie avec `Tester la recherche RAG` que `ALPHA-RAG-001` ressort.
5. Remplace le contenu par `BETA-RAG-002`.
6. Verifie que `BETA-RAG-002` ressort.
7. Verifie que `ALPHA-RAG-001` ne ressort plus.
8. Supprime la note et verifie que `BETA-RAG-002` ne ressort plus.
9. Cree une note contenant `RENAME-RAG-003`.
10. Renomme ou deplace la note.
11. Verifie que l'ancien chemin n'apparait plus et que le nouveau chemin ressort.

En mode `Manuelle`, ces hooks ne declenchent pas d'indexation automatique. Les suppressions et renommages automatiques nettoient l'index seulement quand le mode automatique est actif.

## Selected Text Actions

Command palette and editor context menu actions:

- `Note Compagnon: Correct selected text`
- `Note Compagnon: Rewrite selected text`
- `Note Compagnon: Reecrire plus professionnel`
- `Note Compagnon: Summarize selected text`

For `correct`, `rewrite`, and `summarize`, the plugin requests a direct response and shows a preview with:

- `Remplacer la selection`
- `Inserer sous la selection`
- `Copier`
- `Annuler`

Quick actions use `same_as_input` by default, so French selected text stays French and English selected text stays English. You can force French or English in `Quick actions language`.

`Reecrire plus professionnel` is the enterprise preset. It keeps the same language, preserves the meaning, improves tone and clarity, avoids unnecessary length, and returns only the final text. It does not turn a short sentence into a full email unless the selected text already looks like an email.

## Templates

Note Compagnon reads Markdown files from `Templates folder`. Templates can include frontmatter:

```markdown
---
name: Meeting minutes
language: en
type: meeting_summary
description: Full English meeting minutes.
---
```

The template picker supports language filtering and groups templates by type. When `Output language` is `fr` or `en`, the picker defaults to that language. When output language is `same_as_meeting`, `Preferred template language` controls the default filter.

The dashboard installer offers:

- minimal recommended
- French only
- English only
- all templates

Existing templates are skipped and never overwritten automatically.

Recommended meeting-summary templates are intentionally short. They guide the model toward a direct useful report with five core blocks: summary, decisions, actions, open points, and uncertainties. Empty sections should be removed by the model instead of filled with generic text such as "no information available".

For small local models, prefer the default `Compte rendu direct` templates before adding long custom structures. Tables are avoided by default because compact bullet/pipe lines are usually more reliable with local models.

Example templates are available in `apps/obsidian-plugin/examples/templates`, including:

- `meeting-note-fr.md`
- `compte-rendu-reunion-fr.md`
- `meeting-note-en.md`
- `meeting-minutes-en.md`
- `actions-only-fr.md`
- `actions-only-en.md`

## Audio For Teams / Video Calls

`Mode d'enregistrement` has four modes:

- `Micro seul`: uses the device selected in `Microphone`; recommended for standard use
- `Son ordinateur seul`: records only the device selected in `Son ordinateur`, usually `Mixage stereo`, `Stereo Mix`, `What U Hear`, `Loopback`, or `Monitor`
- `Micro + son ordinateur`: records `Microphone` and `Son ordinateur` in parallel, then mixes both streams in the plugin with the Web Audio API
- `Capture systeme experimentale`: tries direct system capture through Obsidian/Electron; not recommended by default and may not work

### Micro + son ordinateur sans logiciel externe

Note Compagnon can mix two normal Windows audio inputs without external software:

- the physical microphone provides your voice
- `Mixage stereo` / `Stereo Mix` provides the computer audio when the audio driver exposes it
- the plugin mixes both streams internally; you do not need to enable `Listen to this device` in Windows
- do not try to select a speaker/output directly; Obsidian can only record audio inputs exposed by Windows

1. Open `Windows Settings > System > Sound`.
2. Open `More sound settings`.
3. Go to the `Recording` tab.
4. Right-click and enable `Show Disabled Devices`.
5. Enable `Mixage stereo` / `Stereo Mix` if it exists.
6. Verify that the `Mixage stereo` level meter moves when a video or system sound plays.
7. Return to Note Compagnon.
8. Click `Actualiser les peripheriques`.
9. Set `Microphone` to your physical microphone.
10. Set `Son ordinateur` to `Mixage stereo` / `Stereo Mix`.
11. Click `Tester le micro`.
12. Click `Tester le son ordinateur` while playing sound on the computer.
13. Set `Mode d'enregistrement` to `Micro + son ordinateur`.

Some PCs expose `Mixage stereo` only for the built-in analog/Realtek output, and not for USB, Bluetooth, HDMI, or external sound cards. If `Mixage stereo` receives no sound, this usually comes from the audio driver or the selected Windows output.

Settings-only audio tools:

- `Actualiser les peripheriques`: asks microphone permission if needed, then lists available audio inputs
- `Tester le micro`: listens to the selected microphone for a few seconds, detects whether sound is present, then stops all tracks without saving audio
- `Tester le son ordinateur`: listens to the selected computer-audio input for a few seconds; play a sound during the test

`Actualiser les peripheriques` also looks for labels such as `Mixage stereo`, `Stereo Mix`, `What U Hear`, `Loopback`, `Monitor`, and `Mix`. If one is found, the plugin suggests selecting it as `Son ordinateur` and displays it with `(recommande)`. If none is found, it shows a short Windows-oriented hint.

### Capture systeme experimentale

Direct system capture depends on Obsidian/Electron. A screen/window picker can appear, but it may not provide any system-audio track. If no audio track is provided, Note Compagnon cancels instead of creating a silent recording. This is not a backend or transcription bug.

Optional advanced tools such as VoiceMeeter or VB-Cable can help create a Windows audio input, but they are not required by Note Compagnon.

Limits:

- if Teams audio is in headphones, a normal microphone may not capture other participants clearly
- direct global computer-audio capture is not promised unless Windows exposes an input device or Obsidian/Electron provides an audio track
- diarization / speaker separation is not implemented yet

Recorded audio remains in your vault under `Recordings folder`.
Tell participants before recording a meeting.

## Clean AI Summaries

Generated meeting notes are written as normal Obsidian Markdown, not as a global fenced code block. Note Compagnon removes a global Markdown fence wrapper if a model returns one by mistake.

Final meeting summaries use frontmatter similar to:

```yaml
---
type: meeting_summary
source_meeting: "[[source note]]"
source_audio: "[[recording.webm]]"
model: "mistral:latest"
template: "Compte rendu de reunion"
transcription_language: "auto"
output_language: "fr"
recording_source_used: "microphone_only"
job_id: "..."
created: "2026-05-24"
tags:
  - meeting
  - compte-rendu
---
```

The final note should contain the polished minutes only. It should not include the raw transcript, raw prompt instructions, `Language instruction`, `Manual notes`, or `Transcript` source blocks.

## Frequent Errors

- `401`: token absent, invalid, or expired
- `403` on assistant chat: token missing `assistant:chat`
- `403` on test connection: token missing `models:list`
- `403` on summarization: token missing `notes:summarize` or model refused
- `403` on audio upload: token missing `audio:transcribe`
- `403` on meeting generation: token missing `meetings:generate` or model refused
- `502` or `503`: model, Ollama, or gateway backend unavailable
- No selected text: select text in a note, then use command palette or right-click

## Manual Test Checklist

1. Chat from dashboard.
2. Correct selected text from command palette.
3. Correct selected text from right-click menu.
4. Rewrite selected text with a direct, non-verbose answer.
5. Summarize selected text with a direct Markdown summary.
6. Summarize current note from dashboard.
7. Pick templates with language filtering.
8. Verify the dashboard is compact, complete, and has no Teams/test section.
9. Verify the `Reunion` section contains `Demarrer reunion`, `Arreter + CR`, `Depuis audio`, `Resumer note`, `Ouvrir reunions`, and `Ouvrir comptes rendus`.
10. Verify the `Reunion` section does not contain `Ouvrir templates` or `Ouvrir enregistrements`.
11. Ask a simple chat question with `Assistant simple` and verify the active note does not influence the answer.
12. Ask with `Assistant simple` and verify `/v1/vault/ask` is not used.
13. Ask with `Avec la note courante` and verify only the active note is used.
14. Ask with `Avec le vault` and verify sources are displayed.
15. Index the current note.
16. Index the current folder.
17. Index the whole vault and verify exclusions are respected.
18. Open vault stats and verify documents/chunks are shown.
19. Delete the vault index and verify confirmation is required.
20. Verify a token without `vault:index` shows a clear indexing error.
21. Verify a token without `vault:ask` shows a clear vault question error.
22. Verify French chat answers French and English chat answers English when output language is not forced.
23. Verify an assistant answer with headings, lists, and paragraphs renders as readable Markdown.
24. Verify assistant `Inserer` / `Copier` appear only after a real answer.
25. Verify detailed templates, vault knowledge, and status blocks are collapsible.
26. Select `Microphone` and `Son ordinateur`, then run `Tester le micro` and `Tester le son ordinateur`.
27. Record with `Micro seul`.
28. Record with `Son ordinateur seul` using `Mixage stereo` / `Stereo Mix`.
29. Record with `Micro + son ordinateur`: speak into the microphone and play computer audio, then verify the saved file contains both.
30. Verify `Micro + son ordinateur` does not ask for screen/window selection.
31. Try `Capture systeme experimentale`: if Obsidian/Electron provides no audio track, it fails clearly without creating a silent recording.
32. Verify `recording_source_requested`, `recording_source_used`, `microphone_input_device_label`, and `computer_audio_input_device_label` are written in the meeting note.
33. Generate an AI Summary and verify there is no global Markdown code block, no raw transcript, and no internal prompt/source labels.

## Privacy Notes

- The plugin never logs or displays the API token.
- The audio is sent only to the configured `API Base URL`.
- Recorded audio remains in the vault.
- Tell participants before recording a meeting.
