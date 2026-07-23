# Architecture (sanitized)

## Loop (every ~60s)

1. **Scout** — discover candidate communities (keyword search, seed mining, related discussion groups, optional web directory).
2. **Evaluate** — score and persist candidates (grade S/A/B/C); filter non-writable channels.
3. **Scheduler** — assign join vs message tasks under quotas and account health.
4. **Executor (Infiltrator)** — perform joins/messages with stage-aware behavior.
5. **Content** — AI or template generation; similarity / anti-spam checks.
6. **Risk** — per-account rate limits, FloodWait handling, freeze recovery, circuit breaker.

## Harness-style controls

| Concern | Mechanism |
|---------|-----------|
| Tool / action constraints | Daily caps on messages, joins, DMs, links |
| Policy compliance | Promo ratio limits, age-tier policies, hibernate/abandon |
| State verification | Account/group membership and task state in PostgreSQL |
| Execution audit | MessageLog / AgentTask history + structured logs |
| Failure recovery | FloodWait timers, retries, circuit breaker, 7×24 loop |

## Persistence

- **PostgreSQL** — durable accounts, groups, tasks, workflow-related rows  
- **Redis** — hot coordination / queues as configured  
- **Sessions** — local Telethon session files (never commit)

## Dashboard

`dashboard/` exposes account health, task progress, and system KPIs for ops visibility.
