// Express / Connect middleware: the Python SDK's `CustomDomainMiddleware` for
// Node's request/response objects. Verified requests carry the assertion as
// `req.customDomain` (or the property named by `stateKey`).
//
// The types below are structural, so neither Express nor its types are a
// dependency: plain `node:http`, Connect, Express 4 and 5 all fit.

import type { Assertion } from "./assertion.js";
import { CustomDomainGuard, type MiddlewareOptions } from "./middleware.js";

/** The parts of an incoming request the middleware reads. */
export interface NodeRequestLike {
  method?: string | undefined;
  /** The request target, such as `/path?query` (Express rewrites it inside routers). */
  url?: string | undefined;
  /** Express's original request target, preferred over `url` when present. */
  originalUrl?: string | undefined;
  headers: Record<string, string | string[] | undefined>;
}

/** The parts of a response the middleware writes. */
export interface NodeResponseLike {
  statusCode: number;
  setHeader(name: string, value: string): unknown;
  end(body?: string): unknown;
}

export interface ExpressMiddlewareOptions extends MiddlewareOptions {
  /** The request property the assertion is stored on. Default `customDomain`. */
  stateKey?: string;
}

/** A request after the middleware ran; `customDomain` is `null` only in passthrough cases. */
export interface CustomDomainRequest {
  customDomain?: Assertion | null;
}

function pathOf(request: NodeRequestLike): string {
  const target = request.originalUrl ?? request.url ?? "/";
  const end = target.search(/[?#]/);
  return end < 0 ? target : target.slice(0, end);
}

function firstHeader(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}

/**
 * Express/Connect middleware: `app.use(customDomainMiddleware({...}))`.
 * Rejects with 403 JSON as the Python middleware does, answers the workspace
 * probe, and otherwise calls `next()` with the assertion on the request.
 */
export function customDomainMiddleware(
  options: ExpressMiddlewareOptions,
): (request: NodeRequestLike, response: NodeResponseLike, next: (error?: unknown) => void) => void {
  const guard = new CustomDomainGuard(options);
  const stateKey = options.stateKey ?? "customDomain";
  return (request, response, next) => {
    guard
      .decide({
        method: request.method ?? "GET",
        path: pathOf(request),
        headers: request.headers,
        host: firstHeader(request.headers.host),
      })
      .then((decision) => {
        if (decision.kind === "respond") {
          const body = JSON.stringify(decision.body);
          response.statusCode = decision.status;
          response.setHeader("Content-Type", "application/json");
          response.setHeader("Content-Length", String(new TextEncoder().encode(body).length));
          response.end(body);
          return;
        }
        (request as unknown as Record<string, unknown>)[stateKey] = decision.assertion;
        next();
      }, next);
  };
}
