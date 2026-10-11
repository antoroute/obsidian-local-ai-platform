export class App {}
export class Modal {}
export class Setting {}
export class Notice { constructor(_message: string) {} }
export const Platform = { isDesktop: true };
export class TFile { constructor(public path: string) {} }
export function normalizePath(path: string): string { return path.replace(/\\/g, "/"); }
export async function requestUrl(): Promise<never> { throw new Error("Unexpected network request in UI test"); }
