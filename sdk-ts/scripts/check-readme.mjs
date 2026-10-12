// Type-check every ```ts example in README.md against the sources.
//
// Each block becomes its own module under build/readme/, importing
// "@sireto/custom-domain" from src/. The application functions the examples
// call (loadWorkspace, storeSecret) are declared once in a shared prelude.
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const root = new URL("../", import.meta.url);
const out = new URL("build/readme/", root);
const readme = readFileSync(new URL("README.md", root), "utf8");
const blocks = [...readme.matchAll(/^```ts\n([\s\S]*?)^```$/gm)].map((m) => m[1]);
if (blocks.length === 0) {
  console.error("README.md has no ```ts examples");
  process.exit(1);
}

rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
writeFileSync(
  new URL("prelude.d.ts", out),
  [
    "// What the README's examples assume the application provides.",
    "declare function loadWorkspace(reference: string): Promise<{ name: string } | null>;",
    "declare function storeSecret(secret: string): Promise<void>;",
    "",
  ].join("\n"),
);
blocks.forEach((code, index) => {
  writeFileSync(new URL(`example-${String(index + 1).padStart(2, "0")}.ts`, out), code);
});
writeFileSync(
  new URL("tsconfig.json", out),
  JSON.stringify(
    {
      extends: "../../tsconfig.json",
      compilerOptions: {
        noEmit: true,
        rootDir: "../..",
        // The parent config's outDir (build/) would exclude these files.
        outDir: "unused",
        paths: { "@sireto/custom-domain": ["../../src/index.ts"] },
        allowImportingTsExtensions: true,
      },
      include: ["*.ts"],
    },
    null,
    2,
  ),
);

const tsc = createRequire(import.meta.url).resolve("typescript/bin/tsc");
try {
  execFileSync(process.execPath, [tsc, "-p", fileURLToPath(new URL("tsconfig.json", out))], {
    stdio: "inherit",
  });
} catch {
  console.error(`README.md examples do not type-check (files in ${fileURLToPath(out)})`);
  process.exit(1);
}
console.log(`README.md: ${blocks.length} examples type-check`);
