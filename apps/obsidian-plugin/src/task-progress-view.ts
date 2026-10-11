/** Small shared renderer for milestone progress; percentages are never a time estimate. */
export function renderTaskProgress(
  parent: HTMLElement,
  progress: { percent: number; phase: string; detail?: string },
): void {
  const percent = Math.round(Math.max(0, Math.min(100, Number.isFinite(progress.percent) ? progress.percent : 0)));
  parent.createEl("div", { text: `${percent} % · ${progress.phase}` });
  const bar = parent.createEl("progress");
  bar.max = 100;
  bar.value = percent;
  bar.setAttribute("aria-label", progress.phase);
  bar.setAttribute("aria-valuetext", `${percent} %, ${progress.phase}`);
  bar.style.width = "100%";
  if (progress.detail) parent.createEl("small", { text: progress.detail });
}
