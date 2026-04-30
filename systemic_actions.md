# Systemic Action Items

_Identified by deterministic cross-comparison of `postmortem_a.md` and `postmortem_b.md`_

**Shared Failure Pattern:** Both incidents exhibit identical failure mode: scheduled batch jobs performing full table scans due to missing indexes on large tables (user_positions, open_orders), running during production hours (14:00 UTC, 09:30 UTC), holding database connections for 700+ seconds, exhausting the shared 100-connection pool, cascading to service timeouts, triggering circuit breakers, and halting business-critical operations (trading platform, order submission). Resolution in both cases was killing the pathological query to release connections.

---

## Systemic Action 1

| Field | Detail |
|-------|--------|
| **Incidents Affected** | incident_a, incident_b |
| **Shared Failure Pattern** | Both incidents exhibit identical failure mode: scheduled batch jobs performing full table scans due to missing indexes on large tables (user_positions, open_orders), running during production hours (14:00 UTC, 09:30 UTC), holding database connections for 700+ seconds, exhausting the shared 100-connection pool, cascading to service timeouts, triggering circuit breakers, and halting business-critical operations (trading platform, order submission). Resolution in both cases was killing the pathological query to release connections. |
| **Recommended Owner** | Data Engineering |
| **Implementation Priority** | HIGH |
| **Verification Method** | Zero incidents triggered during batch execution windows over 30-day observation |
| **Similarity Score** | 0.26 (keyword overlap) |

**Incident A action:** Reschedule `user_positions_recalc` batch job from 14:00 UTC to 02:00 UTC (off-peak hours)

**Incident B action:** . **[P0] Reschedule `open_orders_settlement` batch job**: Move execution from 09:30 UTC to 02:00 UTC to avoid overlap with production business hours. Owner: Platform Engineering. ETA: 2024-04-06.

---

## Systemic Action 2

| Field | Detail |
|-------|--------|
| **Incidents Affected** | incident_a, incident_b |
| **Shared Failure Pattern** | Both incidents exhibit identical failure mode: scheduled batch jobs performing full table scans due to missing indexes on large tables (user_positions, open_orders), running during production hours (14:00 UTC, 09:30 UTC), holding database connections for 700+ seconds, exhausting the shared 100-connection pool, cascading to service timeouts, triggering circuit breakers, and halting business-critical operations (trading platform, order submission). Resolution in both cases was killing the pathological query to release connections. |
| **Recommended Owner** | Database Engineering |
| **Implementation Priority** | HIGH |
| **Verification Method** | Zero incidents triggered during batch execution windows over 30-day observation |
| **Similarity Score** | 0.32 (keyword overlap) |

**Incident A action:** Implement 60-second query timeout for all `user_positions_recalc` batch job queries

**Incident B action:** . **[P0] Implement query timeout for `open_orders_settlement` job**: Configure 60-second maximum query timeout for all queries executed by `open_orders_settlement` batch job to prevent runaway query execution. Owner: Application Engineering. ETA: 2024-04-08.

---

## Systemic Action 3

| Field | Detail |
|-------|--------|
| **Incidents Affected** | incident_a, incident_b |
| **Shared Failure Pattern** | Both incidents exhibit identical failure mode: scheduled batch jobs performing full table scans due to missing indexes on large tables (user_positions, open_orders), running during production hours (14:00 UTC, 09:30 UTC), holding database connections for 700+ seconds, exhausting the shared 100-connection pool, cascading to service timeouts, triggering circuit breakers, and halting business-critical operations (trading platform, order submission). Resolution in both cases was killing the pathological query to release connections. |
| **Recommended Owner** | Database / Platform Engineering |
| **Implementation Priority** | HIGH |
| **Verification Method** | Pool utilization stays below 80% during batch job execution windows |
| **Similarity Score** | 0.26 (keyword overlap) |

**Incident A action:** Create dedicated connection pool (30 connections) for batch jobs, isolating `user_positions_recalc` from `pricing-service` production traffic

**Incident B action:** . **[P1] Create dedicated connection pool for batch operations**: Establish separate connection pool for `open_orders_settlement` and other batch jobs, isolated from `order-service` production connection pool. Owner: Database Engineering. ETA: 2024-04-12.

---

## Systemic Action 4

| Field | Detail |
|-------|--------|
| **Incidents Affected** | incident_a, incident_b |
| **Shared Failure Pattern** | Both incidents exhibit identical failure mode: scheduled batch jobs performing full table scans due to missing indexes on large tables (user_positions, open_orders), running during production hours (14:00 UTC, 09:30 UTC), holding database connections for 700+ seconds, exhausting the shared 100-connection pool, cascading to service timeouts, triggering circuit breakers, and halting business-critical operations (trading platform, order submission). Resolution in both cases was killing the pathological query to release connections. |
| **Recommended Owner** | Database / Platform Engineering |
| **Implementation Priority** | HIGH |
| **Verification Method** | Pool utilization stays below 80% during batch job execution windows |
| **Similarity Score** | 0.26 (keyword overlap) |

**Incident A action:** Increase database connection pool for `pricing-service` from 100 to 150 connections

**Incident B action:** . **[P1] Increase primary connection pool size**: Expand `order-service` database connection pool from 100 to 200 connections to provide buffer capacity during load spikes. Owner: Database Engineering. ETA: 2024-04-10.

---

## Systemic Action 5

| Field | Detail |
|-------|--------|
| **Incidents Affected** | incident_a, incident_b |
| **Shared Failure Pattern** | Both incidents exhibit identical failure mode: scheduled batch jobs performing full table scans due to missing indexes on large tables (user_positions, open_orders), running during production hours (14:00 UTC, 09:30 UTC), holding database connections for 700+ seconds, exhausting the shared 100-connection pool, cascading to service timeouts, triggering circuit breakers, and halting business-critical operations (trading platform, order submission). Resolution in both cases was killing the pathological query to release connections. |
| **Recommended Owner** | Database / Platform Engineering |
| **Implementation Priority** | HIGH |
| **Verification Method** | Pool utilization stays below 80% during batch job execution windows |
| **Similarity Score** | 0.29 (keyword overlap) |

**Incident A action:** Implement automated alerting when database connection pool waiting queue exceeds 20 connections

**Incident B action:** . **[P2] Add connection pool saturation alerting**: Implement alerting when database connection pool utilization exceeds 75% (75 of 100 connections) to provide early warning before exhaustion. Owner: SRE. ETA: 2024-04-12.

---
