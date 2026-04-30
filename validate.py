#!/usr/bin/env python3
"""Validate all pipeline artifacts for structural correctness."""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

PASS  = "PASS"
FAIL  = "FAIL"
WARN  = "WARN"

results: list[tuple[str, str, str]] = []  # (status, check, detail)


def check(status, name, detail=""):
    results.append((status, name, detail))
    icon = {"PASS": "✓", "FAIL": "✗", "WARN": "⚠"}.get(status, "?")
    print(f"  {icon} [{status}] {name}" + (f": {detail}" if detail else ""))


# ── required artifacts ─────────────────────────────────────────────────────────

REQUIRED = [
    "parsed_logs/incident_a.json",
    "parsed_logs/incident_b.json",
    "incident_metrics.json",
    "timelines.json",
    "root_cause_analysis.json",
    "postmortem_a.md",
    "postmortem_b.md",
    "systemic_actions.md",
    "llm_calls.jsonl",
    "mttr_analysis.md",
    "communications.md",
    "failure_mode_taxonomy.json",
    "predictive_signals.json",
]

PARSED_RECORD_FIELDS = {"timestamp", "level", "service", "message", "parsed_fields"}
PARSED_FIELD_KEYS    = {"query_id", "duration_ms", "duration_seconds",
                        "table", "pool_size", "waiting", "job_name"}
POSTMORTEM_SECTIONS  = [
    "Incident Summary", "Timeline", "Root Cause",
    "Contributing Factors", "Severity Classification",
    "Action Items", "Recurrence Risk",
]
SEVERITY_RE = re.compile(r"\bSEV[123]\b")

LLM_STAGES = {
    "TIMELINES_RECONSTRUCTED":  2,
    "ROOT_CAUSES_ANALYSED":     1,
    "POSTMORTEMS_GENERATED":    2,
    "OPTIONAL_ANALYSES_GENERATED": 4,
}


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_jsonl(path):
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(l) for l in lines if l.strip()]


# ── 1. artifact existence ──────────────────────────────────────────────────────

print("\n── 1. Required Artifacts ─────────────────────────────────────────────────")
for p in REQUIRED:
    if Path(p).exists():
        check(PASS, f"exists: {p}")
    else:
        check(FAIL, f"exists: {p}", "FILE MISSING")


# ── 2. JSON validity ───────────────────────────────────────────────────────────

print("\n── 2. JSON Validity ──────────────────────────────────────────────────────")
JSON_FILES = [p for p in REQUIRED if p.endswith(".json")]
valid_json = {}
for p in JSON_FILES:
    if not Path(p).exists():
        check(FAIL, f"json: {p}", "missing")
        continue
    try:
        valid_json[p] = load_json(p)
        check(PASS, f"json: {p}")
    except json.JSONDecodeError as e:
        check(FAIL, f"json: {p}", str(e))

# validate llm_calls.jsonl separately
if Path("llm_calls.jsonl").exists():
    try:
        llm_calls = load_jsonl("llm_calls.jsonl")
        check(PASS, "jsonl: llm_calls.jsonl", f"{len(llm_calls)} entries")
    except Exception as e:
        llm_calls = []
        check(FAIL, "jsonl: llm_calls.jsonl", str(e))
else:
    llm_calls = []
    check(FAIL, "jsonl: llm_calls.jsonl", "missing")


# ── 3. parsing happened before LLM calls ──────────────────────────────────────

print("\n── 3. Stage Ordering ─────────────────────────────────────────────────────")
if llm_calls:
    # All LLM calls must be in stages that follow LOGS_PARSED
    no_parse_stages = {"LOGS_PARSED", "INCIDENT_WINDOWS_IDENTIFIED",
                       "INIT", "INPUTS_LOADED"}
    bad = [c for c in llm_calls if c.get("stage") in no_parse_stages]
    if bad:
        check(FAIL, "no LLM calls during parse stages",
              f"found calls in: {[b['stage'] for b in bad]}")
    else:
        check(PASS, "no LLM calls during parse stages")

    # llm_calls timestamps must be monotonically non-decreasing
    ts_list = [c.get("timestamp", "") for c in llm_calls]
    ordered = all(ts_list[i] <= ts_list[i+1] for i in range(len(ts_list)-1))
    if ordered:
        check(PASS, "LLM call timestamps are ordered")
    else:
        check(FAIL, "LLM call timestamps are ordered", "timestamps out of order")

    # Parsed logs files must be referenced before timelines in input_artifacts
    parse_stage_calls  = [c for c in llm_calls if c["stage"] == "TIMELINES_RECONSTRUCTED"]
    all_use_parsed = all(
        any("parsed_logs" in a for a in c.get("input_artifacts", []))
        for c in parse_stage_calls
    )
    if all_use_parsed:
        check(PASS, "timeline LLM calls reference parsed_logs as input")
    else:
        check(WARN, "timeline LLM calls reference parsed_logs as input",
              "some calls missing parsed_logs artifact")
else:
    check(WARN, "stage ordering", "no LLM calls to inspect")


# ── 4. parsed records have all required fields ─────────────────────────────────

print("\n── 4. Parsed Record Fields ───────────────────────────────────────────────")
for inc in ("incident_a", "incident_b"):
    path = f"parsed_logs/{inc}.json"
    if path not in valid_json:
        check(FAIL, f"{inc} record fields", "file invalid or missing")
        continue
    records = valid_json[path]
    if not records:
        check(FAIL, f"{inc} record fields", "empty records list")
        continue
    field_errors = []
    for i, r in enumerate(records):
        missing_top = PARSED_RECORD_FIELDS - set(r.keys())
        if missing_top:
            field_errors.append(f"record[{i}] missing: {missing_top}")
        pf = r.get("parsed_fields", {})
        missing_pf = PARSED_FIELD_KEYS - set(pf.keys())
        if missing_pf:
            field_errors.append(f"record[{i}].parsed_fields missing: {missing_pf}")
    if field_errors:
        check(FAIL, f"{inc} record fields", "; ".join(field_errors[:3]))
    else:
        check(PASS, f"{inc} record fields",
              f"{len(records)} records, all fields present")


# ── 5. MTTR computed deterministically ────────────────────────────────────────

print("\n── 5. MTTR Determinism ───────────────────────────────────────────────────")
RECOVERY_KEYWORDS  = ("resumed", "returning to normal", "CLOSED", "recovering")
POST_INCIDENT_RE   = re.compile(r"post.incident", re.IGNORECASE)

def recompute_mttr(records):
    first_crit = next((r for r in records if r["level"] == "CRIT"), None)
    if not first_crit:
        return None
    crit_ts = first_crit["timestamp"]
    candidates = []
    for r in records:
        if r["level"] != "INFO" or r["timestamp"] <= crit_ts:
            continue
        if POST_INCIDENT_RE.search(r["message"]):
            break
        if any(k in r["message"] for k in RECOVERY_KEYWORDS):
            candidates.append(r)
    recovery = candidates[-1] if candidates else None
    if not recovery:
        return None
    delta = (datetime.fromisoformat(recovery["timestamp"])
             - datetime.fromisoformat(crit_ts))
    return round(delta.total_seconds() / 60, 2)

if "incident_metrics.json" in valid_json:
    metrics = valid_json["incident_metrics.json"]
    for inc in ("incident_a", "incident_b"):
        rec_path = f"parsed_logs/{inc}.json"
        if rec_path not in valid_json:
            check(FAIL, f"{inc} MTTR determinism", "parsed records unavailable")
            continue
        expected = recompute_mttr(valid_json[rec_path])
        actual   = metrics.get(inc, {}).get("mttr_minutes")
        if expected is None:
            check(WARN, f"{inc} MTTR determinism", "could not recompute")
        elif abs((expected or 0) - (actual or 0)) < 0.1:
            check(PASS, f"{inc} MTTR determinism",
                  f"stored={actual} recomputed={expected}")
        else:
            check(FAIL, f"{inc} MTTR determinism",
                  f"stored={actual} recomputed={expected}")
else:
    check(FAIL, "MTTR determinism", "incident_metrics.json invalid")


# ── 6. timelines used parsed records ──────────────────────────────────────────

print("\n── 6. Timelines Reference Parsed Records ─────────────────────────────────")
if "timelines.json" in valid_json:
    timelines = valid_json["timelines.json"]
    for inc in ("incident_a", "incident_b"):
        rec_path = f"parsed_logs/{inc}.json"
        if rec_path not in valid_json or inc not in timelines:
            check(WARN, f"{inc} timeline sources", "data unavailable")
            continue
        parsed_ts = {r["timestamp"] for r in valid_json[rec_path]}
        tl_entries = timelines[inc].get("timeline", [])
        matched = sum(1 for e in tl_entries if e.get("timestamp") in parsed_ts)
        total   = len(tl_entries)
        ratio   = matched / total if total else 0
        if ratio >= 0.8:
            check(PASS, f"{inc} timeline timestamps from parsed records",
                  f"{matched}/{total} match")
        elif ratio >= 0.5:
            check(WARN, f"{inc} timeline timestamps from parsed records",
                  f"only {matched}/{total} match")
        else:
            check(FAIL, f"{inc} timeline timestamps from parsed records",
                  f"only {matched}/{total} match — timelines may use raw logs")
else:
    check(FAIL, "timelines reference check", "timelines.json invalid")


# ── 7. root cause used both timelines + historical incidents ───────────────────

print("\n── 7. Root Cause Analysis Inputs ─────────────────────────────────────────")
if "root_cause_analysis.json" in valid_json and "historical_incidents.json" in valid_json:
    rca  = valid_json["root_cause_analysis.json"]
    hist = valid_json["historical_incidents.json"]
    hist_ids = {h["id"] for h in hist.get("historical_incidents", [])}

    for inc in ("incident_a", "incident_b"):
        entry = rca.get(inc, {})
        if not entry:
            check(FAIL, f"{inc} in root_cause_analysis", "missing")
            continue
        matches = entry.get("historical_matches", [])
        valid_matches = [m for m in matches if m in hist_ids]
        if valid_matches:
            check(PASS, f"{inc} references valid historical IDs",
                  str(valid_matches))
        else:
            check(FAIL, f"{inc} references valid historical IDs",
                  f"got {matches}, valid={list(hist_ids)}")

    has_both = "incident_a" in rca and "incident_b" in rca
    check(PASS if has_both else FAIL, "root cause covers both incidents")
    has_top  = "same_failure_mode" in rca and "structured_justification" in rca
    check(PASS if has_top else FAIL, "top-level same_failure_mode + structured_justification")
else:
    check(FAIL, "root cause inputs check", "required JSON files missing/invalid")


# ── 8. post-mortems have all required sections ─────────────────────────────────

print("\n── 8. Post-Mortem Sections ───────────────────────────────────────────────")
for suffix, path in (("a", "postmortem_a.md"), ("b", "postmortem_b.md")):
    if not Path(path).exists():
        check(FAIL, f"{path} sections", "file missing")
        continue
    text = Path(path).read_text(encoding="utf-8")
    headings = re.findall(r"^##\s+(.+)", text, re.MULTILINE)
    for section in POSTMORTEM_SECTIONS:
        found = any(section.lower() in h.lower() for h in headings)
        check(PASS if found else FAIL, f"{path} has '## {section}'")
    if not SEVERITY_RE.search(text):
        check(FAIL, f"{path} Severity Classification contains SEV1/SEV2/SEV3")
    else:
        sev = SEVERITY_RE.search(text).group(0)
        check(PASS, f"{path} Severity Classification", sev)


# ── 9. action items reference specific named components ───────────────────────

print("\n── 9. Action Items Reference Specific Components ─────────────────────────")
KNOWN_COMPONENTS = {
    # incident_a
    "pricing-service", "user_positions", "user_positions_recalc",
    "q_4489", "q_4821", "q_4822",
    # incident_b
    "order-service", "open_orders", "open_orders_settlement", "q_9031",
    # shared
    "db-primary", "api-gateway",
}

def extract_section(text, heading):
    m = re.search(rf"## {re.escape(heading)}(.+?)(?=^##|\Z)",
                  text, re.DOTALL | re.MULTILINE)
    return m.group(1) if m else ""

for path in ("postmortem_a.md", "postmortem_b.md"):
    if not Path(path).exists():
        check(FAIL, f"{path} action items specificity", "file missing")
        continue
    text = Path(path).read_text(encoding="utf-8")
    action_section = extract_section(text, "Action Items")
    hits = [c for c in KNOWN_COMPONENTS if c in action_section]
    if len(hits) >= 2:
        check(PASS, f"{path} action items reference named components",
              f"found: {hits}")
    elif len(hits) == 1:
        check(WARN, f"{path} action items reference named components",
              f"only found: {hits}")
    else:
        check(FAIL, f"{path} action items reference named components",
              "no specific component names found")


# ── 10. systemic actions derived after both post-mortems ──────────────────────

print("\n── 10. Systemic Actions Ordering ─────────────────────────────────────────")
if llm_calls:
    pm_calls  = [c for c in llm_calls if c["stage"] == "POSTMORTEMS_GENERATED"]
    # systemic actions are deterministic so not in llm_calls, but check file mtimes
    pm_ts = [c["timestamp"] for c in pm_calls]
    if len(pm_calls) == 2:
        check(PASS, "exactly 2 POSTMORTEMS_GENERATED LLM calls",
              f"timestamps: {pm_ts}")
    else:
        check(FAIL, "exactly 2 POSTMORTEMS_GENERATED LLM calls",
              f"found {len(pm_calls)}")

    if Path("systemic_actions.md").exists() and pm_calls:
        sys_mtime = Path("systemic_actions.md").stat().st_mtime
        pm_mtime  = max(
            Path("postmortem_a.md").stat().st_mtime,
            Path("postmortem_b.md").stat().st_mtime,
        )
        if sys_mtime >= pm_mtime:
            check(PASS, "systemic_actions.md written after both post-mortems")
        else:
            check(FAIL, "systemic_actions.md written after both post-mortems",
                  "file timestamps suggest wrong order")
else:
    check(WARN, "systemic actions ordering", "no LLM call log to inspect")

# systemic_actions.md non-empty
if Path("systemic_actions.md").exists():
    content = Path("systemic_actions.md").read_text(encoding="utf-8")
    n = len(re.findall(r"^## Systemic Action", content, re.MULTILINE))
    if n > 0:
        check(PASS, "systemic_actions.md has entries", f"{n} systemic action(s)")
    else:
        check(WARN, "systemic_actions.md has entries", "no entries found")


# ── 11. separate LLM call records per stage ────────────────────────────────────

print("\n── 11. LLM Call Records Per Stage ────────────────────────────────────────")
if llm_calls:
    from collections import Counter
    stage_counts = Counter(c["stage"] for c in llm_calls)
    for stage, expected in LLM_STAGES.items():
        actual = stage_counts.get(stage, 0)
        if actual == expected:
            check(PASS, f"{stage} call count", f"{actual} calls")
        else:
            check(FAIL, f"{stage} call count",
                  f"expected {expected}, got {actual}")

    # Each entry has required fields
    required_fields = {"stage","incident_id","timestamp","provider",
                       "model","prompt_hash","input_artifacts","output_artifact"}
    for i, c in enumerate(llm_calls):
        missing = required_fields - set(c.keys())
        if missing:
            check(FAIL, f"llm_calls entry {i} fields", f"missing {missing}")
            break
    else:
        check(PASS, "all llm_calls entries have required fields")

    # All entries reference anthropic + correct model
    wrong_model = [c for c in llm_calls if c.get("provider") != "anthropic"]
    check(
        PASS if not wrong_model else FAIL,
        "all LLM calls use provider=anthropic",
        f"{len(wrong_model)} wrong" if wrong_model else ""
    )
else:
    check(FAIL, "LLM call records", "llm_calls.jsonl empty or missing")


# ── summary ────────────────────────────────────────────────────────────────────

print(f"\n{'='*60}")
n_pass = sum(1 for r in results if r[0] == PASS)
n_warn = sum(1 for r in results if r[0] == WARN)
n_fail = sum(1 for r in results if r[0] == FAIL)
print(f"  VALIDATION SUMMARY: {n_pass} PASS  {n_warn} WARN  {n_fail} FAIL")
print(f"  Total checks: {len(results)}")
if n_fail == 0:
    print("  ✓ All required checks passed.")
else:
    print("  ✗ Failures detected — see above.")
print(f"{'='*60}\n")

sys.exit(0 if n_fail == 0 else 1)
