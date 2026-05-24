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

- `API Base URL`
- `API Token`
- `Default model`
- `Templates folder`
- `Meetings folder`
- `Recordings folder`
- `Output folder`
- `Transcription language`
- `Output language`
- `Preferred template language`
- `Mode d'enregistrement`
- `Microphone`
- `Son ordinateur`

Required API scopes:

- `models:list` for connection tests
- `assistant:chat` for chat, correction, rewriting, and selected-text summarization
- `notes:summarize` for note summaries
- `audio:transcribe` for audio upload jobs
- `meetings:generate` for meeting minutes from completed audio jobs

## Dashboard

Run:

```text
Note Compagnon: Open dashboard
```

The dashboard is an action page with compact sections:

- `Reunion`: start recording, stop and generate minutes, generate from an audio file, summarize the current note, and open the useful folders
- `Assistant`: chat with Note Compagnon, then insert or copy a real Markdown-rendered answer
- `Templates`: install/open templates, with detailed template metadata hidden by default
- `Etat`: backend status and test button, with details hidden by default

The `Reunion` section keeps these daily actions visible: `Demarrer reunion`, `Arreter + CR`, `Depuis audio`, `Resumer note`, `Ouvrir reunions`, and `Ouvrir comptes rendus`.

## Assistant

The dashboard chat calls `POST /v1/assistant/chat`. By default it sends only the question typed in the textarea: no current note, no template, and no vault content is sent automatically.

Enable `Utiliser la note courante comme contexte` only when you explicitly want the active note to be used as reference context. If no active note exists, the plugin sends the question without context and shows a short notice.

For chat, `Output language = same_as_meeting` maps to `same_as_input`, so a French question should receive a French answer and an English question should receive an English answer unless you explicitly force French or English.
Assistant answers are rendered with Obsidian Markdown in the dashboard, so headings, lists, paragraphs, and code blocks should remain readable. The `Inserer` and `Copier` buttons use the original Markdown text.

The dashboard does not expose selected-text actions because clicking outside the editor can clear the active selection.

## Selected Text Actions

Command palette and editor context menu actions:

- `Note Compagnon: Correct selected text`
- `Note Compagnon: Rewrite selected text`
- `Note Compagnon: Summarize selected text`

For `correct`, `rewrite`, and `summarize`, the plugin requests a direct response and shows a preview with:

- `Remplacer la selection`
- `Inserer sous la selection`
- `Copier`
- `Annuler`

Quick actions use `same_as_input` by default, so French selected text stays French and English selected text stays English. You can force French or English in `Quick actions language`.

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
11. Ask a simple chat question with context unchecked and verify the active note does not influence the answer.
12. Ask a simple chat question with context checked and verify the active note can be used.
13. Verify French chat answers French and English chat answers English when output language is not forced.
14. Verify an assistant answer with headings, lists, and paragraphs renders as readable Markdown.
15. Verify assistant `Inserer` / `Copier` appear only after a real answer.
16. Verify detailed templates and status blocks are collapsible.
17. Select `Microphone` and `Son ordinateur`, then run `Tester le micro` and `Tester le son ordinateur`.
18. Record with `Micro seul`.
19. Record with `Son ordinateur seul` using `Mixage stereo` / `Stereo Mix`.
20. Record with `Micro + son ordinateur`: speak into the microphone and play computer audio, then verify the saved file contains both.
21. Verify `Micro + son ordinateur` does not ask for screen/window selection.
22. Try `Capture systeme experimentale`: if Obsidian/Electron provides no audio track, it fails clearly without creating a silent recording.
23. Verify `recording_source_requested`, `recording_source_used`, `microphone_input_device_label`, and `computer_audio_input_device_label` are written in the meeting note.
24. Generate an AI Summary and verify there is no global Markdown code block, no raw transcript, and no internal prompt/source labels.

## Privacy Notes

- The plugin never logs or displays the API token.
- The audio is sent only to the configured `API Base URL`.
- Recorded audio remains in the vault.
- Tell participants before recording a meeting.
