# Webhooks

Status: implemented for issue #10. The payload contract was published with
the API in #15; this adds subscriptions, signing, durable delivery and
recovery.

## Subscribing

```
POST /v1/webhooks
{"url": "https://app.acme.example/hooks/custom-domain", "events": ["domain.ready", "domain.deleted"]}
```

returns the subscription with its signing `secret`, shown once. Up to ten
active subscriptions per application. The URL must be `https`, must not
carry credentials, and must resolve to a public address (the same rule as
origin verification; `ORIGIN_ALLOW_PRIVATE=true` relaxes it for self-hosted
deployments). Subscriptions only ever receive events of their own
application.

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/webhooks` | List subscriptions (no secrets). |
| `DELETE /v1/webhooks/{id}` | Revoke: pending deliveries are abandoned, history stays. |
| `POST /v1/webhooks/{id}/rotate` | New secret; the previous one keeps signing for 24 hours. |
| `GET /v1/webhooks/{id}/deliveries?state=pending|delivered|abandoned` | Delivery history with attempts, status codes and errors. |
| `POST /v1/webhooks/{id}/deliveries/{delivery_id}/replay` | Send that payload again. |
| `POST /v1/webhooks/{id}/replay?since=<ISO time>` | Re-queue everything since a time: the recovery path after a consumer outage. |

## Events

| Type | Sent when |
| --- | --- |
| `domain.ready` | A domain first passes all checks and enters `ready`. |
| `domain.attention_required` | A ready domain fails a check; serving has stopped. |
| `domain.recovered` | An `attention_required` domain is `ready` again. |
| `domain.deleted` | A domain was deleted. |

Payload:

```json
{
  "id": "<event id, stable across retries and replays>",
  "type": "domain.ready",
  "created_at": "2026-09-25T15:00:00+00:00",
  "data": {"domain": {"id": "...", "hostname": "...", "reference": "...", "status": "ready",
                      "dns_records": [...], "checks": [...], "created_at": "...", "updated_at": "...",
                      "deleted_at": null}}
}
```

`data.domain` is the domain resource as it was when the event happened
(snapshotted at enqueue time), so a consumer needs no follow-up read to know
the hostname, workspace reference and check results, and later purges never
change what was delivered. Event ids are the ids of the underlying domain
events.

## Signature

Every delivery carries

```
X-Custom-Domain-Signature: t=<unix seconds>,v1=<hex>[,v1=<hex>]
X-Custom-Domain-Event: domain.ready
X-Custom-Domain-Event-Id: <event id>
X-Custom-Domain-Delivery-Id: <delivery id>
X-Custom-Domain-Attempt: <n>
```

Each `v1` is HMAC-SHA256 over `<t>.<raw body>` with one of the
subscription's secrets. Verify by recomputing with every secret you hold and
comparing in constant time against any listed `v1`; reject if `t` is more
than five minutes from now. During a rotation the service signs with both
the new and the previous secret, so switch the consumer to the new secret
any time within 24 hours. The webhook secret is separate from API
credentials and from the edge assertion key.

`app.webhooks.signature.verify()` implements this and the SDK (#11) ships it.

## Delivery guarantees

- Deliveries are created in the same database transaction as the event that
  causes them (transactional outbox), so an event is never recorded without
  its deliveries.
- The delivery worker (in the API process by default, `WEBHOOK_WORKER_ENABLED`,
  every `WEBHOOK_WORKER_INTERVAL` seconds, or in `custom-domain worker run`)
  leases each delivery so several workers never send the same one twice,
  POSTs with a 10 second timeout and no redirects, and treats any 2xx as
  success.
- Failures (non-2xx, timeouts, connection errors) retry at 1, 5, 30
  minutes, then 2, 12 and 24 hours, up to 8 attempts, then the delivery is
  marked abandoned. Every attempt's status and error are kept in the history.
- At least once: retries and replays can deliver the same event more than
  once, and deliveries for one domain can arrive out of order. Consumers
  deduplicate by event `id` and compare `created_at` before moving state.

## Consumer sample

`examples/webhook_consumer.py` verifies the signature, ignores duplicate
event ids, and keeps only the newest event per domain, so a late delivery
of an older event never moves state backwards. Its `Consumer.handle` is
exercised in `tests/test_webhooks.py` with a duplicate, an out-of-order
pair and a stale timestamp.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `WEBHOOK_WORKER_ENABLED` | `true` | Run the delivery worker in this process. |
| `WEBHOOK_WORKER_INTERVAL` | `5` | Seconds between delivery passes. |
| `ORIGIN_ALLOW_PRIVATE` | `false` | Also allows non-public webhook URLs and plain http. |
