export class App {}
export class Editor {}
export class ItemView {}
export class MarkdownRenderer {}
export class MarkdownView {}
export class Menu {}
export class Modal {}
export class Plugin { constructor(..._args: unknown[]) {} }
export class PluginSettingTab {}
export class Setting {}
export class Notice { constructor(_message: string) {} }
export const Platform = { isDesktop: true };
export class TAbstractFile {}
export class TFile { constructor(public path: string) {} }
export class WorkspaceLeaf {}
export function normalizePath(path: string): string { return path.replace(/\\/g, "/"); }
export async function requestUrl(): Promise<never> { throw new Error("Unexpected network request in UI test"); }
