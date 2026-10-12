// Resolve the workspace from the edge's assertion in a web framework.
//
// `WorkspaceResolver` is framework neutral: give it the request headers and
// host and it returns the verified assertion or throws. `createRequestVerifier`
// and `withCustomDomain` do what the Python SDK's `CustomDomainMiddleware`
// does, for Fetch API `Request`s (Hono, Next.js, Cloudflare Workers, Deno,
// Bun): they reject a request whose assertion is invalid with 403, reject
// one without an assertion unless `onMissing` is "passthrough", let the
// origin-verification probe through, and answer the workspace probe
// (`/.well-known/custom-domain-workspace`) the lifecycle worker uses.

import {
  ASSERTION_HEADER,
  type Assertion,
  AssertionInvalid,
  type AssertionKeys,
  DEFAULT_SKEW,
  verifyAssertion,
} from "./assertion.js";

export const WORKSPACE_PATH = "/.well-known/custom-domain-workspace";
/**
 * Served by the application for the operator's proof-of-control check. That
 * probe comes from the management API, not through the edge, so it carries no
 * assertion and must not be rejected.
 */
export const ORIGIN_VERIFICATION_PATH = "/.well-known/custom-domain-origin-verification";

/**
 * Which header carries the customer's hostname, matching the origin's
 * setting at the edge: `customer` (default) checks `Host`; `origin` (an
 * origin behind a CDN or a host-routed proxy) checks `X-Forwarded-Host`.
 */
export type HostHeaderMode = "customer" | "origin";

/** Request headers in any of the usual shapes. */
export type HeadersLike =
  | Headers
  | Readonly<Record<string, string | readonly string[] | undefined>>
  | Iterable<readonly [string, string]>;

export interface ResolverOptions {
  /** Verification keys by id (current and previous during a rotation). */
  keys: AssertionKeys;
  /** Your application's id, as given by the operator. */
  applicationId: string;
  /** Allowed clock skew in seconds. Default 30. */
  skew?: number;
  hostHeader?: HostHeaderMode;
}

function headerLookup(headers: HeadersLike): (name: string) => string | undefined {
  if (typeof Headers !== "undefined" && headers instanceof Headers) {
    return (name) => headers.get(name) ?? undefined;
  }
  const map = new Map<string, string>();
  const entries: Iterable<readonly [string, unknown]> =
    Symbol.iterator in headers
      ? (headers as Iterable<readonly [string, string]>)
      : Object.entries(headers as Record<string, unknown>);
  for (const [name, value] of entries) {
    if (value === undefined) continue;
    map.set(name.toLowerCase(), Array.isArray(value) ? value.join(", ") : String(value));
  }
  return (name) => map.get(name.toLowerCase());
}

/** Verifies the edge's assertion for a request. */
export class WorkspaceResolver {
  readonly applicationId: string;
  readonly skew: number;
  readonly hostHeader: HostHeaderMode;
  readonly #keys: AssertionKeys;

  constructor(options: ResolverOptions) {
    const mode = options.hostHeader ?? "customer";
    if (mode !== "customer" && mode !== "origin") {
      throw new TypeError("hostHeader must be 'customer' or 'origin'");
    }
    if (!options.applicationId) throw new TypeError("applicationId is required");
    this.#keys = options.keys instanceof Map ? new Map(options.keys) : { ...options.keys };
    this.applicationId = options.applicationId;
    this.skew = options.skew ?? DEFAULT_SKEW;
    this.hostHeader = mode;
  }

  /**
   * Verify the assertion for this request; throws {@link AssertionInvalid}.
   * `host` is the request's `Host` header (a port is ignored).
   */
  async resolve(
    headers: HeadersLike,
    host: string | null | undefined,
    options: { now?: number } = {},
  ): Promise<Assertion> {
    const get = headerLookup(headers);
    const token = get(ASSERTION_HEADER);
    let hostValue = host ?? undefined;
    if (this.hostHeader === "origin") {
      // The first value is the edge's: it replaces whatever the visitor sent.
      hostValue = (get("x-forwarded-host") ?? "").split(",", 1)[0]?.trim() || undefined;
      if (hostValue === undefined && token) {
        throw new AssertionInvalid(
          "wrong_hostname",
          "No X-Forwarded-Host: is the origin in 'origin' mode?",
        );
      }
    }
    const hostname = hostValue ? hostValue.split(":", 1)[0] : null;
    return verifyAssertion(token, this.#keys, {
      applicationId: this.applicationId,
      hostname,
      skew: this.skew,
      ...(options.now !== undefined ? { now: options.now } : {}),
    });
  }
}

/** Returns a truthy value only when the application really serves the workspace. */
export type WorkspaceLookup = (reference: string) => unknown;

export interface MiddlewareOptions extends ResolverOptions {
  /**
   * Called with the reference on the workspace probe; must return a truthy
   * value (or a promise of one) only when the application actually serves
   * that workspace, from the same source your request handlers use. The
   * probe answers 200 only then, so readiness proves real tenant selection.
   */
  workspaceLookup: WorkspaceLookup;
  /**
   * `reject` (default): a request without an assertion gets 403.
   * `passthrough`: it continues with no assertion, for an application that
   * also serves its own domain and decides per request.
   */
  onMissing?: "reject" | "passthrough";
}

/** What the shared logic decided for one request. */
export type Decision =
  | { kind: "respond"; status: number; body: Record<string, unknown> }
  | { kind: "continue"; assertion: Assertion | null };

/** The framework-neutral core shared by the Fetch and Express middleware. */
export class CustomDomainGuard {
  readonly resolver: WorkspaceResolver;
  readonly onMissing: "reject" | "passthrough";
  readonly #lookup: WorkspaceLookup;

  constructor(options: MiddlewareOptions) {
    const onMissing = options.onMissing ?? "reject";
    if (onMissing !== "reject" && onMissing !== "passthrough") {
      throw new TypeError("onMissing must be 'reject' or 'passthrough'");
    }
    if (typeof options.workspaceLookup !== "function") {
      throw new TypeError("workspaceLookup must be a function taking the workspace reference");
    }
    this.resolver = new WorkspaceResolver(options);
    this.onMissing = onMissing;
    this.#lookup = options.workspaceLookup;
  }

  async decide(request: {
    method: string;
    path: string;
    headers: HeadersLike;
    host: string | null | undefined;
  }): Promise<Decision> {
    if (request.path === ORIGIN_VERIFICATION_PATH && request.method.toUpperCase() === "GET") {
      return { kind: "continue", assertion: null };
    }
    let assertion: Assertion | null;
    try {
      assertion = await this.resolver.resolve(request.headers, request.host);
    } catch (error) {
      if (!(error instanceof AssertionInvalid)) throw error;
      if (error.code === "missing" && this.onMissing === "passthrough") {
        assertion = null;
      } else {
        return {
          kind: "respond",
          status: 403,
          body: { error: `assertion_${error.code}`, message: error.message },
        };
      }
    }
    if (request.path === WORKSPACE_PATH) {
      if (assertion === null) {
        return { kind: "respond", status: 403, body: { error: "assertion_missing" } };
      }
      if (!(await this.#lookup(assertion.reference))) {
        return { kind: "respond", status: 404, body: { error: "workspace_not_found" } };
      }
      return {
        kind: "respond",
        status: 200,
        body: { reference: assertion.reference, application: assertion.applicationId },
      };
    }
    return { kind: "continue", assertion };
  }
}

/**
 * The outcome for a Fetch API request: either a `response` to return as is
 * (a 403, or the workspace probe's answer), or the verified `assertion`
 * (`null` only with `onMissing: "passthrough"` or on the origin-verification
 * probe).
 */
export type VerifyOutcome =
  | { response: Response; assertion?: undefined }
  | { response?: undefined; assertion: Assertion | null };

function requestHost(request: Request): string | null {
  return request.headers.get("host") ?? new URL(request.url).host;
}

/**
 * A function that checks a Fetch API `Request` the way the Python SDK's
 * `CustomDomainMiddleware` does. Use it in Hono, Next.js middleware and route
 * handlers, Cloudflare Workers, Deno and Bun.
 */
export function createRequestVerifier(
  options: MiddlewareOptions,
): (request: Request) => Promise<VerifyOutcome> {
  const guard = new CustomDomainGuard(options);
  return async (request) => {
    const decision = await guard.decide({
      method: request.method,
      path: new URL(request.url).pathname,
      headers: request.headers,
      host: requestHost(request),
    });
    if (decision.kind === "respond") {
      return { response: Response.json(decision.body, { status: decision.status }) };
    }
    return { assertion: decision.assertion };
  };
}

/**
 * Wrap a Fetch API handler: it runs only for requests that pass, with the
 * verified assertion as its second argument. Extra arguments (a Worker's
 * `env` and `ctx`, a route's `context`) are passed through.
 */
export function withCustomDomain<Rest extends unknown[]>(
  options: MiddlewareOptions,
  handler: (
    request: Request,
    assertion: Assertion | null,
    ...rest: Rest
  ) => Response | Promise<Response>,
): (request: Request, ...rest: Rest) => Promise<Response> {
  const verify = createRequestVerifier(options);
  return async (request, ...rest) => {
    const outcome = await verify(request);
    if (outcome.response) return outcome.response;
    return handler(request, outcome.assertion, ...rest);
  };
}
