# Post-Mortem: Trading Platform Outage Due to Missing Database Index

**Incident ID:** incident_a  
**Date:** March 15, 2024  
**Duration:** 17 minutes 44 seconds (14:09:01 UTC - 14:26:45 UTC)  
**MTTR:** 15.58 minutes  
**Impact:** Trading platform halted, unable to serve price quotes to users

## Incident Summary

On March 15, 2024 at 14:11:10 UTC, the trading platform halted operations and became unable to serve price quotes to users. The outage was caused by a missing composite index on the `user_positions` table (`user_id`, `position_date` columns), which forced the scheduled `user_positions_recalc` batch job to perform a 731-second full table scan. This pathological query (q_4489) exhausted the database connection pool (100 connections with 47 waiting), preventing the `pricing-service` from obtaining connections to serve requests. The API gateway circuit breaker opened after repeated 5000ms timeouts, halting all trading operations. The incident was resolved by killing query q_4489, which released database connections and allowed services to recover. Full trading operations resumed at 14:26:45 UTC after the circuit breaker closed.

## Timeline

| Timestamp (UTC) | Event |
|-----------------|-------|
| 2024-03-15 14:02:11 | API gateway operating normally with p99 latency at 120ms baseline |
| 2024-03-15 14:08:44 | API gateway p99 latency increased to 340ms (283% of baseline), first observable symptom |
| 2024-03-15 14:09:01 | Pricing service detected slow query q_4821 taking 2100ms |
| 2024-03-15 14:09:15 | Pricing service detected slow query q_4822 taking 3400ms, escalating query durations |
| 2024-03-15 14:10:33 | Database connection pool exhausted (100 connections, 47 waiting) |
| 2024-03-15 14:10:45 | API gateway reports upstream timeout to pricing-service (5000ms) |
| 2024-03-15 14:11:02 | Circuit breaker opened for pricing-service |
| 2024-03-15 14:11:10 | **Trading halted - platform unable to serve price quotes** |
| 2024-03-15 14:11:45 | PagerDuty alert triggered, on-call engineer notified |
| 2024-03-15 14:16:30 | Engineer acknowledged alert and began investigation |
| 2024-03-15 14:22:08 | Long-running query q_4489 killed after 731 seconds on user_positions table |
| 2024-03-15 14:22:09 | Database connection pool recovering, waiting queue reduced from 47 to 12 |
| 2024-03-15 14:23:15 | Pricing service query latency returning to normal |
| 2024-03-15 14:24:00 | Circuit breaker transitioned to HALF-OPEN state |
| 2024-03-15 14:25:30 | Circuit breaker closed, service fully recovered |
| 2024-03-15 14:26:45 | **Trading operations resumed** |
| 2024-03-15 14:45:00 | Root cause identified: query q_4489 from scheduled batch job user_positions_recalc |
| 2024-03-15 14:45:01 | Deep root cause identified: user_positions table missing index on user_id and position_date |

## Root Cause

The `user_positions` table was missing a composite index on (`user_id`, `position_date`) columns. When the scheduled `user_positions_recalc` batch job executed at 14:00 UTC, query q_4489 was forced to perform a full table scan, consuming a database connection for 731 seconds. This pathological query, combined with concurrent production traffic from the `pricing-service`, exhausted the database connection pool (100 total connections). The connection pool saturation prevented the `pricing-service` from obtaining connections to execute queries, causing cascading timeouts in the API gateway and ultimately triggering the circuit breaker that halted all trading operations.

This incident required both the missing database index AND the batch job scheduling during production trading hours to manifest the failure condition.

## Contributing Factors

1. **Batch job timing conflict:** `user_positions_recalc` batch job scheduled at 14:00 UTC during peak production trading hours, creating resource contention with live traffic
2. **Insufficient connection pool capacity:** Database connection pool size limited to 100 connections was inadequate to handle both the `user_positions_recalc` batch job and concurrent `pricing-service` production load
3. **No query timeout enforcement:** Batch job queries (specifically q_4489) had no timeout configured, allowing 731-second execution that held connections indefinitely
4. **Shared connection pool:** `pricing-service` and batch operations competed for the same database connection pool without isolation or prioritization
5. **Circuit breaker configuration:** API gateway circuit breaker timeout threshold of 5000ms was excessive relative to user experience requirements, delaying failure detection

## Severity Classification

**SEV1** - Trading platform was completely halted for 15.58 minutes, preventing all users from receiving price quotes and executing trades, directly impacting critical business revenue operations.

## Action Items

| Priority | Action | Owner | Deadline |
|----------|--------|-------|----------|
| P0 | Create composite index on `user_positions` table for (`user_id`, `position_date`) columns | Database Team | 2024-03-16 |
| P0 | Reschedule `user_positions_recalc` batch job from 14:00 UTC to 02:00 UTC (off-peak hours) | Data Engineering | 2024-03-16 |
| P0 | Implement 60-second query timeout for all `user_positions_recalc` batch job queries | Data Engineering | 2024-03-18 |
| P1 | Create dedicated connection pool (30 connections) for batch jobs, isolating `user_positions_recalc` from `pricing-service` production traffic | Database Team | 2024-03-20 |
| P1 | Increase database connection pool for `pricing-service` from 100 to 150 connections | Database Team | 2024-03-20 |
| P1 | Reduce API gateway circuit breaker timeout for `pricing-service` from 5000ms to 2000ms | Platform Team | 2024-03-22 |
| P1 | Add pre-execution query plan analysis to `user_positions_recalc` job to detect full table scans before execution | Data Engineering | 2024-03-25 |
| P2 | Implement automated alerting when database connection pool waiting queue exceeds 20 connections | Observability Team | 2024-03-27 |
| P2 | Audit all scheduled batch jobs (`user_positions_recalc`, similar jobs) for execution during trading hours (09:00-18:00 UTC) | Data Engineering | 2024-03-29 |
| P2 | Review `user_positions` table and all tables accessed by `user_positions_recalc` for missing indexes using query execution patterns | Database Team | 2024-04-05 |

## Recurrence Risk

**HIGH** - This failure pattern has recurred **3 times** in the past 14 months (INC-2023-041, INC-2024-007, and this incident). The organization has a demonstrated pattern of missing_index_batch_job incidents where batch jobs scheduled during production hours cause resource exhaustion. INC-2024-007 involved an identical failure mode (batch job during trading hours causing full table scan), and INC-2023-041 demonstrated the same connection pool exhaustion pattern from analytics jobs without query timeouts.

The P0 action items addressing the immediate `user_positions_recalc` job will prevent this specific incident from recurring, but the P2 audit action item is critical to prevent similar failures in other batch jobs. Without systematic review of batch job scheduling, query timeouts, and index coverage across all scheduled jobs, the organization remains vulnerable to this class of incident recurring in different components.