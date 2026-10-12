/**
 * Custom Domain SDK for TypeScript and JavaScript.
 *
 * - {@link CustomDomainClient} talks to the v1 API.
 * - {@link verifyAssertion} and {@link WorkspaceResolver} verify the signed
 *   workspace assertion the edge adds to every proxied request.
 * - {@link createRequestVerifier} / {@link withCustomDomain} (Fetch API) and
 *   {@link customDomainMiddleware} (Express, Connect) do that per request and
 *   serve the workspace probe.
 * - {@link verifyWebhook} and {@link parseEvent} handle webhook deliveries.
 *
 * @packageDocumentation
 */

export {
  ASSERTION_HEADER,
  type Assertion,
  type AssertionErrorCode,
  AssertionInvalid,
  type AssertionKeys,
  type AssertionPayload,
  DEFAULT_SKEW,
  verifyAssertion,
  type VerifyAssertionOptions,
} from "./assertion.js";
export {
  type ClientOptions,
  type CreateDomainOptions,
  type CreateWebhookInput,
  CustomDomainClient,
  DEFAULT_MAX_RETRIES,
  DEFAULT_TIMEOUT_MS,
  type GetDomainOptions,
  type IterDomainsParams,
  type ListDeliveriesParams,
  type ListDomainsParams,
  type RequestOptions,
  USER_AGENT,
} from "./client.js";
export type { Secret } from "./crypto.js";
export {
  ApiError,
  AuthenticationError,
  ConflictError,
  NotFoundError,
  RateLimitedError,
  ServerError,
  TransportError,
  ValidationError,
} from "./errors.js";
export {
  type CustomDomainRequest,
  customDomainMiddleware,
  type ExpressMiddlewareOptions,
  type NodeRequestLike,
  type NodeResponseLike,
} from "./express.js";
export {
  createRequestVerifier,
  CustomDomainGuard,
  type Decision,
  type HeadersLike,
  type HostHeaderMode,
  type MiddlewareOptions,
  ORIGIN_VERIFICATION_PATH,
  type ResolverOptions,
  type VerifyOutcome,
  withCustomDomain,
  WORKSPACE_PATH,
  type WorkspaceLookup,
  WorkspaceResolver,
} from "./middleware.js";
export {
  type Check,
  type CheckStatus,
  type CheckType,
  type Delivery,
  type DeliveryState,
  type DnsRecord,
  type Domain,
  type DomainCreateInput,
  type DomainStatus,
  type ErrorBody,
  type ErrorResponse,
  findCheck,
  isReady,
  type Page,
  renderDnsInstructions,
  renderDnsRecord,
  type Webhook,
  type WebhookCreateInput,
  type WebhookEvent,
  type WebhookEventType,
  type WebhookWithSecret,
} from "./models.js";
export { VERSION } from "./version.js";
export {
  DEFAULT_TOLERANCE,
  parseEvent,
  SIGNATURE_HEADER,
  type SignatureErrorCode,
  SignatureInvalid,
  verifyWebhook,
  type VerifyWebhookOptions,
} from "./webhooks.js";
