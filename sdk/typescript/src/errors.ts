// Errors raised by the client: one class per family, as in the Python SDK.
// `code` is the API's stable error code (docs/api-v1.md).

export interface ApiErrorInit {
  status: number;
  code: string;
  message: string;
  details?: Record<string, unknown> | null;
  requestId?: string | undefined;
}

/** An error response from the API. */
export class ApiError extends Error {
  readonly status: number;
  /** Stable machine-readable code, for example `hostname_already_claimed`. */
  readonly code: string;
  readonly details: Record<string, unknown>;
  /** The `X-Request-Id` response header, when a proxy in front of the API sets one. */
  readonly requestId: string | undefined;

  constructor(init: ApiErrorInit) {
    super(init.message);
    this.name = new.target.name;
    this.status = init.status;
    this.code = init.code;
    this.details = init.details ?? {};
    this.requestId = init.requestId;
  }
}

/** 401: missing, invalid, revoked or expired credential, or a suspended application. */
export class AuthenticationError extends ApiError {}

/** 403: the credential isn't allowed to do this. */
export class PermissionDeniedError extends ApiError {}

/** 403 `credential_read_only`: a read-only key tried to change something (only GET and HEAD are allowed). */
export class CredentialReadOnlyError extends PermissionDeniedError {}

/** 404: no such resource in this application. */
export class NotFoundError extends ApiError {}

/** 409: hostname already claimed, domain limit, invalid status transition, request in progress. */
export class ConflictError extends ApiError {}

/**
 * 409 `domain_connect_unavailable`: one-step setup can't be offered for this
 * domain (`setup.method` isn't `domain_connect`, the domain isn't live, or the
 * signer refused it). Send the customer to `setup.guide_url` instead.
 */
export class DomainConnectUnavailableError extends ConflictError {}

/**
 * 409 `connect_page_unavailable`: this deployment serves no setup page (it
 * needs `PUBLIC_API=true` and `EDGE_HOSTNAME`). Show the DNS records yourself.
 */
export class ConnectPageUnavailableError extends ConflictError {}

/** 422: invalid hostname, reference, body, or an idempotency key reused with another body. */
export class ValidationError extends ApiError {}

/** 429: too many requests; wait `retryAfter` seconds. */
export class RateLimitedError extends ApiError {
  /** Seconds to wait, from `Retry-After` (1 when the header is absent). */
  readonly retryAfter: number;

  constructor(init: ApiErrorInit & { retryAfter?: number | undefined }) {
    super(init);
    this.retryAfter = init.retryAfter ?? 1;
  }
}

/** 5xx. */
export class ServerError extends ApiError {}

/** The request never produced an HTTP response (connection error, timeout). */
export class TransportError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "TransportError";
  }
}

export function errorFor(init: ApiErrorInit & { retryAfter?: number | undefined }): ApiError {
  const { status } = init;
  if (status === 401) return new AuthenticationError(init);
  if (status === 403) {
    return init.code === "credential_read_only"
      ? new CredentialReadOnlyError(init)
      : new PermissionDeniedError(init);
  }
  if (status === 404) return new NotFoundError(init);
  if (status === 409) {
    if (init.code === "domain_connect_unavailable") return new DomainConnectUnavailableError(init);
    if (init.code === "connect_page_unavailable") return new ConnectPageUnavailableError(init);
    return new ConflictError(init);
  }
  if (status === 422) return new ValidationError(init);
  if (status === 429) return new RateLimitedError(init);
  if (status >= 500) return new ServerError(init);
  return new ApiError(init);
}
