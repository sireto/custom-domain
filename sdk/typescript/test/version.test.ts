import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { USER_AGENT, VERSION } from "../src/index.js";
import { readJson, ROOT } from "./helpers.js";

test("VERSION, the user agent and package.json name the same release", () => {
  const pkg = readJson<{ version: string }>("package.json");
  assert.equal(VERSION, pkg.version);
  assert.equal(USER_AGENT, `custom-domain-sdk-ts/${pkg.version}`);
});

test("released with the service: package.json equals pyproject.toml", () => {
  const pyproject = readFileSync(new URL("../../pyproject.toml", ROOT), "utf8");
  const match = /^version\s*=\s*"([^"]+)"/m.exec(pyproject);
  assert.ok(match, "no version in pyproject.toml");
  assert.equal(readJson<{ version: string }>("package.json").version, match[1]);
});

test("no runtime dependencies", () => {
  const pkg = readJson<Record<string, unknown>>("package.json");
  assert.equal(pkg.dependencies, undefined);
  assert.equal(pkg.peerDependencies, undefined);
});
