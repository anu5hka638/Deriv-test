#!/usr/bin/env python3
"""Replayable AI-powered incident analysis pipeline."""

import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

# ── constants ──────────────────────────────────────────────────────────────────
MODEL = "claude-sonnet-4-5"

STAGES = [
    "INIT",
    "INPUTS_LOADED",
    "LOGS_PARSED",
    "INCIDENT_WINDOWS_IDENTIFIED",
    "TIMELINES_RECONSTRUCTED",
    "ROOT_CAUSES_ANALYSED",
    "POSTMORTEMS_GENERATED",
    "SYSTEMIC_ACTIONS_IDENTIFIED",
    "OPTIONAL_ANALYSES_GENERATED",
    "VALIDATION_COMPLETE",
    "RESULTS_FINALISED",
]

# [YYYY-MM-DD HH:MM:SS UTC] LEVEL  service  message
LOG_LINE_RE = re.compile(
    r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\]\s+"
    r"(INFO|WARN|ERROR|CRIT)\s+(\S+)\s+(.*)"
)

RECOVERY_KEYWORDS = ("resumed", "returning to normal", "CLOSED", "recovering")
POST_INCIDENT_RE  = re.compile(r"post.incident", re.IGNORECASE)

LLM_CALLS_LOG = Path("llm_calls.jsonl")


# ── state machine helpers ──────────────────────────────────────────────────────

def advance(state):
    idx = STAGES.index(state)
    nxt = STAGES[idx + 1]
    print(f"  ✓ [{state}] complete  →  {nxt}")
    return nxt


def require(current, expected):
    if current != expected:
        raise RuntimeError(
            f"Stage order violation: expected {expected}, currently at {current}"
        )


# ── deterministic log parsing ──────────────────────────────────────────────────

def _to_iso(ts_str):
    dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _extract_fields(message):
    """Pull structured fields from a log message with regex only. No LLM."""
    fields = {
        "query_id":        None,
        "duration_ms":     None,
        "duration_seconds": None,
        "table":           None,
        "pool_size":       None,
        "waiting":         None,
        "job_name":        None,
    }

    m = re.search(r"query_id=(\S+)", message)
    if m:
        fields["query_id"] = m.group(1)

    # "query q_XXXX from …" pattern used in post-incident lines
    if fields["query_id"] is None:
        m = re.search(r"\bquery\s+(q_\w+)\b", message, re.IGNORECASE)
        if m:
            fields["query_id"] = m.group(1)

    m = re.search(r"duration=(\d+)ms", message)
    if m:
        fields["duration_ms"] = int(m.group(1))

    m = re.search(r"duration=(\d+)s\b", message)
    if m:
        fields["duration_seconds"] = int(m.group(1))

    m = re.search(r"table=(\S+)", message)
    if m:
        fields["table"] = m.group(1)

    m = re.search(r"pool_size=(\d+)", message)
    if m:
        fields["pool_size"] = int(m.group(1))

    m = re.search(r"waiting=(\d+)", message)
    if m:
        fields["waiting"] = int(m.group(1))

    m = re.search(r"batch job (\S+)", message)
    if m:
        fields["job_name"] = m.group(1)

    return fields


def parse_log_file(path):
    """Parse a .log file into structured records. Deterministic, no LLM."""
    records = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        m = LOG_LINE_RE.match(line)
        if not m:
            print(f"    WARNING: unparseable line {lineno}: {line[:80]}")
            continue
        ts_str, level, service, message = m.groups()
        records.append({
            "timestamp":     _to_iso(ts_str),
            "level":         level,
            "service":       service,
            "message":       message,
            "parsed_fields": _extract_fields(message),
        })
    return records


def compute_metrics(incident_id, records):
    """Compute timing metrics from parsed records. Deterministic, no LLM."""
    first_warn = next((r for r in records if r["level"] == "WARN"), None)
    first_crit = next((r for r in records if r["level"] == "CRIT"), None)

    recovery = None
    if first_crit:
        crit_ts = first_crit["timestamp"]
        candidates = []
        for r in records:
            if r["level"] != "INFO" or r["timestamp"] <= crit_ts:
                continue
            if POST_INCIDENT_RE.search(r["message"]):
                break  # post-incident analysis lines are not recovery events
            if any(kw in r["message"] for kw in RECOVERY_KEYWORDS):
                candidates.append(r)
        # last candidate = most complete recovery signal
        recovery = candidates[-1] if candidates else None

    first_warn_ts = first_warn["timestamp"] if first_warn else None
    first_crit_ts = first_crit["timestamp"] if first_crit else None
    recovery_ts   = recovery["timestamp"]    if recovery   else None

    mttr_minutes = None
    if first_crit_ts and recovery_ts:
        delta = (
            datetime.fromisoformat(recovery_ts)
            - datetime.fromisoformat(first_crit_ts)
        )
        mttr_minutes = round(delta.total_seconds() / 60, 2)

    return {
        "incident_id":                   incident_id,
        "first_warning_timestamp":       first_warn_ts,
        "first_critical_impact_timestamp": first_crit_ts,
        "final_recovery_timestamp":      recovery_ts,
        "incident_window": (
            {"start": first_warn_ts, "end": recovery_ts}
            if first_warn_ts and recovery_ts else None
        ),
        "mttr_minutes": mttr_minutes,
    }


# ── LLM call logger ────────────────────────────────────────────────────────────

def log_llm_call(stage, incident_id, prompt, input_artifacts, output_artifact):
    entry = {
        "stage":            stage,
        "incident_id":      incident_id,
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "provider":         "anthropic",
        "model":            MODEL,
        "prompt_hash":      hashlib.md5(prompt.encode()).hexdigest(),
        "input_artifacts":  input_artifacts,
        "output_artifact":  output_artifact,
    }
    with LLM_CALLS_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ── LLM helpers ───────────────────────────────────────────────────────────────

def extract_json(text):
    """Strip markdown code fences and parse JSON from an LLM response."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = len(lines)
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].strip() == "```":
                end = i
                break
        text = "\n".join(lines[1:end])
    return json.loads(text)


def llm_reconstruct_timeline(client, incident_id, records, metrics):
    """One LLM call per incident. Receives only structured records, not raw logs."""
    prompt = f"""You are an expert SRE analyzing a system incident.
Below are structured parsed log records for {incident_id}, extracted deterministically by a regex parser.
Raw log text is NOT provided — only the structured fields.

STRUCTURED PARSED RECORDS:
{json.dumps(records, indent=2)}

INCIDENT METRICS:
{json.dumps(metrics, indent=2)}

Reconstruct the full causal timeline of this incident.

Return ONLY valid JSON — no markdown, no explanation — with this exact structure:
{{
  "first_symptom": "ISO-8601 timestamp + description of the earliest observable signal",
  "impact_moment": "ISO-8601 timestamp + description of when user-facing impact began",
  "resolution_trigger": "description of the action or event that caused recovery",
  "timeline": [
    {{
      "timestamp": "ISO-8601 timestamp copied exactly from the records",
      "event": "clear description of what happened",
      "causal_significance": "how this event caused or contributed to subsequent events",
      "supporting_log_refs": ["service@ISO-8601-timestamp"]
    }}
  ]
}}

Rules:
- Every timeline entry must reference a real timestamp from the parsed records.
- supporting_log_refs format: "service@timestamp" e.g. "db-primary@2024-03-15T14:10:33+00:00"
- causal_significance must explain the causal chain, not merely describe the event.
- Include every causally significant event across the full incident lifecycle.
- Do not invent events not evidenced by the parsed records."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text
    result = extract_json(raw)

    log_llm_call(
        stage="TIMELINES_RECONSTRUCTED",
        incident_id=incident_id,
        prompt=prompt,
        input_artifacts=[f"parsed_logs/{incident_id}.json", "incident_metrics.json"],
        output_artifact="timelines.json",
    )
    return result


def llm_root_cause_analysis(client, all_timelines, incident_metrics, historical):
    """One combined LLM call: both timelines + metrics + historical incidents."""
    prompt = f"""You are an expert SRE performing root cause analysis across two related system incidents.
You have been given structured causal timelines (reconstructed from parsed log records), incident metrics, and a historical incident database.

INCIDENT A TIMELINE:
{json.dumps(all_timelines['incident_a'], indent=2)}

INCIDENT B TIMELINE:
{json.dumps(all_timelines['incident_b'], indent=2)}

INCIDENT METRICS (both incidents):
{json.dumps(incident_metrics, indent=2)}

HISTORICAL INCIDENT DATABASE:
{json.dumps(historical, indent=2)}

Perform root cause analysis for both incidents and cross-reference the historical database.

Return ONLY valid JSON — no markdown, no explanation — with this exact structure:
{{
  "incident_a": {{
    "incident_id": "incident_a",
    "root_cause": "precise technical root cause statement",
    "root_cause_category": "one of: missing_index, connection_pool_exhaustion, missing_query_timeout, db_latency_cascade, batch_job_scheduling, missing_index_batch_job, other",
    "contributing_factors": ["factor 1", "factor 2"],
    "historical_matches": ["INC-XXXX-XXX"],
    "same_failure_mode_as_other_incident": true,
    "justification": "explanation of why this root cause was identified"
  }},
  "incident_b": {{
    "incident_id": "incident_b",
    "root_cause": "precise technical root cause statement",
    "root_cause_category": "one of: missing_index, connection_pool_exhaustion, missing_query_timeout, db_latency_cascade, batch_job_scheduling, missing_index_batch_job, other",
    "contributing_factors": ["factor 1", "factor 2"],
    "historical_matches": ["INC-XXXX-XXX"],
    "same_failure_mode_as_other_incident": true,
    "justification": "explanation of why this root cause was identified"
  }},
  "same_failure_mode": true,
  "structured_justification": {{
    "shared_pattern": "description of the shared failure pattern",
    "key_differences": "how the incidents differ despite shared pattern",
    "systemic_risk": "what this pattern means for the platform overall"
  }}
}}

Rules:
- historical_matches must reference real IDs from the historical database provided.
- contributing_factors must reference specific named services, tables, jobs, or queries from the timelines.
- same_failure_mode must be a boolean (true/false).
- Be precise — no generic statements."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text
    result = extract_json(raw)

    log_llm_call(
        stage="ROOT_CAUSES_ANALYSED",
        incident_id=None,
        prompt=prompt,
        input_artifacts=["timelines.json", "incident_metrics.json", "historical_incidents.json"],
        output_artifact="root_cause_analysis.json",
    )
    return result


def llm_generate_postmortem(client, incident_id, metrics, timeline, rca_entry, hist_matches):
    """One LLM call per incident. Produces a structured markdown post-mortem."""
    prompt = f"""You are an expert SRE writing a formal post-mortem for a production incident.
You have structured data only — no raw logs.

INCIDENT METRICS:
{json.dumps(metrics, indent=2)}

CAUSAL TIMELINE:
{json.dumps(timeline, indent=2)}

ROOT CAUSE ANALYSIS:
{json.dumps(rca_entry, indent=2)}

HISTORICAL MATCH CONTEXT:
{json.dumps(hist_matches, indent=2)}

Write a complete post-mortem in markdown. You MUST include ALL of these sections with EXACTLY these headings:

## Incident Summary
## Timeline
## Root Cause
## Contributing Factors
## Severity Classification
## Action Items
## Recurrence Risk

STRICT REQUIREMENTS:
- Severity Classification must be one of SEV1, SEV2, or SEV3 with a one-sentence justification.
- Action Items must reference SPECIFIC named components from the data: exact service names, table names, job names, and query IDs. No generic items like "improve monitoring" — every item must name the exact component.
- Timeline section must list events with timestamps drawn from the causal timeline provided.
- Contributing Factors must name the specific services and components involved.
- Recurrence Risk must reference the historical matches and quantify how many times this pattern has recurred.

Return only the markdown document. No preamble, no explanation."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()

    suffix = "a" if incident_id == "incident_a" else "b"
    out_path = Path(f"postmortem_{suffix}.md")
    out_path.write_text(text, encoding="utf-8")

    log_llm_call(
        stage="POSTMORTEMS_GENERATED",
        incident_id=incident_id,
        prompt=prompt,
        input_artifacts=[
            f"parsed_logs/{incident_id}.json",
            "incident_metrics.json",
            "timelines.json",
            "root_cause_analysis.json",
            "historical_incidents.json",
        ],
        output_artifact=str(out_path),
    )
    return out_path


# ── systemic actions (deterministic) ──────────────────────────────────────────

_STOPWORDS = frozenset({
    "the", "a", "an", "for", "to", "of", "on", "in", "and", "or", "with",
    "is", "are", "be", "this", "that", "should", "must", "add", "create",
    "ensure", "implement", "all", "any", "not", "by", "from", "as", "at",
    "it", "its", "if", "when", "no", "new", "use", "set",
})


def extract_action_items(md_text):
    """Extract action items from ## Action Items section — handles bullets and tables."""
    lines = md_text.splitlines()
    in_section = False
    items = []
    table_action_col = None  # column index for "Action" in a markdown table

    for line in lines:
        if re.match(r"^##\s+Action Items", line, re.IGNORECASE):
            in_section = True
            table_action_col = None
            continue
        if not in_section:
            continue
        if re.match(r"^##\s+", line):
            break

        stripped = line.strip()

        # Markdown table row
        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            # Header row: find which column is "Action"
            if table_action_col is None:
                headers = [c.lower() for c in cells]
                if "action" in headers:
                    table_action_col = headers.index("action")
                continue  # skip header and separator rows
            # Separator row (---|---|---)
            if all(re.match(r"^[-:]+$", c) for c in cells if c):
                continue
            # Data row
            if table_action_col is not None and table_action_col < len(cells):
                action = re.sub(r"`[^`]+`", lambda m: m.group(0), cells[table_action_col])
                action = action.strip()
                if action and action.lower() not in ("action", ""):
                    items.append(action)
            continue

        # Bullet point row
        m = re.match(r"^[-*\d.•]\s*\*{0,2}(.+?)\*{0,2}$", stripped)
        if m:
            items.append(m.group(1).strip())

    return items


def _keywords(text):
    words = re.findall(r"[a-z0-9_/-]+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _jaccard(a, b):
    ka, kb = _keywords(a), _keywords(b)
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / len(ka | kb)


def find_systemic_pairs(items_a, items_b, threshold=0.22):
    """Greedy best-match pairing of action items by keyword overlap."""
    pairs = []
    used = set()
    for a in items_a:
        best_j, best_sim = -1, threshold
        for j, b in enumerate(items_b):
            if j in used:
                continue
            sim = _jaccard(a, b)
            if sim > best_sim:
                best_j, best_sim = j, sim
        if best_j >= 0:
            pairs.append((a, items_b[best_j], best_sim))
            used.add(best_j)
    return pairs


def _infer_owner(text):
    t = text.lower()
    if any(k in t for k in ("index", "table", "query", "full table scan")):
        return "Database Engineering"
    if any(k in t for k in ("connection pool", "pool_size", "pool")):
        return "Database / Platform Engineering"
    if any(k in t for k in ("batch job", "schedule", "cron", "settlement", "recalc")):
        return "Data Engineering"
    if any(k in t for k in ("circuit breaker", "timeout", "upstream")):
        return "Platform Engineering"
    if any(k in t for k in ("alert", "monitor", "observability", "pagerduty")):
        return "SRE / Observability"
    return "Platform Engineering"


def _infer_priority(text):
    t = text.lower()
    if any(k in t for k in ("index", "connection pool", "query timeout", "batch job", "schedule")):
        return "HIGH"
    if any(k in t for k in ("alert", "monitor", "circuit breaker", "timeout")):
        return "MEDIUM"
    return "LOW"


def _infer_verification(text):
    t = text.lower()
    if "index" in t:
        return "EXPLAIN plan shows index seek (not full table scan) for affected queries"
    if "connection pool" in t or "pool_size" in t:
        return "Pool utilization stays below 80% during batch job execution windows"
    if "schedule" in t or "batch job" in t or "trading hours" in t:
        return "Zero incidents triggered during batch execution windows over 30-day observation"
    if "timeout" in t:
        return "No query exceeds timeout threshold under simulated batch load"
    if "alert" in t or "monitor" in t:
        return "Alert fires in staging before critical threshold is reached"
    return "Load test with concurrent batch job confirms no service degradation"


def format_systemic_actions(pairs, rca):
    shared_pattern = (
        rca.get("structured_justification", {}).get("shared_pattern", "")
        or "Unindexed batch job full-table-scan exhausting connection pool"
    )

    lines = [
        "# Systemic Action Items",
        "",
        "_Identified by deterministic cross-comparison of `postmortem_a.md` and `postmortem_b.md`_",
        "",
        f"**Shared Failure Pattern:** {shared_pattern}",
        "",
        "---",
        "",
    ]

    for idx, (a_item, b_item, sim) in enumerate(pairs, 1):
        combined = f"{a_item} {b_item}"
        lines += [
            f"## Systemic Action {idx}",
            "",
            f"| Field | Detail |",
            f"|-------|--------|",
            f"| **Incidents Affected** | incident_a, incident_b |",
            f"| **Shared Failure Pattern** | {shared_pattern} |",
            f"| **Recommended Owner** | {_infer_owner(combined)} |",
            f"| **Implementation Priority** | {_infer_priority(combined)} |",
            f"| **Verification Method** | {_infer_verification(combined)} |",
            f"| **Similarity Score** | {sim:.2f} (keyword overlap) |",
            "",
            f"**Incident A action:** {a_item}",
            "",
            f"**Incident B action:** {b_item}",
            "",
            "---",
            "",
        ]

    if not pairs:
        lines.append("_No systemic actions identified (no overlapping action items)._\n")

    return "\n".join(lines)


# ── optional analyses (LLM) ───────────────────────────────────────────────────

def compute_mttr_stats(incident_metrics, historical):
    """Deterministic MTTR stats — no LLM."""
    hist_list = historical.get("historical_incidents", [])
    hist_entries = [
        {"id": h["id"], "mttr_minutes": h["mttr_minutes"]}
        for h in hist_list if "mttr_minutes" in h
    ]
    hist_vals = [e["mttr_minutes"] for e in hist_entries]
    a = incident_metrics["incident_a"]["mttr_minutes"]
    b = incident_metrics["incident_b"]["mttr_minutes"]
    avg = round(sum(hist_vals) / len(hist_vals), 2) if hist_vals else None
    return {
        "incident_a_mttr_minutes": a,
        "incident_b_mttr_minutes": b,
        "historical_incidents": hist_entries,
        "historical_avg_mttr_minutes": avg,
        "historical_min_mttr_minutes": min(hist_vals) if hist_vals else None,
        "historical_max_mttr_minutes": max(hist_vals) if hist_vals else None,
        "incident_a_delta_vs_avg": round(a - avg, 2) if avg else None,
        "incident_b_delta_vs_avg": round(b - avg, 2) if avg else None,
    }


def llm_mttr_analysis(client, mttr_stats, rca):
    prompt = f"""You are an expert SRE analyzing MTTR (Mean Time To Recovery) across incidents.

MTTR STATISTICS (computed deterministically from parsed log timestamps):
{json.dumps(mttr_stats, indent=2)}

ROOT CAUSE ANALYSIS CONTEXT:
{json.dumps(rca.get('structured_justification', {}), indent=2)}

Write an MTTR analysis in markdown covering:
1. How each incident's MTTR compares to the historical baseline.
2. Why incident_b resolved faster than incident_a (reference specific timeline differences).
3. What the historical trend says about this failure mode's detection and resolution speed.
4. Concrete recommendations to reduce MTTR for this failure mode.

Use headers. Be concise and technical. Reference the actual numbers."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    log_llm_call(
        stage="OPTIONAL_ANALYSES_GENERATED",
        incident_id=None,
        prompt=prompt,
        input_artifacts=["incident_metrics.json", "root_cause_analysis.json",
                         "historical_incidents.json"],
        output_artifact="mttr_analysis.md",
    )
    return text


def llm_communications(client, incident_metrics, rca, systemic_md):
    prompt = f"""You are an SRE communications lead. Two production incidents have been resolved.
Write two separate communication pieces based on the structured data below.

INCIDENT METRICS:
{json.dumps(incident_metrics, indent=2)}

ROOT CAUSE SUMMARY:
{json.dumps({k: v for k, v in rca.items() if k != 'structured_justification'}, indent=2)}

SYSTEMIC ACTIONS SUMMARY:
{systemic_md[:1500]}

Return ONLY valid JSON — no markdown wrapper — with this structure:
{{
  "user_facing_update": "Present-tense status update for end users. Plain English, no jargon, no technical terms. 3-4 sentences max. Both incidents resolved.",
  "engineering_leadership_summary": "Past-tense technical summary for engineering leadership. Include: incident dates, affected services, root cause category, MTTR for each incident, number of systemic actions identified, historical recurrence context. 150-200 words."
}}"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text
    result = extract_json(raw)
    log_llm_call(
        stage="OPTIONAL_ANALYSES_GENERATED",
        incident_id=None,
        prompt=prompt,
        input_artifacts=["incident_metrics.json", "root_cause_analysis.json",
                         "systemic_actions.md"],
        output_artifact="communications.md",
    )
    return result


def llm_failure_mode_taxonomy(client, rca, historical):
    prompt = f"""You are an expert SRE building a failure mode taxonomy for a trading platform.

RECENT ROOT CAUSE ANALYSIS:
{json.dumps(rca, indent=2)}

HISTORICAL INCIDENT DATABASE:
{json.dumps(historical, indent=2)}

Create a failure mode taxonomy with at least 5 categories relevant to database-backed trading platforms.

Return ONLY valid JSON — no markdown — with this structure:
{{
  "taxonomy": [
    {{
      "name": "short category name",
      "description": "what this failure mode is",
      "example_incident": "reference to a real incident ID from the data, or a description",
      "early_warning_signals": ["signal 1", "signal 2", "signal 3"]
    }}
  ]
}}

Requirements:
- At least 5 categories.
- early_warning_signals must be specific and observable (e.g. metrics, log patterns, thresholds).
- example_incident must reference real incident IDs where possible."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text
    result = extract_json(raw)
    log_llm_call(
        stage="OPTIONAL_ANALYSES_GENERATED",
        incident_id=None,
        prompt=prompt,
        input_artifacts=["root_cause_analysis.json", "historical_incidents.json"],
        output_artifact="failure_mode_taxonomy.json",
    )
    return result


def llm_predictive_signals(client, all_timelines, incident_metrics, rca):
    prompt = f"""You are an expert SRE designing proactive monitoring rules.

INCIDENT TIMELINES:
{json.dumps(all_timelines, indent=2)}

INCIDENT METRICS:
{json.dumps(incident_metrics, indent=2)}

ROOT CAUSE ANALYSIS:
{json.dumps(rca.get('structured_justification', {}), indent=2)}

For each incident, identify:
1. The earliest observable signal (before the critical impact).
2. A monitoring rule that would fire at least 5 minutes before the critical event.

Return ONLY valid JSON — no markdown — with this structure:
{{
  "incident_a": {{
    "critical_event_timestamp": "ISO-8601",
    "earliest_observable_signal": {{
      "timestamp": "ISO-8601",
      "signal": "description of the signal",
      "minutes_before_critical": 0.0,
      "log_ref": "service@timestamp"
    }},
    "monitoring_rule": {{
      "rule_name": "short rule name",
      "condition": "exact metric/log condition to detect",
      "threshold": "specific threshold value",
      "fires_at_timestamp": "ISO-8601 (must be 5+ min before critical)",
      "minutes_before_critical": 0.0,
      "alert_action": "what to do when it fires"
    }}
  }},
  "incident_b": {{
    "critical_event_timestamp": "ISO-8601",
    "earliest_observable_signal": {{
      "timestamp": "ISO-8601",
      "signal": "description",
      "minutes_before_critical": 0.0,
      "log_ref": "service@timestamp"
    }},
    "monitoring_rule": {{
      "rule_name": "short rule name",
      "condition": "exact metric/log condition",
      "threshold": "specific threshold",
      "fires_at_timestamp": "ISO-8601 (must be 5+ min before critical)",
      "minutes_before_critical": 0.0,
      "alert_action": "what to do when it fires"
    }}
  }}
}}

Rules:
- fires_at_timestamp must be at least 5 minutes before the critical_event_timestamp.
- All timestamps must be real values from the timeline data.
- conditions must be specific and automatable."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text
    result = extract_json(raw)
    log_llm_call(
        stage="OPTIONAL_ANALYSES_GENERATED",
        incident_id=None,
        prompt=prompt,
        input_artifacts=["timelines.json", "incident_metrics.json",
                         "root_cause_analysis.json"],
        output_artifact="predictive_signals.json",
    )
    return result


# ── pipeline ───────────────────────────────────────────────────────────────────

def main():
    state = "INIT"
    print(f"\n{'='*60}")
    print("  INCIDENT ANALYSIS PIPELINE")
    print(f"{'='*60}\n")

    # ── INIT ──────────────────────────────────────────────────────────────────
    print(f"[{state}] Preparing environment and output directories...")
    load_dotenv(override=True)
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ERROR: ANTHROPIC_API_KEY not set in .env")

    Path("parsed_logs").mkdir(exist_ok=True)
    LLM_CALLS_LOG.unlink(missing_ok=True)  # fresh run clears prior call log

    state = advance(state)

    # ── INPUTS_LOADED ─────────────────────────────────────────────────────────
    require(state, "INPUTS_LOADED")
    print(f"\n[{state}] Verifying and reading input files...")

    log_a_path  = Path("incident_a.log")
    log_b_path  = Path("incident_b.log")
    hist_path   = Path("historical_incidents.json")

    for p in (log_a_path, log_b_path, hist_path):
        if not p.exists():
            sys.exit(f"ERROR: required input file missing: {p}")
        print(f"    ✓ {p}")

    historical = json.loads(hist_path.read_text(encoding="utf-8"))
    state = advance(state)

    # ── LOGS_PARSED ───────────────────────────────────────────────────────────
    require(state, "LOGS_PARSED")
    print(f"\n[{state}] Parsing logs with regex (no LLM)...")

    records_a = parse_log_file(log_a_path)
    records_b = parse_log_file(log_b_path)
    print(f"    incident_a → {len(records_a)} records")
    print(f"    incident_b → {len(records_b)} records")

    out_a = Path("parsed_logs/incident_a.json")
    out_b = Path("parsed_logs/incident_b.json")
    out_a.write_text(json.dumps(records_a, indent=2), encoding="utf-8")
    out_b.write_text(json.dumps(records_b, indent=2), encoding="utf-8")
    print(f"    saved {out_a}")
    print(f"    saved {out_b}")

    state = advance(state)

    # ── INCIDENT_WINDOWS_IDENTIFIED ───────────────────────────────────────────
    require(state, "INCIDENT_WINDOWS_IDENTIFIED")
    print(f"\n[{state}] Computing incident metrics deterministically...")

    metrics_a = compute_metrics("incident_a", records_a)
    metrics_b = compute_metrics("incident_b", records_b)

    for m in (metrics_a, metrics_b):
        print(f"    {m['incident_id']}")
        print(f"      first_warning : {m['first_warning_timestamp']}")
        print(f"      first_critical: {m['first_critical_impact_timestamp']}")
        print(f"      recovery      : {m['final_recovery_timestamp']}")
        print(f"      MTTR          : {m['mttr_minutes']} minutes")

    incident_metrics = {"incident_a": metrics_a, "incident_b": metrics_b}
    metrics_path = Path("incident_metrics.json")
    metrics_path.write_text(json.dumps(incident_metrics, indent=2), encoding="utf-8")
    print(f"    saved {metrics_path}")

    state = advance(state)

    # ── TIMELINES_RECONSTRUCTED ───────────────────────────────────────────────
    require(state, "TIMELINES_RECONSTRUCTED")
    print(f"\n[{state}] Reconstructing causal timelines via LLM (2 calls)...")

    client = anthropic.Anthropic(api_key=api_key)
    all_timelines = {}

    for incident_id, records, metrics in [
        ("incident_a", records_a, metrics_a),
        ("incident_b", records_b, metrics_b),
    ]:
        print(f"    [{incident_id}] calling LLM...")
        timeline = llm_reconstruct_timeline(client, incident_id, records, metrics)
        all_timelines[incident_id] = timeline
        n = len(timeline.get("timeline", []))
        print(f"    [{incident_id}] {n} timeline entries  "
              f"| first_symptom: {timeline.get('first_symptom', '')[:60]}")

    timelines_path = Path("timelines.json")
    timelines_path.write_text(json.dumps(all_timelines, indent=2), encoding="utf-8")
    print(f"    saved {timelines_path}")

    state = advance(state)

    # ── ROOT_CAUSES_ANALYSED ──────────────────────────────────────────────────
    require(state, "ROOT_CAUSES_ANALYSED")
    print(f"\n[{state}] Root cause analysis via LLM (1 combined call)...")
    print(f"    sending: both timelines + incident_metrics + historical_incidents")

    rca = llm_root_cause_analysis(client, all_timelines, incident_metrics, historical)

    for inc in ("incident_a", "incident_b"):
        d = rca.get(inc, {})
        print(f"    [{inc}] root_cause_category: {d.get('root_cause_category', '?')}")
        print(f"    [{inc}] historical_matches  : {d.get('historical_matches', [])}")

    print(f"    same_failure_mode: {rca.get('same_failure_mode')}")

    rca_path = Path("root_cause_analysis.json")
    rca_path.write_text(json.dumps(rca, indent=2), encoding="utf-8")
    print(f"    saved {rca_path}")

    state = advance(state)

    # ── POSTMORTEMS_GENERATED ─────────────────────────────────────────────────
    require(state, "POSTMORTEMS_GENERATED")
    print(f"\n[{state}] Generating post-mortems via LLM (2 calls)...")

    # Build a lookup for historical incidents by ID
    hist_lookup = {h["id"]: h for h in historical.get("historical_incidents", [])}

    for incident_id, metrics, timeline, records in [
        ("incident_a", metrics_a, all_timelines["incident_a"], records_a),
        ("incident_b", metrics_b, all_timelines["incident_b"], records_b),
    ]:
        rca_entry = rca.get(incident_id, {})
        matched_ids = rca_entry.get("historical_matches", [])
        hist_matches = [hist_lookup[i] for i in matched_ids if i in hist_lookup]

        print(f"    [{incident_id}] calling LLM...")
        out_path = llm_generate_postmortem(
            client, incident_id, metrics, timeline, rca_entry, hist_matches
        )
        print(f"    [{incident_id}] saved {out_path}")

    state = advance(state)

    # ── SYSTEMIC_ACTIONS_IDENTIFIED ───────────────────────────────────────────
    require(state, "SYSTEMIC_ACTIONS_IDENTIFIED")
    print(f"\n[{state}] Comparing action items deterministically...")

    pm_a_text = Path("postmortem_a.md").read_text(encoding="utf-8")
    pm_b_text = Path("postmortem_b.md").read_text(encoding="utf-8")

    items_a = extract_action_items(pm_a_text)
    items_b = extract_action_items(pm_b_text)
    print(f"    postmortem_a: {len(items_a)} action items extracted")
    print(f"    postmortem_b: {len(items_b)} action items extracted")

    pairs = find_systemic_pairs(items_a, items_b)
    print(f"    {len(pairs)} systemic action(s) identified by keyword overlap")

    systemic_md = format_systemic_actions(pairs, rca)
    sys_path = Path("systemic_actions.md")
    sys_path.write_text(systemic_md, encoding="utf-8")
    print(f"    saved {sys_path}")

    state = advance(state)

    # ── OPTIONAL_ANALYSES_GENERATED ───────────────────────────────────────────
    require(state, "OPTIONAL_ANALYSES_GENERATED")
    print(f"\n[{state}] Running optional analyses (4 LLM calls)...")

    # MTTR analysis — deterministic compute then LLM explanation
    print(f"    [mttr] computing stats deterministically...")
    mttr_stats = compute_mttr_stats(incident_metrics, historical)
    print(f"    [mttr] calling LLM for variance explanation...")
    mttr_md = llm_mttr_analysis(client, mttr_stats, rca)
    mttr_header = (
        f"# MTTR Analysis\n\n"
        f"| Metric | Value |\n|--------|-------|\n"
        f"| Incident A MTTR | {mttr_stats['incident_a_mttr_minutes']} min |\n"
        f"| Incident B MTTR | {mttr_stats['incident_b_mttr_minutes']} min |\n"
        f"| Historical Avg  | {mttr_stats['historical_avg_mttr_minutes']} min |\n"
        f"| Historical Min  | {mttr_stats['historical_min_mttr_minutes']} min |\n"
        f"| Historical Max  | {mttr_stats['historical_max_mttr_minutes']} min |\n\n"
        f"---\n\n"
    )
    Path("mttr_analysis.md").write_text(mttr_header + mttr_md, encoding="utf-8")
    print(f"    saved mttr_analysis.md")

    # Communications
    print(f"    [comms] calling LLM...")
    comms = llm_communications(client, incident_metrics, rca, systemic_md)
    comms_md = (
        "# Communications\n\n"
        "## User-Facing Status Update\n\n"
        f"{comms.get('user_facing_update', '')}\n\n"
        "## Engineering Leadership Summary\n\n"
        f"{comms.get('engineering_leadership_summary', '')}\n"
    )
    Path("communications.md").write_text(comms_md, encoding="utf-8")
    print(f"    saved communications.md")

    # Failure mode taxonomy (stretch)
    print(f"    [taxonomy] calling LLM...")
    taxonomy = llm_failure_mode_taxonomy(client, rca, historical)
    Path("failure_mode_taxonomy.json").write_text(
        json.dumps(taxonomy, indent=2), encoding="utf-8"
    )
    n_cats = len(taxonomy.get("taxonomy", []))
    print(f"    saved failure_mode_taxonomy.json  ({n_cats} categories)")

    # Predictive signals (stretch)
    print(f"    [signals] calling LLM...")
    signals = llm_predictive_signals(client, all_timelines, incident_metrics, rca)
    Path("predictive_signals.json").write_text(
        json.dumps(signals, indent=2), encoding="utf-8"
    )
    for inc in ("incident_a", "incident_b"):
        rule = signals.get(inc, {}).get("monitoring_rule", {})
        print(f"    [{inc}] rule: {rule.get('rule_name', '?')}  "
              f"| fires {rule.get('minutes_before_critical', '?')} min before CRIT")

    state = advance(state)

    # ── VALIDATION_COMPLETE ───────────────────────────────────────────────────
    require(state, "VALIDATION_COMPLETE")
    print(f"\n[{state}] Checking all required artifacts exist...")

    required = [
        "parsed_logs/incident_a.json", "parsed_logs/incident_b.json",
        "incident_metrics.json", "timelines.json", "root_cause_analysis.json",
        "postmortem_a.md", "postmortem_b.md", "systemic_actions.md",
        "llm_calls.jsonl", "mttr_analysis.md", "communications.md",
        "failure_mode_taxonomy.json", "predictive_signals.json",
    ]
    missing = [p for p in required if not Path(p).exists()]
    if missing:
        print(f"    WARNING: missing artifacts: {missing}")
    else:
        print(f"    ✓ all {len(required)} required artifacts present")

    llm_call_count = sum(1 for _ in LLM_CALLS_LOG.open(encoding="utf-8"))
    print(f"    ✓ {llm_call_count} LLM calls logged in llm_calls.jsonl")

    state = advance(state)

    # ── RESULTS_FINALISED ─────────────────────────────────────────────────────
    require(state, "RESULTS_FINALISED")
    state = advance(state)

    print(f"\n{'='*60}")
    print(f"  PIPELINE COMPLETE  |  final state: {state}")
    print(f"  All {len(required)} artifacts written.")
    print(f"  {llm_call_count} LLM calls logged.")
    print(f"  Run validate.py to verify structural correctness.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
