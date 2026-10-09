#!/usr/bin/env bash
# End-to-end checks against the running docker-compose stack. Each scenario uses its own commit,
# so scenarios don't see each other's evidence. Exit code = number of failed checks.
set -uo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
export CQP_URL=http://localhost:8080

compose() { docker compose -f "$root/docker-compose.yml" "$@"; }
cqp()     { uv run -q --project "$root" cqp "$@"; }
token()   { uv run -q --project "$root" python "$root/scripts/dev_token.py" "$@"; }  # sha [role] [project]
sql()     { compose exec -T postgres psql -U cqp -tAc "$1"; }
newsha()  { python3 -c "import secrets; print(secrets.token_hex(20))"; }
job_of()  { python3 -c "import base64,json,sys; p=sys.argv[1].split('.')[1]; print(json.loads(base64.urlsafe_b64decode(p+'='*(-len(p)%4)))['job_id'])" "$1"; }

passed=0; failed=0
check() {  # name expected actual
  if [ "$2" == "$3" ]; then echo "PASS  $1"; passed=$((passed + 1))
  else echo "FAIL  $1 (expected '$2', got '$3')"; failed=$((failed + 1)); fi
}

JUNIT_PASS='<testsuite><testcase classname="LedgerTest" name="rounding"/></testsuite>'
JUNIT_FAIL='<testsuite><testcase classname="PaymentRefundTest" name="partial"><failure/></testcase></testsuite>'
SARIF_CLEAN='{"runs":[{"results":[]}]}'
K6='{"metrics":{"http_req_duration":{"values":{"p(95)":212}}}}'

workspace() { cd "$(mktemp -d)" && mkdir reports && export CI_JOB_ID="e2e-$RANDOM"; }
upload() {  # check format content file
  printf '%s' "$3" > "reports/$4"
  cqp report --service "${SERVICE:-payments-api}" --check "$1" --format "$2" --files "reports/$4" 2>>gate.log
}
gate() {  # production decisions are asked for by the deploy system, with its own token
  local t="$CQP_TOKEN"; [[ " $* " == *" prod-deploy "* ]] && t="$(token "$(newsha)" "" platform/deployer)"
  CQP_TOKEN="$t" cqp gate --service "${SERVICE:-payments-api}" "$@" 2>gate.log; echo $?
}
hook() {  # tool json -> HTTP status of a correctly signed webhook
  local sig; sig="$(python3 -c "import hmac,hashlib,sys; print('sha256='+hmac.new(b'local-secret', sys.argv[1].encode(), hashlib.sha256).hexdigest())" "$2")"
  curl -s -o /dev/null -w '%{http_code}' "$CQP_URL/v1/hooks/$1" -H "X-CQP-Signature: $sig" -d "$2"
}
until_sql() {  # sql expected: poll up to 30s instead of sleeping a fixed time
  for _ in $(seq 60); do [ "$(sql "$1")" == "$2" ] && return 0; sleep 0.5; done; return 1
}
stored() {  # service check run: wait until that result is in Postgres
  until_sql "select count(*) from signals where service = '$1' and check_name = '$2' and run = '$3'" 1
}
wait_api() { for _ in $(seq 60); do [ "$(curl -s -o /dev/null -w '%{http_code}' "$CQP_URL/v1/decisions/x")" == "404" ] && return; sleep 1; done; }

echo "== gather → store → decide"

workspace; export CQP_TOKEN="$(token "$(newsha)")"
upload tests.unit junit "$JUNIT_PASS" junit.xml
upload scan.secrets sarif "$SARIF_CLEAN" secrets.sarif
check "PR with all expected evidence passing proceeds" 0 "$(gate --stage pr)"

workspace; export CQP_TOKEN="$(token "$(newsha)")"
upload tests.unit junit "$JUNIT_FAIL" junit.xml
upload scan.secrets sarif "$SARIF_CLEAN" secrets.sarif
check "PR with a failing unit test is blocked" 1 "$(gate --stage pr)"
check "the block names the failing test" 1 "$(grep -c 'PaymentRefundTest.partial' gate.log)"
blocked_id="$(grep -o 'decision [0-9a-f]*' gate.log | awk '{print $2}')"

check "a decision can be looked up by ID" BLOCKED \
  "$(curl -s "$CQP_URL/v1/decisions/$blocked_id" | python3 -c 'import json,sys; print(json.load(sys.stdin)["outcome"])')"
check "a decision replays to the same outcome" True "$(compose exec -T decision-api python -c "
from cqp.decide.server import pool, policy
from cqp.decide.app import replay
print(replay(pool.connection, policy, '$blocked_id'))" 2>/dev/null | tail -1)"

workspace; export CQP_TOKEN="$(token "$(newsha)")"
upload tests.unit junit "$JUNIT_PASS" junit.xml
upload tests.unit junit "$JUNIT_PASS" junit.xml   # same job uploads twice
event="$(head -1 .cqp/events/*.txt)"; until_sql "select count(*) from signals where event_id = '$event'" 1
check "a duplicate upload is stored once" 1 "$(sql "select count(*) from signals where event_id = '$event'")"

workspace; export CQP_TOKEN="$(token "$(newsha)")"; mkdir -p .cqp; echo "sha256:$(newsha)" > .cqp/image-digest
check "production with missing evidence fails closed (tier-1)" 1 "$(gate --stage prod-deploy --max-wait 0)"
check "...because evidence is missing, not because it was refused" yes "$(grep -q 'did not report' gate.log && echo yes)"

echo "== trust and bad input"

check "a repo can't report for a service it doesn't own" 403 "$(printf '<testsuite/>' > reports/j.xml; \
  curl -s -o /dev/null -w '%{http_code}' "$CQP_URL/v1/reports" -H "Authorization: Bearer $CQP_TOKEN" \
  -F 'meta={"service":"checkout-web","check":"tests.unit","format":"junit"}' -F 'files=@reports/j.xml')"
check "a service's own pipeline can't ask about production" 1 \
  "$(cqp gate --service payments-api --stage prod-deploy 2>gate.log; echo $?)"
check "...and is told why" 1 "$(grep -c 'only the deploy system' gate.log)"

printf 'x' > reports/x.xml
check "a forged CI token is rejected" 401 "$(curl -s -o /dev/null -w '%{http_code}' "$CQP_URL/v1/reports" \
  -H 'Authorization: Bearer forged' -F 'meta={"service":"s","check":"tests.unit","format":"junit"}' -F 'files=@reports/x.xml')"

sha="$(newsha)"
body="{\"provider\":\"payments-api\",\"provider_version\":\"$sha\",\"consumer\":\"web\",\"verification_url\":\"v/$sha\",\"success\":false}"
sig() { python3 -c "import hmac,hashlib,sys; print('sha256='+hmac.new(sys.argv[1].encode(), sys.argv[2].encode(), hashlib.sha256).hexdigest())" "$1" "$body"; }
check "a correctly signed Pact webhook is accepted" 202 "$(curl -s -o /dev/null -w '%{http_code}' "$CQP_URL/v1/hooks/pact-broker" \
  -H "X-CQP-Signature: $(sig local-secret)" -d "$body")"
stored payments-api tests.contract "v/$sha"
check "the webhook's contract result is stored" fail "$(sql "select status from signals where commit = '$sha' and check_name = 'tests.contract'")"
check "a webhook with a wrong signature is rejected" 401 "$(curl -s -o /dev/null -w '%{http_code}' "$CQP_URL/v1/hooks/pact-broker" \
  -H "X-CQP-Signature: $(sig wrong-secret)" -d "$body")"

workspace; export CQP_TOKEN="$(token "$(newsha)")"
upload tests.unit junit '<not xml' junit.xml
event="$(head -1 .cqp/events/*.txt)"
in_dlq() { compose exec -T kafka /opt/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server kafka:9092 --topic cqp.signals.dlq --from-beginning --timeout-ms 5000 2>/dev/null | grep -c "$event"; }
# Wait until the worker has handled it: "not stored" is trivially true before that.
for _ in $(seq 6); do parked="$(in_dlq)"; [ "$parked" -ge 1 ] && break; done
check "an unreadable report goes to the dead-letter topic" 1 "$parked"
check "an unreadable report is not stored" 0 "$(sql "select count(*) from signals where event_id = '$event'")"

echo "== long checks: pending, then resumed by the notifier"

workspace; sha="$(newsha)"; mkdir -p .cqp; echo "sha256:e2e-$sha" > .cqp/image-digest
export CQP_TOKEN="$(token "$sha")"
upload tests.integration junit "$JUNIT_PASS" junit.xml
gate_token="$(token "$sha")"
check "integration waits for the 2-hour load test (exit 2)" 2 "$(CQP_TOKEN=$gate_token gate --stage integration --max-wait 6)"
upload perf.load k6 "$K6" k6.json
retried=0
for _ in $(seq 30); do
  compose logs notifier 2>/dev/null | grep -q "retry job $(job_of "$gate_token") " && { retried=1; break; }; sleep 1
done
check "the notifier retries the waiting gate job once the result lands" 1 "$retried"
check "the retried gate gets the final decision and proceeds" 0 "$(CQP_TOKEN=$gate_token gate --stage integration)"

echo "== production quality"

# A fresh service each run: a failed canary affects a service for 24h and would leak into the next run.
export SERVICE="e2e-svc-$RANDOM"
prod_ready() {  # every check a default-tier prod deploy expects, passing, for a new image
  workspace; sha="$(newsha)"; mkdir -p .cqp; echo "sha256:$sha" > .cqp/image-digest
  export CQP_TOKEN="$(token "$sha" "" platform/e2e)"  # the e2e services' repo (see catalog.yaml)
  upload build.provenance json '{}' provenance.json
  upload tests.unit junit "$JUNIT_PASS" junit.xml
  upload scan.sca cyclonedx '{"vulnerabilities":[]}' sbom.json
  upload scan.secrets sarif "$SARIF_CLEAN" secrets.sarif
}
slo() {
  local at="$(date -u +%s)-$RANDOM"
  hook runtime "{\"type\":\"slo\",\"service\":\"$SERVICE\",\"error_budget_remaining\":$1,\"at\":\"$at\"}" >/dev/null
  stored "$SERVICE" runtime.slo "$at"
}
freeze() {  # true|false, then wait until it's stored
  local at="$(date -u +%s)-$RANDOM"
  hook runtime "{\"type\":\"freeze\",\"active\":$1,\"at\":\"$at\"}" >/dev/null
  stored '*' org.freeze "$at"
}
outcome() { gate --stage prod-deploy "$@" >/dev/null; head -1 gate.log | awk '{print $2}'; }

slo 0.5; prod_ready
check "healthy production: deploy proceeds" PROCEED "$(outcome)"

check "an incident webhook is accepted" 202 "$(hook runtime "{\"type\":\"incident\",\"service\":\"$SERVICE\",\"id\":\"INC-1\",\"severity\":\"sev1\",\"state\":\"open\"}")"
stored "$SERVICE" runtime.incident "INC-1:open"; prod_ready
check "active incident: deploy needs approval" REQUIRE_VALIDATION "$(outcome)"
check "active incident: a fix still goes out, with a warning" PROCEED_WITH_WARNING "$(outcome --fix INC-1)"
hook runtime "{\"type\":\"incident\",\"service\":\"$SERVICE\",\"id\":\"INC-1\",\"severity\":\"sev1\",\"state\":\"resolved\"}" >/dev/null
stored "$SERVICE" runtime.incident "INC-1:resolved"

run="r-$RANDOM"
check "a canary result from Argo Rollouts is accepted" 202 "$(hook argo-rollouts \
  "{\"service\":\"$SERVICE\",\"commit\":\"$sha\",\"image\":\"sha256:$sha\",\"analysis_run\":\"$run\",\"phase\":\"Failed\"}")"
stored "$SERVICE" deploy.canary "$run"; slo 0.5; prod_ready
check "after a failed canary: the next deploy warns" PROCEED_WITH_WARNING "$(outcome)"

freeze true; prod_ready
check "change freeze: deploy is blocked" BLOCKED "$(outcome)"
freeze false  # waited for, so the freeze can't leak into later sections
unset SERVICE

echo "== team policies and waivers"

policy_file() { printf '%s' "$1" > policy.yaml; echo "$PWD/policy.yaml"; }
workspace; sha="$(newsha)"; export CQP_TOKEN="$(token "$sha")"
check "another repo can't publish payments-api's policy" 403 "$(curl -s -o /dev/null -w '%{http_code}' \
  -X PUT "$CQP_URL/v1/team-policies/payments-api" -H "Authorization: Bearer $(token "$sha" "" checkout/checkout-web)" \
  -H 'Content-Type: application/json' -d '{}')"
check "a policy looser than the baseline fails its MR check" 1 \
  "$(cqp policy check --service payments-api --file "$(policy_file 'on_fail: {scan.sca: {build: warn}}')" 2>/dev/null; echo $?)"
check "a valid team policy is published" 0 \
  "$(cqp policy publish --service payments-api --file "$(policy_file 'thresholds: {perf.load: {max: 100, effect: block}}')" >/dev/null; echo $?)"
upload tests.integration junit "$JUNIT_PASS" integration.xml
upload perf.load k6 "$K6" k6.json
check "the team's p95 limit blocks integration" 1 "$(gate --stage integration)"
check "a developer can't create a waiver" 1 "$(cqp waiver --service payments-api --rule team.perf.load.threshold \
  --reason e2e --ticket E2E-1 --days 1 >/dev/null 2>&1; echo $?)"
waiver_id="$(CQP_TOKEN="$(token "$sha" approver)" cqp waiver --service payments-api --rule team.perf.load.threshold \
  --reason e2e --ticket E2E-1 --days 1 | python3 -c "import json,sys; print(json.load(sys.stdin)['waiver_id'])")"
check "with an approver's waiver it only warns" 0 "$(gate --stage integration)"
check "the warning names the waiver ticket" 1 "$(grep -c 'E2E-1' gate.log)"
check "the waiver can be revoked" 204 "$(curl -s -o /dev/null -w '%{http_code}' -X DELETE \
  "$CQP_URL/v1/waivers/$waiver_id" -H "Authorization: Bearer $(token "$sha" approver)")"
# Clean up: an empty policy makes payments-api follow the baseline again.
cqp policy publish --service payments-api --file "$(policy_file '{}')" >/dev/null

echo "== kill switches, break-glass and the portal"

decision_id() { grep -o 'decision [0-9a-f]*' gate.log | head -1 | cut -d' ' -f2; }
blocked_pr() {
  workspace; sha="$(newsha)"; export CQP_TOKEN="$(token "$sha")"
  upload tests.unit junit "$JUNIT_FAIL" junit.xml
  upload scan.secrets sarif "$SARIF_CLEAN" secrets.sarif
}
blocked_pr
check "a failing unit test blocks the PR" 1 "$(gate --stage pr)"
id="$(decision_id)"
check "a developer can't break glass" 1 "$(cqp break-glass --decision "$id" --reason e2e --ticket INC-E2E >/dev/null 2>&1; echo $?)"
check "on-call can break glass" 0 "$(CQP_TOKEN="$(token "$sha" oncall)" cqp break-glass --decision "$id" \
  --reason "e2e emergency" --ticket INC-E2E >/dev/null; echo $?)"
check "the re-run gate lets the change through" 0 "$(gate --stage pr)"
check "the gate log says it was break-glass" 1 "$(grep -c 'break-glass by' gate.log)"

blocked_pr
kill_id="$(CQP_TOKEN="$(token "$sha" platform)" cqp kill-switch --rule baseline.tests.unit \
  --reason "e2e: bad rule release" --hours 1 | python3 -c "import json,sys; print(json.load(sys.stdin)['kill_id'])")"
check "with the rule switched off, the PR only warns" 0 "$(gate --stage pr)"
check "the rule can be switched back on" 204 "$(curl -s -o /dev/null -w '%{http_code}' -X DELETE \
  "$CQP_URL/v1/kill-switches/$kill_id" -H "Authorization: Bearer $(token "$sha" platform)")"
check "switched back on, the PR is blocked again" 1 "$(gate --stage pr)"
# Checked before the outage tests restart the Decision API (its counters start again at 0).
check "the break-glass alert fires" 1 "$(for _ in $(seq 30); do
  n="$(curl -s "http://localhost:9090/api/v1/alerts" | grep -c BreakGlassUsed)"; [ "$n" -gt 0 ] && break; sleep 5; done; echo "${n:-0}")"
check "the portal shows the break-glass" yes "$(curl -s "$CQP_URL/portal" | grep -q 'INC-E2E' && echo yes)"

echo "== adoption modes"

# checkout-web is in advisory mode (policy/catalog.yaml): it's told, but never stopped.
workspace; sha="$(newsha)"; export SERVICE=checkout-web CQP_TOKEN="$(token "$sha" "" checkout/checkout-web)"
upload tests.unit junit "$JUNIT_FAIL" junit.xml
upload scan.secrets sarif "$SARIF_CLEAN" secrets.sarif
check "advisory mode: a failing test doesn't stop the pipeline" 0 "$(gate --stage pr)"
check "...but the log says it would be blocked once enforced" yes \
  "$(grep -q 'advisory mode: this would be BLOCKED' gate.log && echo yes)"
check "...and the decision is recorded as BLOCKED for the adoption numbers" BLOCKED \
  "$(sql "select outcome from decisions where service = 'checkout-web' and subject = '$sha' order by created_at desc limit 1")"
unset SERVICE

echo "== failures"

compose stop decision-api >/dev/null 2>&1
workspace; export CQP_TOKEN="$(token "$(newsha)")"
check "Decision API down: PR continues with a warning" 0 "$(gate --stage pr)"
check "Decision API down: production asks for approval" 2 "$(gate --stage prod-deploy)"
compose start decision-api >/dev/null 2>&1; wait_api

compose stop kafka >/dev/null 2>&1
workspace; export CQP_TOKEN="$(token "$(newsha)")"
upload tests.unit junit "$JUNIT_PASS" junit.xml; code=$?
check "Kafka down: the upload never fails the CI job" 0 "$code"
check "Kafka down: the job log says reports are kept for backfill" 1 "$(grep -c backfill gate.log)"
code="$(gate --stage pr --max-wait 0)"
check "Kafka down: the Decision API still answers (exit code, fallback used)" "0 0" "$code $(grep -c unavailable gate.log)"
compose start kafka >/dev/null 2>&1

# Workers crashed on the outage; compose restarts them with growing back-off, so poll.
workspace; export CQP_TOKEN="$(token "$(newsha)")"
flowing=0
for _ in $(seq 24); do
  upload tests.unit junit "$JUNIT_PASS" junit.xml
  event="$(head -1 .cqp/events/*.txt 2>/dev/null)"
  [ -n "$event" ] && [ "$(sql "select count(*) from signals where event_id = '$event'")" == "1" ] && { flowing=1; break; }
  sleep 5
done
check "after Kafka recovers, new evidence flows again" 1 "$flowing"

echo "== observability"

prom() { curl -s "http://localhost:9090/api/v1/$1"; }
up_targets() { prom targets | python3 -c 'import json,sys; print(sum(t["health"] == "up" for t in json.load(sys.stdin)["data"]["activeTargets"] if t["labels"]["job"] == "cqp"))'; }
firing() { prom alerts | python3 -c 'import json,sys; print(sum(a["labels"]["alertname"] == sys.argv[1] and a["state"] == "firing" for a in json.load(sys.stdin)["data"]["alerts"]))' "$1"; }
for _ in $(seq 12); do [ "$(up_targets)" == "4" ] && break; sleep 5; done
check "Prometheus scrapes all four CQP deployables" 4 "$(up_targets)"
# The unreadable report earlier in this run went to the dead-letter topic; that must raise a ticket.
for _ in $(seq 12); do [ "$(firing DeadLetterGrowing)" == "1" ] && break; sleep 5; done
check "the dead-letter alert is firing" 1 "$(firing DeadLetterGrowing)"

echo; echo "$passed passed, $failed failed"
exit "$failed"
