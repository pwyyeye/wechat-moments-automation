# Publisher Agent V2 contract

V2 adds a generic executor/account registry without changing any V1 payload or
route. A Desktop Agent Host reports all local executors and their currently
authenticated accounts in one heartbeat.

## Routes

- `GET /openapi/publisher-agent/v2/meta`
- `POST /openapi/publisher-agent/v2/agents/heartbeat`
- `POST /openapi/publisher-agent/v2/tasks/claim`
- `POST /openapi/publisher-agent/v2/tasks/{taskId}/lease/renew`
- `POST /openapi/publisher-agent/v2/tasks/{taskId}/events`

Every write request uses `X-Agent-Id`, `X-Agent-Instance-Id`, and
`X-Request-Id`. Event requests additionally use `Idempotency-Key`, equal to
the event ID.

## Exact routing key

The server only leases a task when all fields match:

```text
deviceId
  + executorInstanceId
  + providerKey
  + platform
  + operation
  + profileId (when present)
  + accountStableId
```

The claim request repeats the accounts visible at execution time. This closes
the race where a browser or WeChat session switches account after an earlier
heartbeat. Expected routing errors are `ACCOUNT_MISMATCH`,
`CAPABILITY_MISMATCH`, and `CONNECTOR_UNAVAILABLE`.

`draft_created` is a terminal success for a draft operation, but it is not a
published result. The event must include `result.output.draftOnly=true`.
