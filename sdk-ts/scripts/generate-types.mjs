// Generates src/generated/openapi.ts from the committed API contract
// (../docs/openapi.json). Run `npm run generate` after the contract changes;
// `npm run check:generated` (also part of `npm test`) fails when the committed
// file differs from what the contract produces.
//
// Only the v1 schemas the SDK uses are emitted. Request bodies keep the
// contract's optional fields; responses list every property as present, since
// the API always serializes every field of a response model.
import { readFileSync, writeFileSync } from "node:fs";

const CONTRACT = new URL("../../docs/openapi.json", import.meta.url);
const OUTPUT = new URL("../src/generated/openapi.ts", import.meta.url);

/** Schema name -> "input" (request body) or "output" (response). */
const SCHEMAS = {
  DomainStatus: "output",
  CheckType: "output",
  CheckStatus: "output",
  DnsRecord: "output",
  CheckResult: "output",
  DomainResource: "output",
  DomainPage: "output",
  DomainCreate: "input",
  ErrorBody: "output",
  ErrorResponse: "output",
  WebhookCreate: "input",
  WebhookResource: "output",
  WebhookCreated: "output",
  DeliveryResource: "output",
  ReplayResult: "output",
  WebhookEventData: "output",
  WebhookEvent: "output",
};

function refName(ref) {
  const prefix = "#/components/schemas/";
  if (!ref.startsWith(prefix)) throw new Error(`unsupported $ref ${ref}`);
  const name = ref.slice(prefix.length);
  if (!(name in SCHEMAS)) throw new Error(`schema ${name} is referenced but not generated`);
  return name;
}

function literal(value) {
  return JSON.stringify(value);
}

function typeOf(schema) {
  if (schema.$ref) return refName(schema.$ref);
  if (schema.anyOf) {
    const parts = schema.anyOf.map(typeOf);
    return [...new Set(parts)].join(" | ");
  }
  if (schema.enum) return schema.enum.map(literal).join(" | ");
  switch (schema.type) {
    case "string":
      return "string";
    case "integer":
    case "number":
      return "number";
    case "boolean":
      return "boolean";
    case "null":
      return "null";
    case "array": {
      const item = typeOf(schema.items ?? {});
      return item.includes(" ") ? `Array<${item}>` : `${item}[]`;
    }
    case "object": {
      if (schema.properties) throw new Error("inline object schemas are not supported");
      const extra = schema.additionalProperties;
      const value = extra && extra !== true ? typeOf(extra) : "unknown";
      return `Record<string, ${value}>`;
    }
    case undefined:
      return "unknown";
    default:
      throw new Error(`unsupported type ${schema.type}`);
  }
}

function comment(text, indent) {
  const lines = text.trim().split("\n");
  if (lines.length === 1) return `${indent}/** ${lines[0]} */\n`;
  return `${indent}/**\n${lines.map((l) => `${indent} * ${l}`.trimEnd()).join("\n")}\n${indent} */\n`;
}

function render(contract) {
  const schemas = contract.components.schemas;
  let out =
    "// Generated from docs/openapi.json by scripts/generate-types.mjs. Do not edit;\n" +
    "// run `npm run generate` instead. Field names are the API's (snake_case).\n";
  for (const [name, mode] of Object.entries(SCHEMAS)) {
    const schema = schemas[name];
    if (!schema) throw new Error(`docs/openapi.json has no schema ${name}`);
    out += "\n";
    if (schema.description) out += comment(schema.description, "");
    if (schema.enum) {
      out += `export type ${name} = ${schema.enum.map(literal).join(" | ")};\n`;
      continue;
    }
    if (schema.type !== "object" || !schema.properties) {
      throw new Error(`schema ${name} is not an object or enum`);
    }
    const required = new Set(schema.required ?? []);
    out += `export interface ${name} {\n`;
    for (const [prop, propSchema] of Object.entries(schema.properties)) {
      if (propSchema.description) out += comment(propSchema.description, "  ");
      const optional = mode === "input" && !required.has(prop) ? "?" : "";
      out += `  ${prop}${optional}: ${typeOf(propSchema)};\n`;
    }
    out += "}\n";
  }
  return out;
}

function main() {
  const contract = JSON.parse(readFileSync(CONTRACT, "utf8"));
  const generated = render(contract);
  if (process.argv.includes("--check")) {
    let committed = "";
    try {
      committed = readFileSync(OUTPUT, "utf8");
    } catch {
      // missing file: reported below
    }
    if (committed !== generated) {
      console.error(
        "src/generated/openapi.ts does not match docs/openapi.json; run `npm run generate` in sdk-ts/",
      );
      process.exit(1);
    }
    return;
  }
  writeFileSync(OUTPUT, generated);
}

main();
