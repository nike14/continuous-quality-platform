# Organisation baseline for every CQP decision, evaluated by the OPA sidecar.
# Input: one decision's evidence, missing checks, team policy, waivers, kill switches,
# break-glass and (for prod-deploy) production state. Output: `findings`, each with an effect.
# The *_errors rules check team policy files and override requests before they're accepted.
package cqp.baseline

import rego.v1

# Effect when a check FAILS at a stage. A check not listed for a stage isn't judged there.
# Missing evidence uses on_missing from expectations.yaml instead.
# prod-deploy judges every check it expects: failing evidence must never do better than none.
on_fail := {
	"tests.unit": {"pr": "block", "build": "block", "prod-deploy": "block"},
	"tests.contract": {"pr": "warn", "build": "block", "integration": "block", "prod-deploy": "block"},
	"tests.integration": {"integration": "block", "prod-deploy": "block"},
	"tests.e2e": {"pr": "warn", "integration": "validate", "prod-deploy": "warn"},
	"scan.sca": {"pr": "warn", "build": "block", "integration": "block", "prod-deploy": "block"},
	"scan.secrets": {"pr": "block", "build": "block", "prod-deploy": "block"},
	"static.quality": {"pr": "warn", "build": "warn"},
}

# Organisation-wide cells: a team policy may make them stricter, never looser.
locked := {
	"tests.unit": {"prod-deploy"},
	"tests.contract": {"integration", "prod-deploy"},
	"tests.integration": {"prod-deploy"},
	"scan.sca": {"build", "integration", "prod-deploy"},
	"scan.secrets": {"pr", "build", "prod-deploy"},
}

# Orders effects, so "stricter" can be checked: a team may only move a locked cell up this list.
rank := {"warn": 1, "validate": 2, "block": 3}

# Only a security approver may waive these; nobody may waive the second set (break-glass instead).
security_rules := {"baseline.tests.contract", "baseline.scan.sca", "baseline.scan.secrets"}

not_waivable := {"baseline.evidence_required", "baseline.change_freeze"}

# A waiver on these must name the items it accepts (CVE IDs, finding fingerprints), so a new
# critical CVE found while it is active still blocks.
item_rules := {"baseline.scan.sca", "baseline.scan.secrets"}

# ---- Decisions: raw findings, then kill switches, waivers and break-glass ----

# Every raw finding goes through the same steps: kill switch, else waiver, then break-glass on top.
findings contains final if {
	some f in raw
	final := adjust(f)
}

# Break-glass is itself a finding, so it shows in the MR comment and in Slack.
findings contains f if {
	bg := input.break_glass
	bg != null # in Rego a null value still counts as true, so say it outright
	f := {"rule": "baseline.break_glass", "effect": "warn",
	      "message": sprintf("break-glass by %s: %s (%s)", [bg.by, bg.reason, bg.ticket])}
}

# ---- Raw findings: what the evidence and production say, before any override ----

raw contains f if {
	some check, e in input.evidence
	e.status == "fail"
	f := {
		"rule": sprintf("baseline.%s", [check]),
		"effect": effect_for(check),
		"message": sprintf("%s failed: %s", [check, detail(e)]),
		# What made it fail (test names, CVE IDs, fingerprints), which a waiver can name.
		"items": object.get(e.payload, "failing", []),
	}
}

raw contains f if {
	some m in input.missing
	f := {
		"rule": "baseline.evidence_required",
		"effect": m.on_missing,
		"message": missing_message(m),
	}
}

missing_message(m) := sprintf("%s result is too old to count; run it again", [m.check]) if m.stale

else := sprintf("%s did not report before its deadline", [m.check])

# A team's own limit on a measurement, e.g. p95 latency.
raw contains f if {
	some check, t in input.team.thresholds
	e := input.evidence[check]
	e.payload.value > t.max
	f := {
		"rule": sprintf("team.%s.threshold", [check]),
		"effect": t.effect,
		"message": sprintf("%s is %v %s, over the team's limit of %v", [check, e.payload.value, e.payload.unit, t.max]),
	}
}

# Production state shapes the next production deploy. Only present for prod-deploy decisions.

# No SLO data at all is null, not 0; production_health below covers that case.
raw contains f if {
	is_number(input.runtime.error_budget_remaining)
	input.runtime.error_budget_remaining <= 0
	not input.fix
	f := {"rule": "baseline.error_budget", "effect": "validate",
	      "message": "error budget used up: only fixes go out without approval"}
}

# A fix skips the two approvals above, so it's always visible: a warning, which also goes to Slack.
raw contains f if {
	input.fix
	f := {"rule": "baseline.fix_release", "effect": "warn",
	      "message": sprintf("released as a fix under %s", [input.fix_ticket])}
}

raw contains f if {
	input.runtime.incident != null
	not input.fix
	f := {"rule": "baseline.active_incident", "effect": "validate",
	      "message": sprintf("active %s incident: only fixes go out without approval", [input.runtime.incident])}
}

raw contains f if {
	input.runtime.rolled_back_recently
	f := {"rule": "baseline.recent_rollback", "effect": "warn",
	      "message": "a canary failed in the last 24h: use the slowest canary steps"}
}

raw contains f if {
	input.runtime.freeze
	f := {"rule": "baseline.change_freeze", "effect": "block", "message": "organisation-wide change freeze"}
}

raw contains f if {
	input.runtime
	not input.runtime.health_fresh
	f := {"rule": "baseline.production_health", "effect": stale_health_effect,
	      "message": "production health is missing or older than 5 minutes"}
}

# When we can't see production, tier-1 needs a person to look; everyone else is just told.
stale_health_effect := "validate" if input.tier == "tier-1"

else := "warn"

# The team's effect for a failing check if the baseline allows it, otherwise the baseline's.
effect_for(check) := e if {
	e := input.team.on_fail[check][input.stage]
	allowed(check, input.stage, e)
} else := on_fail[check][input.stage]

# Two rules means either is enough: the cell isn't locked, or the team's effect is at least as strict.
allowed(check, stage, e) if not stage in object.get(locked, check, set())

allowed(check, stage, e) if rank[e] >= rank[on_fail[check][stage]]

adjust(f) := overridden(step(f)) if input.break_glass != null # explicit, see the null note above

else := step(f)

# A rule switched off by the platform team (e.g. a bad rule release) only warns, everywhere.
step(f) := object.union(f, {
	"effect": "warn",
	"message": sprintf("%s (rule switched off: %s)", [f.message, killed[f.rule]]),
}) if f.rule in object.keys(killed)

else := apply_waiver(f)

killed[k.rule] := k.reason if some k in input.kill_switches

# Under break-glass nothing stops the change, but every finding stays visible.
overridden(f) := object.union(f, {"effect": "warn", "message": sprintf("%s (overridden by break-glass)", [f.message])}) if {
	f.effect != "warn"
} else := f

# A waived finding stays visible but only warns, and says which ticket accepted it.
apply_waiver(f) := object.union(f, {
	"effect": "warn",
	"waiver": w.ticket,
	"message": sprintf("%s (waived until %s, %s)", [f.message, w.expires_at, w.ticket]),
}) if {
	not f.rule in not_waivable
	ws := [w | some w in input.waivers; w.rule == f.rule; covers(w, f)]
	count(ws) > 0
	w := ws[0] # several can match; quote the oldest (input is sorted by created_at)
} else := f

# A waiver without items accepts any finding of its rule.
covers(w, f) if count(object.get(w, "items", [])) == 0

# One with items accepts a finding only if everything failing is among them: one new CVE and
# the finding blocks again.
covers(w, f) if {
	failing := object.get(f, "items", [])
	count(failing) > 0
	every i in failing { i in w.items }
}

# ---- Checking a team policy file before it is published ----

team_policy_errors contains msg if {
	some check, by_stage in input.team.on_fail
	some stage, e in by_stage
	not allowed(check, stage, e)
	msg := sprintf("%s at %s can't be looser than the organisation baseline (%s)", [check, stage, on_fail[check][stage]])
}

# ---- Checking a waiver request ----

waiver_errors contains "this rule can't be waived; use break-glass" if input.waiver.rule in not_waivable

waiver_errors contains "only approvers can create waivers" if not input.role in {"approver", "security"}

waiver_errors contains "baseline security rules need a security approver" if {
	input.waiver.rule in security_rules
	input.role != "security"
}

waiver_errors contains "a waiver lasts at most 30 days" if input.waiver.days > 30

waiver_errors contains "a scan waiver must name the items it accepts, e.g. a CVE ID" if {
	input.waiver.rule in item_rules
	count(object.get(input.waiver, "items", [])) == 0
}

# ---- Checking a kill switch or break-glass request ----

kill_errors contains "only the platform team can switch off rules" if not input.role == "platform"

kill_errors contains "a kill switch lasts at most 72 hours" if input.kill.hours > 72

break_glass_errors contains "only approvers and on-call engineers can break glass" if {
	not input.role in {"approver", "security", "oncall"}
}

# The most useful one-line detail for each kind of signal.
detail(e) := concat(", ", e.payload.failing) if {
	count(e.payload.failing) > 0
} else := sprintf("%v", [e.payload.by_severity]) if {
	e.payload.by_severity
} else := sprintf("%v", [e.payload.by_level]) if {
	e.payload.by_level
} else := e.status
