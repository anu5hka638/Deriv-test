# Post-Mortem: Database Connection Pool Exhaustion from Unindexed Batch Query

**Incident ID:** incident_b  
**Date:** 2024-04-05  
**Duration:** 14 minutes 38 seconds (09:44:22 - 09:59:00 UTC)  
**MTTR:** 13.33 minutes  
**Author:** SRE Team  

## Incident Summary

On April 5, 2024, order submission functionality was completely unavailable for 13 minutes and 20 seconds (09:45:40 - 09:59:00 UTC) due to database connection pool exhaustion. The `open_orders_settlement` batch job, scheduled daily at 09:30 UTC, executed query `q_9031` which performed a 748-second full table scan on the `open_orders` table due to missing composite indexes on `account_id` and `order_date` columns. This long-running query held database connections open, exhausting the 100-connection pool and preventing `order-service` from processing requests. The incident was resolved by forcibly killing query `q_9031`, which immediately released connections and restored service. This represents the third occurrence of the `missing_index_batch_job` failure pattern, matching incidents INC-2024-007 and INC-2023-041.

## Timeline

**2024-04-05 09:44:22 UTC** - Slow database query `q_9031` detected with 1800ms duration in `order-service`. Query originated from `open_orders_settlement` batch job scheduled at 09:30 UTC and began full table scan on `open_orders` table due to missing indexes on `account_id` and `order_date` columns.

**2024-04-05 09:44:55 UTC** - Database connection pool exhausted with all 100 connections occupied and 39 requests waiting. Query `q_9031` held connections open during full table scan, preventing connection recycling and starving other services.

**2024-04-05 09:45:10 UTC** - API gateway experienced upstream timeout when calling `order-service`. Service could not acquire database connections due to pool exhaustion, causing requests to hang until timeout.

**2024-04-05 09:45:28 UTC** - Circuit breaker opened for `order-service` at API gateway. Repeated timeout failures triggered circuit breaker protection, blocking all order-related requests from reaching backend.

**2024-04-05 09:45:40 UTC** - Order submission functionality completely halted. Circuit breaker activation combined with database unavailability resulted in total loss of order submission capability.

**2024-04-05 09:57:14 UTC** - Long-running query `q_9031` forcibly killed after running for 748 seconds on `open_orders` table. Termination immediately released database connections and locks.

**2024-04-05 09:57:15 UTC** - Database connection pool began recovering. Connections released back to pool, allowing 39 waiting requests to be serviced and enabling `order-service` to resume database operations.

**2024-04-05 09:59:00 UTC** - Order submission functionality fully resumed. Database connectivity restored, `order-service` health checks passed, circuit breaker closed, and API gateway resumed forwarding traffic.

**2024-04-05 10:15:00 UTC** - Post-incident analysis identified query `q_9031` originated from daily batch job `open_orders_settlement` scheduled at 09:30 UTC.

**2024-04-05 10:15:01 UTC** - Post-incident analysis confirmed `open_orders` table missing composite index on `account_id` and `order_date` columns, forcing query `q_9031` to perform full table scan requiring 748 seconds instead of milliseconds.

## Root Cause

The `open_orders` table was missing a composite index on `(account_id, order_date)` columns. When the `open_orders_settlement` batch job executed at 09:30 UTC on April 5, 2024, its query `q_9031` was forced to perform a full table scan, taking 748 seconds to complete. This pathological query held database connections open for the entire duration, preventing normal connection recycling. The shared connection pool of 100 connections was exhausted within 33 seconds (by 09:44:55), blocking `order-service` from acquiring connections for production requests. The connection starvation cascaded to complete order submission failure when the API gateway circuit breaker opened at 09:45:28 after repeated timeouts.

## Contributing Factors

1. **Batch job scheduling during business hours**: The `open_orders_settlement` batch job was scheduled at 09:30 UTC, overlapping with production traffic and competing for database resources during peak business hours.

2. **Insufficient connection pool capacity**: The database connection pool size of 100 connections was inadequate to handle concurrent batch operations and production load from `order-service`.

3. **Missing query timeout configuration**: Query `q_9031` from the `open_orders_settlement` batch job had no timeout configured, allowing 748-second execution that monopolized connection pool resources.

4. **Shared connection pool**: The `order-service` and `open_orders_settlement` batch job shared the same connection pool, creating resource contention without isolation between batch and production workloads.

5. **Circuit breaker fail-closed behavior**: The API gateway circuit breaker opening at 09:45:28 eliminated all order submission capability rather than providing degraded service or queue-based fallback.

## Severity Classification

**SEV1**: Complete loss of order submission functionality for 13 minutes and 20 seconds directly impacted core business capability and revenue generation during production hours.

## Action Items

1. **[P0] Create composite index on `open_orders` table**: Add composite index on `(account_id, order_date)` columns to prevent full table scans by `open_orders_settlement` batch job queries. Owner: Database Engineering. ETA: 2024-04-06.

2. **[P0] Reschedule `open_orders_settlement` batch job**: Move execution from 09:30 UTC to 02:00 UTC to avoid overlap with production business hours. Owner: Platform Engineering. ETA: 2024-04-06.

3. **[P0] Implement query timeout for `open_orders_settlement` job**: Configure 60-second maximum query timeout for all queries executed by `open_orders_settlement` batch job to prevent runaway query execution. Owner: Application Engineering. ETA: 2024-04-08.

4. **[P1] Create dedicated connection pool for batch operations**: Establish separate connection pool for `open_orders_settlement` and other batch jobs, isolated from `order-service` production connection pool. Owner: Database Engineering. ETA: 2024-04-12.

5. **[P1] Increase primary connection pool size**: Expand `order-service` database connection pool from 100 to 200 connections to provide buffer capacity during load spikes. Owner: Database Engineering. ETA: 2024-04-10.

6. **[P1] Audit all batch jobs for missing indexes**: Review query execution plans for `open_orders_settlement`, `daily_reconciliation`, and `customer_analytics_refresh` batch jobs to identify additional missing indexes causing full table scans. Owner: Database Engineering. ETA: 2024-04-15.

7. **[P2] Add connection pool saturation alerting**: Implement alerting when database connection pool utilization exceeds 75% (75 of 100 connections) to provide early warning before exhaustion. Owner: SRE. ETA: 2024-04-12.

8. **[P2] Implement query performance monitoring for batch jobs**: Deploy automated detection for batch job queries exceeding 10-second execution time, with automatic alerts to on-call. Owner: SRE. ETA: 2024-04-15.

## Recurrence Risk

**HIGH**: This incident represents the third occurrence of the `missing_index_batch_job` failure pattern in 16 months (INC-2023-041, INC-2024-007, and current incident_b), indicating systemic issues with batch job database optimization and scheduling practices. Previous incident INC-2024-007 involved batch job scheduled during trading hours causing full table scan on `transactions` table, demonstrating identical failure mechanism. Incident INC-2023-041 involved database connection pool exhaustion from analytics job without query timeout, sharing the resource exhaustion pattern. Without comprehensive batch job audit (Action Item #6) and dedicated connection pool isolation (Action Item #4), recurrence probability remains high for any newly deployed batch job or schema change affecting existing batch queries. The pattern recurs because batch jobs continue to be scheduled during production hours against tables without verified index coverage.