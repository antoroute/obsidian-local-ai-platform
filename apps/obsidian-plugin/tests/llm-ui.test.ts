import assert from "node:assert/strict";
import test from "node:test";
import { TFile } from "obsidian";
import { LlmUiController } from "../src/llm-ui";

function fixture() {
  const files = new Map<string, { file: TFile; content: string }>();
  let creations = 0;
  let opened = "";
  const host = {
    app: {
      vault: {
        getAbstractFileByPath: (path: string) => files.get(path)?.file ?? null,
        create: async (path: string, content: string) => {
          if (files.has(path)) throw new Error("Already exists");
          const file = new TFile(path);
          files.set(path, { file, content });
          creations++;
          return file;
        },
      },
      workspace: { getLeaf: () => ({ openFile: async (file: TFile) => { opened = file.path; } }) },
    },
    getApiBaseUrl: () => "https://gateway.example.test",
    getApiToken: () => "gateway-test-token",
    getPersonalModel: () => "local-model",
    setPersonalModel: async (_model: string) => {},
    getOutputFolder: () => "Note Compagnon/Comptes rendus",
  };
  const controller = new LlmUiController(host as never);
  return { controller, files, creations: () => creations, opened: () => opened };
}

test("each plugin session starts with personal mode, with no enterprise model selected", () => {
  const { controller } = fixture();
  assert.equal(controller.isEnterprise, false);
  assert.equal(controller.enterpriseModel, "");
  assert.equal(controller.label, "Personnel · local-model");
});

test("recovered generation export preserves a note edited after its first delivery", async () => {
  const { controller, files, creations, opened } = fixture();
  const id = "aa3bded3-a6b5-4654-b3e0-b38c1e950822";
  const first = await controller.saveGeneratedNote(id, "---\ntitle: Réunion\n---\n\nDécision initiale.");
  const saved = files.get(first.path)!;
  assert.match(saved.content, /ai_provider: enterprise_relay/);
  assert.match(saved.content, new RegExp(`generation_id: "${id}"`));
  saved.content = "Correction humaine à conserver.";
  assert.equal(await controller.saveGeneratedNote(id, "Nouvelle livraison"), first);
  await controller.exportResult({ id, model_id: "company-model" } as never);
  assert.equal(saved.content, "Correction humaine à conserver.");
  assert.equal(creations(), 1);
  assert.equal(opened(), first.path);
});

test("concurrent deliveries produce one saved note", async () => {
  const { controller, creations } = fixture();
  const files = await Promise.all([
    controller.saveGeneratedNote("generation-1", "Première livraison"),
    controller.saveGeneratedNote("generation-1", "Livraison répétée"),
  ]);
  assert.equal(files[0], files[1]);
  assert.equal(creations(), 1);
});

test("server generation IDs cannot traverse outside the selected output folder", () => {
  const { controller } = fixture();
  for (const id of ["../../secrets", "/tmp/note", "x\\..\\note", "", "x".repeat(65)]) {
    assert.throws(() => controller.outputPath(id), /Identifiant/);
  }
});
