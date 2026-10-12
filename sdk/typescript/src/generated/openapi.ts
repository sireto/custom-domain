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

export interface SetupProvider {
  /** Stable provider id, such as `cloudflare`, `godaddy` or `route53`; `unknown` when the provider was not recognized. */
  id: string;
  /** Display name. */
  name: string;
}

/** How the customer will publish the DNS records, from the detected DNS provider. */
export interface DomainSetup {
  /** When the zone was last looked up. Null until then. */
  detected_at: string | null;
  /** Setup instructions for the provider. Null until the zone has been looked up. */
  guide_url: string | null;
  /** The hostname relative to `zone`: what most DNS providers want in the record's Name or Host field. */
  host: string;
  /** `domain_connect`: the provider supports one-step setup through Domain Connect and the service can use it. `guided`: follow the provider's guide at `guide_url`. `manual`: the provider has no specific guide; `guide_url` is the generic one. Null until the service has looked up the hostname's DNS zone. */
  method: "domain_connect" | "guided" | "manual" | null;
  /** The DNS provider hosting the zone, from its nameservers. Null until the zone has been looked up. */
  provider: SetupProvider | null;
  /** The DNS zone the records go in (the registrable domain until the zone has been looked up). */
  zone: string;
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
  setup: DomainSetup;
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

/** Ask for a signed Domain Connect apply URL for one domain. */
export interface ConnectRequest {
  /** Where the DNS provider sends the customer when they are done: an https URL with a host, no user name and no fragment, at most 512 bytes. It ends up in the signed URL, so it is the application's responsibility; point it at a page of your own. The customer arrives there with your `state` (and `error=access_denied` if they declined). */
  redirect_uri?: string | null;
  /** Opaque value returned to `redirect_uri` unchanged, at most 512 bytes without control characters. */
  state?: string | null;
}

export interface ConnectResource {
  /** Ask for a new URL after this time. The DNS records in it stay valid as long as the domain's records do. */
  expires_at: string;
  /** The DNS provider the URL goes to. */
  provider: SetupProvider | null;
  /** The signed apply URL at the customer's DNS provider. Send the customer there (a link or a redirect); they approve the two records in one step. */
  url: string;
}

/** Ask for a link to the setup page for one domain. */
export interface ConnectSessionRequest {
  /** Your application's name, shown on the page as "Back to <app_name>" when `return_url` is set. At most 64 characters without control characters. */
  app_name?: string | null;
  /** Where "Back to <app_name>" goes: an https URL with a host, no user name and no fragment, at most 512 bytes. */
  return_url?: string | null;
  /** How long the link works, in seconds: 300 to 86400. Default 86400 (24 hours). */
  ttl_seconds?: number | null;
}

export interface ConnectSessionResource {
  created_at: string;
  /** The link stops working after this time. */
  expires_at: string;
  /** The session's id, to revoke it. */
  id: string;
  /** The setup page for the customer: `https://<edge hostname>/connect/<token>`. Anyone with it can see the domain's setup and status, ask for a check and start one-step setup, so send it only to the customer. It is shown only in this response. */
  url: string;
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
