// Generated from docs/openapi.json by scripts/generate-types.mjs. Do not edit;
// run `npm run generate` instead. Field names are the API's (snake_case).

export type DomainStatus = "pending_dns" | "provisioning" | "ready" | "attention_required" | "suspended" | "deleting";

export type CheckType = "ownership" | "routing" | "certificate" | "origin";

export type CheckStatus = "pending" | "passing" | "failing";

export interface DnsRecord {
  /** Guidance for the customer's DNS console. */
  help: string;
  /** Fully qualified record name to create. */
  name: string;
  /** `ownership` proves control of the hostname; `routing` sends traffic to the edge. */
  purpose: "ownership" | "routing";
  type: "TXT" | "CNAME";
  value: string;
}

export interface CheckResult {
  /** Stable machine-readable failure code when `status` is `failing`. */
  error_code: string | null;
  message: string | null;
  next_check_at: string | null;
  observed_at: string | null;
  status: CheckStatus;
  type: CheckType;
}

export interface DomainResource {
  checks: CheckResult[];
  created_at: string;
  deleted_at: string | null;
  /** Records the customer must publish. Empty once the domain is deleted. */
  dns_records: DnsRecord[];
  /** Canonical hostname (lowercase, punycode). */
  hostname: string;
  id: string;
  metadata: Record<string, unknown> | null;
  reference: string;
  status: DomainStatus;
  updated_at: string;
}

export interface DomainPage {
  items: DomainResource[];
  limit: number;
  /** Pass as `offset` to fetch the next page; null on the last page. */
  next_offset: number | null;
  offset: number;
}

/** Register a customer hostname for a workspace of the calling application. */
export interface DomainCreate {
  /** Exact customer subdomain. Normalized to lowercase punycode. */
  hostname: string;
  /** Optional caller metadata, at most 32 scalar values, stored and echoed back. */
  metadata?: Record<string, string | number | boolean | null> | null;
  /** Opaque workspace identifier owned by the application. Returned verbatim. */
  reference: string;
}

export interface ErrorBody {
  code: string;
  details: Record<string, unknown>;
  message: string;
}

export interface ErrorResponse {
  error: ErrorBody;
}

export interface WebhookCreate {
  /** Event types to deliver. */
  events: Array<"domain.ready" | "domain.attention_required" | "domain.recovered" | "domain.deleted">;
  /** Absolute https URL that receives POST deliveries. Must resolve to a public address. */
  url: string;
}

export interface WebhookResource {
  active: boolean;
  created_at: string;
  events: string[];
  id: string;
  /** Until when the previous secret still signs, after a rotation. */
  previous_secret_expires_at: string | null;
  revoked_at: string | null;
  url: string;
}

export interface WebhookCreated {
  active: boolean;
  created_at: string;
  events: string[];
  id: string;
  /** Until when the previous secret still signs, after a rotation. */
  previous_secret_expires_at: string | null;
  revoked_at: string | null;
  /** Signing secret, shown once. Distinct from API credentials. */
  secret: string;
  url: string;
}

export interface DeliveryResource {
  abandoned_at: string | null;
  attempts: number;
  created_at: string;
  delivered_at: string | null;
  domain_id: string | null;
  event_id: string;
  event_type: string;
  id: string;
  last_attempt_at: string | null;
  last_error: string | null;
  last_status: number | null;
  next_attempt_at: string | null;
  state: "pending" | "delivered" | "abandoned";
}

export interface ReplayResult {
  requeued: number;
}

export interface WebhookEventData {
  domain: DomainResource;
}

/** Payload delivered to an application's webhook endpoint (delivery is tracked in #10). */
export interface WebhookEvent {
  created_at: string;
  data: WebhookEventData;
  /** Stable event id; deliveries of the same event share it. */
  id: string;
  type: "domain.ready" | "domain.attention_required" | "domain.recovered" | "domain.deleted";
}
