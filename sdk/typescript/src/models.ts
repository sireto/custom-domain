// API resources. The shapes are generated from docs/openapi.json
// (src/generated/openapi.ts); the names here match the Python SDK's models.
// Timestamps are ISO 8601 strings, exactly as the API sends them.

import type {
  CheckResult,
  DeliveryResource,
  DnsRecord,
  DomainCreate,
  DomainPage,
  DomainResource,
  WebhookCreate,
  WebhookCreated,
  WebhookEvent,
  WebhookResource,
} from "./generated/openapi.js";

export type {
  CheckStatus,
  CheckType,
  DnsRecord,
  DomainStatus,
  ErrorBody,
  ErrorResponse,
  WebhookEvent,
} from "./generated/openapi.js";

/** A registered hostname with its DNS records and checks. */
export type Domain = DomainResource;
/** One of the four checks (`ownership`, `routing`, `certificate`, `origin`). */
export type Check = CheckResult;
/** One page of {@link Domain}s; `next_offset` is `null` on the last page. */
export type Page = DomainPage;
/** A webhook subscription, without its secret. */
export type Webhook = WebhookResource;
/** A webhook subscription with its signing secret, shown once. */
export type WebhookWithSecret = WebhookCreated;
/** One delivery of an event to a subscription. */
export type Delivery = DeliveryResource;
export type DomainCreateInput = DomainCreate;
export type WebhookCreateInput = WebhookCreate;
export type WebhookEventType = WebhookEvent["type"];
export type DeliveryState = DeliveryResource["state"];

/** Whether all four checks pass and the domain is served. */
export function isReady(domain: Domain): boolean {
  return domain.status === "ready";
}

/** The check of the given type, if the domain has it. */
export function findCheck(domain: Domain, type: Check["type"]): Check | undefined {
  return domain.checks.find((check) => check.type === type);
}

/** One line suitable for a customer-facing DNS instruction. */
export function renderDnsRecord(record: DnsRecord): string {
  return `${record.type.padEnd(5)} ${record.name}  ->  ${record.value}`;
}

/** Text to show a customer: the records to create and why. */
export function renderDnsInstructions(domain: Domain): string {
  const lines = [`Create these DNS records for ${domain.hostname}:`, ""];
  for (const record of domain.dns_records) {
    lines.push(renderDnsRecord(record));
    lines.push(`      purpose: ${record.purpose}. ${record.help}`);
  }
  return lines.join("\n");
}
