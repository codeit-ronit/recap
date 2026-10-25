#!/usr/bin/env node
/**
 * `npm test`: every dashboard test, with no test packages.
 *
 *   tests/*.test.ts   pure helpers. Node runs them as they are (type stripping, Node 22.18+).
 *   tests/*.test.tsx  React components. Node cannot run TSX, so each file is bundled first with
 *                     the esbuild that ships with vite (node_modules/esbuild), into
 *                     node_modules/.cache/recap-dashboard-tests/. React's development build is
 *                     bundled, so its warnings fire, and tests/support/setup.ts is injected
 *                     into every bundle: a console.error or console.warn during a test (React
 *                     warnings included) fails that test.
 *
 * Both sets run in ONE `node --test` call: one report, one exit code.
 *
 *   node scripts/test.mjs                          everything
 *   node scripts/test.mjs tests/modal.test.tsx     only the files named
 */

import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, rmSync } from "node:fs";
import { basename, dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const testsDir = join(root, "tests");
const outDir = join(root, "node_modules", ".cache", "recap-dashboard-tests");
const setup = join(testsDir, "support", "setup.ts");

const requested = process.argv.slice(2).map((file) => resolve(root, file));
const files = requested.length
  ? requested
  : readdirSync(testsDir)
      .filter((name) => /\.test\.tsx?$/.test(name))
      .sort()
      .map((name) => join(testsDir, name));

for (const file of files) {
  if (!existsSync(file) || !/\.test\.tsx?$/.test(file)) {
    console.error(`scripts/test.mjs: not a test file: ${relative(root, file)}`);
    process.exit(2);
  }
}

const tsTests = files.filter((file) => file.endsWith(".ts"));
const tsxTests = files.filter((file) => file.endsWith(".tsx"));
let bundles = [];

if (tsxTests.length) {
  let esbuild;
  try {
    esbuild = await import("esbuild");
  } catch {
    console.error("scripts/test.mjs: esbuild is missing. Run `npm install` (vite brings it).");
    process.exit(1);
  }
  rmSync(outDir, { recursive: true, force: true });
  mkdirSync(outDir, { recursive: true });
  try {
    await esbuild.build({
      absWorkingDir: root,
      entryPoints: tsxTests,
      outdir: outDir,
      entryNames: "[name]",
      outExtension: { ".js": ".mjs" },
      bundle: true,
      splitting: true,
      chunkNames: "chunks/[name]-[hash]",
      format: "esm",
      platform: "node",
      target: "node22",
      jsx: "automatic",
      sourcemap: "inline",
      inject: [setup],
      // Bundled CommonJS (React) calls require() for Node built-ins; give ESM a real one.
      banner: {
        js: 'import { createRequire as __recapCreateRequire } from "node:module"; const require = __recapCreateRequire(import.meta.url);',
      },
      define: {
        // What Vite would provide (src/api.ts reads it); the API base stays "" in tests.
        "import.meta.env": JSON.stringify({ DEV: false, PROD: true, MODE: "test", BASE_URL: "/", SSR: true }),
        // React's development build: its warnings are the point of the console guard.
        "process.env.NODE_ENV": JSON.stringify("development"),
      },
      loader: { ".css": "empty" },
      logLevel: "warning",
    });
  } catch {
    // esbuild has printed the errors (file, line, column).
    process.exit(1);
  }
  bundles = tsxTests.map((file) => join(outDir, `${basename(file, ".tsx")}.mjs`));
}

const result = spawnSync(process.execPath, ["--enable-source-maps", "--test", ...tsTests, ...bundles], {
  cwd: root,
  stdio: "inherit",
});
process.exit(result.status ?? 1);
