// Typed client for the v1 API (docs/api-v1.md).
//
// Timeouts and retries: every request has a timeout (`timeoutMs`, 10 s by
// default). Requests that are safe to repeat (GET, DELETE, a domain create
// with an idempotency key, and the webhook replays) are retried up to
// `maxRetries` times on connection errors, timeouts, 429 (honouring
// Retry-After) and 5xx, with exponential backoff. A create without an
// idempotency key, a recheck, a webhook create and a secret rotation are never
// retried automatically: the caller decides. The same rules as the Python SDK.

import { errorFor, RateLimitedError, TransportError } from "./errors.js";
import type {
  Delivery,
  DeliveryState,
  Domain,
  DomainCreateInput,
  DomainStatus,
  Page,
  Webhook,
  WebhookEventType,
  WebhookWithSecret,
} from "./models.js";
import { VERSION } from "./version.js";

export const DEFAULT_TIMEOUT_MS = 10_000;
export const DEFAULT_MAX_RETRIES = 2;
export const USER_AGENT = `custom-domain-sdk-ts/${VERSION}`;

type FetchLike = (input: string, init: RequestInit) => Promise<Response>;

export interface ClientOptions {
  /** The API's base URL, for example `https://edge.example.net` (with or without `/v1`). */
  baseUrl: string;
  /** The application credential (`cd_...`) issued by the operator. Keep it server side. */
  apiKey: string;
  /** Per-attempt timeout in milliseconds. Default 10 000. */
  timeoutMs?: number;
  /** Retries for requests that are safe to repeat. Default 2. */
  maxRetries?: number;
  /**
   * Longest `Retry-After` the client waits for on its own, in milliseconds.
   * A 429 asking for longer is thrown as {@link RateLimitedError}. Default 60 000.
   */
  maxRetryDelayMs?: number;
  /** A `fetch` implementation; defaults to the global one. */
  fetch?: FetchLike;
  /** How to wait between retries (tests). */
  sleep?: (ms: number) => Promise<void>;
}

export interface RequestOptions {
  /** Cancels the request, including any retries still to come. */
  signal?: AbortSignal;
}

export interface CreateDomainOptions extends RequestOptions {
  /** Makes the call safe to retry; the client retries a create only with one. */
  idempotencyKey?: string;
}

export interface GetDomainOptions extends RequestOptions {
  includeDeleted?: boolean;
}

export interface ListDomainsParams extends RequestOptions {
  reference?: string;
  status?: DomainStatus;
  includeDeleted?: boolean;
  /** 1 to 200. Default 50. */
  limit?: number;
  offset?: number;
}

export interface IterDomainsParams extends RequestOptions {
  reference?: string;
  status?: DomainStatus;
  includeDeleted?: boolean;
  pageSize?: number;
}

export interface CreateWebhookInput {
  /** Absolute https URL that receives POST deliveries. */
  url: string;
  events: WebhookEventType[];
}

export interface ListDeliveriesParams extends RequestOptions {
  state?: DeliveryState;
  limit?: number;
  offset?: number;
}

type Query = Record<string, string | number | undefined>;

interface Call {
  method: "GET" | "POST" | "DELETE";
  path: string;
  query?: Query | undefined;
  body?: unknown;
  headers?: Record<string, string> | undefined;
  retryable: boolean;
  signal?: AbortSignal | undefined;
}

interface Answer {
  status: number;
  headers: Headers;
  text: string;
}

function errorOf(answer: Answer) {
  let body: { code?: unknown; message?: unknown; details?: unknown } = {};
  try {
    const envelope = (JSON.parse(answer.text) as { error?: unknown } | null)?.error;
    if (envelope && typeof envelope === "object") body = envelope;
  } catch {
    // not JSON: fall back to the status and the start of the text
  }
  const retryAfter = answer.headers.get("Retry-After");
  return errorFor({
    status: answer.status,
    code: typeof body.code === "string" ? body.code : "http_error",
    message: typeof body.message === "string" ? body.message : answer.text.slice(0, 200),
    details:
      body.details && typeof body.details === "object" && !Array.isArray(body.details)
        ? (body.details as Record<string, unknown>)
        : {},
    requestId: answer.headers.get("X-Request-Id") ?? undefined,
    retryAfter: retryAfter && /^\d+$/.test(retryAfter) ? Number(retryAfter) : undefined,
  });
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

function segment(id: string): string {
  // Ids are UUIDs; encoding keeps any other value inside its path segment.
  return encodeURIComponent(String(id));
}

export class CustomDomainClient {
  readonly baseUrl: string;
  readonly maxRetries: number;
  readonly timeoutMs: number;
  readonly maxRetryDelayMs: number;
  readonly #apiKey: string;
  readonly #fetch: FetchLike;
  readonly #sleep: (ms: number) => Promise<void>;

  constructor(options: ClientOptions) {
    if (typeof options.apiKey !== "string" || !options.apiKey.startsWith("cd_")) {
      throw new TypeError("apiKey must be an application credential (cd_...)");
    }
    // Accept the base URL with or without the /v1 suffix.
    this.baseUrl = options.baseUrl.replace(/\/+$/, "").replace(/\/v1$/, "");
    this.#apiKey = options.apiKey;
    this.timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    this.maxRetries = Math.max(0, options.maxRetries ?? DEFAULT_MAX_RETRIES);
    this.maxRetryDelayMs = options.maxRetryDelayMs ?? 60_000;
    const fetchImpl = options.fetch ?? globalThis.fetch;
    if (typeof fetchImpl !== "function") {
      throw new TypeError("No fetch implementation: pass options.fetch or use Node 20 or later");
    }
    this.#fetch = (input, init) => fetchImpl(input, init);
    this.#sleep = options.sleep ?? defaultSleep;
  }

  // --- transport ---------------------------------------------------------

  #url(path: string, query?: Query): string {
    const url = new URL(this.baseUrl + path);
    for (const [name, value] of Object.entries(query ?? {})) {
      if (value !== undefined) url.searchParams.set(name, String(value));
    }
    return url.toString();
  }

  async #attempt(call: Call): Promise<Answer> {
    const controller = new AbortController();
    const onAbort = () => controller.abort(call.signal?.reason);
    if (call.signal?.aborted) onAbort();
    call.signal?.addEventListener("abort", onAbort, { once: true });
    const timer = setTimeout(
      () => controller.abort(new TransportError(`no response within ${this.timeoutMs} ms`)),
      this.timeoutMs,
    );
    const headers: Record<string, string> = {
      Accept: "application/json",
      Authorization: `Bearer ${this.#apiKey}`,
      "User-Agent": USER_AGENT,
      ...call.headers,
    };
    let body: string | undefined;
    if (call.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(call.body);
    }
    try {
      const response = await this.#fetch(this.#url(call.path, call.query), {
        method: call.method,
        headers,
        body,
        signal: controller.signal,
        // Never follow a redirect with the credential; a 3xx is reported as an error.
        redirect: "manual",
      });
      // Read the body before the timer is cleared, so a stalled body times out too.
      const text = await response.text();
      return { status: response.status, headers: response.headers, text };
    } finally {
      clearTimeout(timer);
      call.signal?.removeEventListener("abort", onAbort);
    }
  }

  async #request<T>(call: Call): Promise<T> {
    const attempts = call.retryable ? this.maxRetries + 1 : 1;
    let delay = 500;
    let lastError: Error | undefined;
    for (let attempt = 0; attempt < attempts; attempt++) {
      const last = attempt === attempts - 1;
      let answer: Answer | undefined;
      try {
        answer = await this.#attempt(call);
      } catch (error) {
        if (call.signal?.aborted) throw call.signal.reason ?? error;
        const detail = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
        lastError = new TransportError(detail, { cause: error });
      }
      if (answer !== undefined) {
        if (answer.status >= 200 && answer.status < 300) {
          return (answer.text === "" ? undefined : JSON.parse(answer.text)) as T;
        }
        const error = errorOf(answer);
        if (last) throw error;
        if (error instanceof RateLimitedError) {
          const wait = error.retryAfter * 1000;
          if (wait > this.maxRetryDelayMs) throw error;
          await this.#sleep(wait);
          continue;
        }
        if (answer.status < 500) throw error;
        lastError = error;
      }
      if (last) break;
      await this.#sleep(delay);
      delay *= 2;
    }
    throw lastError ?? new TransportError("request failed");
  }

  // --- domains -----------------------------------------------------------

  /** Register a hostname for a workspace. Retried only when `idempotencyKey` is given. */
  createDomain(input: DomainCreateInput, options: CreateDomainOptions = {}): Promise<Domain> {
    const body: DomainCreateInput = { hostname: input.hostname, reference: input.reference };
    if (input.metadata !== undefined && input.metadata !== null) body.metadata = input.metadata;
    return this.#request<Domain>({
      method: "POST",
      path: "/v1/domains",
      body,
      headers: options.idempotencyKey ? { "Idempotency-Key": options.idempotencyKey } : undefined,
      retryable: Boolean(options.idempotencyKey),
      signal: options.signal,
    });
  }

  /** One domain with its current checks. */
  getDomain(id: string, options: GetDomainOptions = {}): Promise<Domain> {
    return this.#request<Domain>({
      method: "GET",
      path: `/v1/domains/${segment(id)}`,
      query: { include_deleted: options.includeDeleted ? "true" : undefined },
      retryable: true,
      signal: options.signal,
    });
  }

  /** One page of domains, in creation order. */
  listDomains(params: ListDomainsParams = {}): Promise<Page> {
    return this.#request<Page>({
      method: "GET",
      path: "/v1/domains",
      query: {
        limit: params.limit ?? 50,
        offset: params.offset ?? 0,
        reference: params.reference,
        status: params.status,
        include_deleted: params.includeDeleted ? "true" : undefined,
      },
      retryable: true,
      signal: params.signal,
    });
  }

  /** Every matching domain, fetching pages as needed. */
  async *iterDomains(params: IterDomainsParams = {}): AsyncGenerator<Domain, void, undefined> {
    const { pageSize = 100, ...filters } = params;
    let offset: number | null = 0;
    while (offset !== null) {
      const page: Page = await this.listDomains({ ...filters, limit: pageSize, offset });
      yield* page.items;
      offset = page.next_offset;
    }
  }

  /**
   * Ask for all checks to run again, after the customer fixed DNS. Not
   * retried: rechecks are rate limited server side.
   */
  recheckDomain(id: string, options: RequestOptions = {}): Promise<Domain> {
    return this.#request<Domain>({
      method: "POST",
      path: `/v1/domains/${segment(id)}/checks`,
      retryable: false,
      signal: options.signal,
    });
  }

  /** Stop serving, revoke ownership and leave a tombstone. Deleting twice is a no-op. */
  deleteDomain(id: string, options: RequestOptions = {}): Promise<Domain> {
    return this.#request<Domain>({
      method: "DELETE",
      path: `/v1/domains/${segment(id)}`,
      retryable: true,
      signal: options.signal,
    });
  }

  // --- webhooks ----------------------------------------------------------

  /** Subscribe to domain events. The signing `secret` is in this response only. */
  createWebhook(input: CreateWebhookInput, options: RequestOptions = {}): Promise<WebhookWithSecret> {
    return this.#request<WebhookWithSecret>({
      method: "POST",
      path: "/v1/webhooks",
      body: { url: input.url, events: input.events },
      retryable: false,
      signal: options.signal,
    });
  }

  /** The application's subscriptions, without secrets. */
  listWebhooks(options: RequestOptions = {}): Promise<Webhook[]> {
    return this.#request<Webhook[]>({
      method: "GET",
      path: "/v1/webhooks",
      retryable: true,
      signal: options.signal,
    });
  }

  /**
   * Issue a new signing secret (in this response only). The previous secret
   * keeps signing as well for 24 hours.
   */
  rotateWebhook(id: string, options: RequestOptions = {}): Promise<WebhookWithSecret> {
    return this.#request<WebhookWithSecret>({
      method: "POST",
      path: `/v1/webhooks/${segment(id)}/rotate`,
      retryable: false,
      signal: options.signal,
    });
  }

  /** Revoke a subscription: deliveries stop, pending ones are abandoned, history stays. */
  deleteWebhook(id: string, options: RequestOptions = {}): Promise<Webhook> {
    return this.#request<Webhook>({
      method: "DELETE",
      path: `/v1/webhooks/${segment(id)}`,
      retryable: true,
      signal: options.signal,
    });
  }

  /** Delivery history of a subscription. */
  listDeliveries(webhookId: string, params: ListDeliveriesParams = {}): Promise<Delivery[]> {
    return this.#request<Delivery[]>({
      method: "GET",
      path: `/v1/webhooks/${segment(webhookId)}/deliveries`,
      query: { limit: params.limit ?? 50, offset: params.offset ?? 0, state: params.state },
      retryable: true,
      signal: params.signal,
    });
  }

  /** Queue one delivery's payload again. Consumers deduplicate by event id. */
  replayDelivery(webhookId: string, deliveryId: string, options: RequestOptions = {}): Promise<Delivery> {
    return this.#request<Delivery>({
      method: "POST",
      path: `/v1/webhooks/${segment(webhookId)}/deliveries/${segment(deliveryId)}/replay`,
      retryable: true,
      signal: options.signal,
    });
  }

  /** Re-queue every delivery created at or after `since`; returns how many. */
  async replaySince(webhookId: string, since: Date | string, options: RequestOptions = {}): Promise<number> {
    const result = await this.#request<{ requeued: number }>({
      method: "POST",
      path: `/v1/webhooks/${segment(webhookId)}/replay`,
      query: { since: since instanceof Date ? since.toISOString() : since },
      retryable: true,
      signal: options.signal,
    });
    return result.requeued;
  }
}
