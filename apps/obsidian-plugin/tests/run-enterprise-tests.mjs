import { build } from "esbuild";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

const testsDirectory = path.dirname(fileURLToPath(import.meta.url));
const output = await mkdtemp(path.join(tmpdir(), "note-compagnon-tests-"));
try {
  const tests = (await readdir(testsDirectory)).filter((name) => name.endsWith(".test.ts"));
  const bundles = [];
  for (const name of tests) {
    const bundle = path.join(output, name.replace(/\.ts$/, ".cjs"));
    await build({ entryPoints: [path.join(testsDirectory, name)], outfile: bundle, bundle: true, platform: "node", format: "cjs", alias: { obsidian: path.join(testsDirectory, "obsidian-stub.ts") }, logLevel: "silent" });
    bundles.push(bundle);
  }
  const result = spawnSync(process.execPath, ["--test", ...bundles], { stdio: "inherit" });
  process.exitCode = result.status ?? 1;
} finally { await rm(output, { recursive: true, force: true }); }
