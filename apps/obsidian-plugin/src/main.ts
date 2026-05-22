import { Notice, Plugin } from "obsidian";

export default class LocalAiPlatformPlugin extends Plugin {
  async onload(): Promise<void> {
    this.addCommand({
      id: "local-ai-platform-health-check",
      name: "Show local AI platform bootstrap message",
      callback: () => {
        new Notice("Local AI Platform plugin is loaded.");
      },
    });
  }
}
