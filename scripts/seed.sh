#!/usr/bin/env bash
# Fills every table with varied, realistic data by going through the real system (uploads,
# webhooks, gates). Needs the docker-compose stack running. Safe to run more than once.
set -uo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
export CQP_URL=http://localhost:8080

cqp()    { uv run -q --project "$root" cqp "$@"; }
token()  { uv run -q --project "$root" python "$root/scripts/dev_token.py" "$@"; }  # sha [role] [project]
newsha() { python3 -c "import secrets; print(secrets.token_hex(20))"; }

# Reports as the real tools write them (Surefire JUnit, Gitleaks/Semgrep SARIF, Trivy CycloneDX, k6 summary).
JUNIT_PASS="$(cat "$root/examples/reports/junit-pass.xml")"
JUNIT_FAIL="$(cat "$root/examples/reports/junit-fail.xml")"
SARIF_CLEAN="$(cat "$root/examples/reports/sarif-clean.json")"
SARIF_ISSUES="$(cat "$root/examples/reports/sarif-issues.json")"
SBOM_CLEAN="$(cat "$root/examples/reports/sbom-clean.json")"
SBOM_CRITICAL="$(cat "$root/examples/reports/sbom-critical.json")"
K6="$(cat "$root/examples/reports/k6-summary.json")"

repo_of() { case "$1" in checkout-web) echo checkout/checkout-web ;; ledger-service) echo payments/ledger-service ;;
  *) echo payments/payments-api ;; esac; }

# One change = its own workspace, commit and (optional) image.
change() {  # service [image]
  cd "$(mktemp -d)" && mkdir -p reports .cqp
  SERVICE="$1"; SHA="$(newsha)"; export CI_JOB_ID="seed-$RANDOM" CQP_TOKEN="$(token "$SHA" "" "$(repo_of "$1")")"
  if [ -n "${2:-}" ]; then echo "$2" > .cqp/image-digest; fi
}
upload() {  # check format content file
  printf '%s' "$3" > "reports/$4"
  cqp report --service "$SERVICE" --check "$1" --format "$2" --files "reports/$4" 2>/dev/null
}
deployer() { token "$(newsha)" "" platform/deployer; }  # production is asked about by the deploy system
gate() {  # stage [max-wait]
  local t="$CQP_TOKEN"; [ "$1" = prod-deploy ] && t="$(deployer)"
  CQP_TOKEN="$t" cqp gate --service "$SERVICE" --stage "$1" --max-wait "${2:-30}" 2>&1 | head -1
}

echo "-- PR: proceed"
change payments-api
upload tests.unit junit "$JUNIT_PASS" junit.xml
upload scan.secrets sarif "$SARIF_CLEAN" secrets.sarif
gate pr

echo "-- PR: blocked by a failing unit test, then a rerun that passes"
change payments-api
upload tests.unit junit "$JUNIT_FAIL" junit.xml
upload scan.secrets sarif "$SARIF_CLEAN" secrets.sarif
gate pr
export CI_JOB_ID="seed-$RANDOM" CQP_TOKEN="$(token "$SHA" "" "$(repo_of "$SERVICE")")"   # the retried job: same commit, new run
upload tests.unit junit "$JUNIT_PASS" junit.xml
gate pr

echo "-- PR: warning (static-analysis issues, secrets scan missing)"
change checkout-web
upload tests.unit junit "$JUNIT_PASS" junit.xml
upload static.quality sarif "$SARIF_ISSUES" lint.sarif
gate pr

echo "-- Build: blocked by a critical vulnerability"
change ledger-service "sha256:ledger-$RANDOM"
upload build.provenance json '{"built_by":"golden-path"}' provenance.json
upload tests.unit junit "$JUNIT_PASS" junit.xml
upload tests.contract junit "$JUNIT_PASS" contract.xml
upload scan.sca cyclonedx "$SBOM_CRITICAL" sbom.json
upload scan.secrets sarif "$SARIF_CLEAN" secrets.sarif
gate build

echo "-- Integration: proceed"
change payments-api "sha256:payments-$RANDOM"
upload tests.integration junit "$JUNIT_PASS" integration.xml
upload perf.load k6 "$K6" k6.json
gate integration

echo "-- Integration: still waiting for the load test (stays in waiting_pipelines for 2h)"
change payments-api "sha256:payments-$RANDOM"
upload tests.integration junit "$JUNIT_PASS" integration.xml
gate integration 0

hook() {  # tool json: a signed webhook, as the tool itself would send it
  local sig; sig="$(python3 -c "import hmac,hashlib,sys; print('sha256='+hmac.new(b'local-secret', sys.argv[1].encode(), hashlib.sha256).hexdigest())" "$2")"
  curl -s -o /dev/null -w "webhook $1: %{http_code}\n" "$CQP_URL/v1/hooks/$1" -H "X-CQP-Signature: $sig" -d "$2"
}
slo()      { hook runtime "{\"type\":\"slo\",\"service\":\"$1\",\"error_budget_remaining\":$2,\"at\":\"$(date -u +%s)-$RANDOM\"}"; }
incident() { hook runtime "{\"type\":\"incident\",\"service\":\"$1\",\"id\":\"$2\",\"severity\":\"sev1\",\"state\":\"$3\"}"; }
freeze()   { hook runtime "{\"type\":\"freeze\",\"active\":$1,\"at\":\"$(date -u +%s)-$RANDOM\"}"; }
prod_ready() {  # service image: every check a prod deploy expects, passing, for one image
  change "$1" "$2"
  upload build.provenance json '{"built_by":"golden-path"}' provenance.json
  upload tests.unit junit "$JUNIT_PASS" junit.xml
  upload tests.contract junit "$JUNIT_PASS" contract.xml
  upload scan.sca cyclonedx "$SBOM_CLEAN" sbom.json
  upload scan.secrets sarif "$SARIF_CLEAN" secrets.sarif
  upload tests.integration junit "$JUNIT_PASS" integration.xml
  upload perf.load k6 "$K6" k6.json
}

echo "-- Prod deploy: proceed (tier-1, all evidence, healthy production)"
slo payments-api 0.42
prod_ready payments-api "sha256:payments-$RANDOM"
gate prod-deploy

echo "-- Prod deploy: blocked (tier-1, evidence missing)"
change payments-api "sha256:payments-$RANDOM"
gate prod-deploy 0

echo "-- Prod deploy: needs validation (default tier, only provenance)"
change checkout-web "sha256:checkout-$RANDOM"
upload build.provenance json '{"built_by":"golden-path"}' provenance.json
gate prod-deploy

echo "-- Production: active incident needs approval, but a fix goes out"
slo payments-api 0.42
incident payments-api "INC-$RANDOM" open
prod_ready payments-api "sha256:payments-$RANDOM"
gate prod-deploy
CQP_TOKEN="$(deployer)" cqp gate --service payments-api --stage prod-deploy --fix "INC-$RANDOM" 2>&1 | head -1
incident payments-api "INC-resolved-$RANDOM" resolved

echo "-- Production: error budget used up needs approval"
slo ledger-service 0
prod_ready ledger-service "sha256:ledger-$RANDOM"
gate prod-deploy

echo "-- Production: a canary failed and was rolled back, so the next deploy warns"
hook argo-rollouts "{\"service\":\"ledger-service\",\"commit\":\"$SHA\",\"image\":\"sha256:ledger-bad\",\"analysis_run\":\"run-$RANDOM\",\"phase\":\"Failed\"}"
slo ledger-service 0.3
prod_ready ledger-service "sha256:ledger-$RANDOM"
gate prod-deploy

echo "-- Production: organisation-wide change freeze blocks"
freeze true
slo payments-api 0.42
prod_ready payments-api "sha256:payments-$RANDOM"
gate prod-deploy
freeze false

echo "-- Team policy: checkout-web publishes its own rules (p95 limit 150ms)"
CQP_TOKEN="$(token "$(newsha)" "" checkout/checkout-web)" \
  cqp policy publish --service checkout-web --file "$root/examples/team-policy.yaml"

echo "-- Integration: needs validation, load test over checkout-web's own limit"
change checkout-web "sha256:checkout-$RANDOM"
export CQP_TOKEN="$(token "$SHA" "" checkout/checkout-web)"
upload tests.integration junit "$JUNIT_PASS" integration.xml
upload perf.load k6 "$K6" k6.json
upload tests.e2e junit "$JUNIT_PASS" e2e.xml
gate integration

echo "-- Waivers: security accepts ledger-service's critical CVE for a week, so the build only warns"
waiver_id="$(CQP_TOKEN="$(token x security)" cqp waiver --service ledger-service --rule baseline.scan.sca --item CVE-2026-1234 \
  --reason "no fixed version yet; not reachable from our code" --ticket SEC-118 --days 7 \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['waiver_id'])")"
change ledger-service "sha256:ledger-$RANDOM"
upload tests.unit junit "$JUNIT_PASS" junit.xml
upload tests.contract junit "$JUNIT_PASS" contract.xml
upload scan.sca cyclonedx "$SBOM_CRITICAL" sbom.json
upload scan.secrets sarif "$SARIF_CLEAN" secrets.sarif
gate build
# Revoked again so the earlier "blocked by a critical vulnerability" scenario stays blocked on a rerun.
curl -s -o /dev/null -X DELETE "$CQP_URL/v1/waivers/$waiver_id" -H "Authorization: Bearer $(token x security)"

echo "-- Waivers: an active one, static-analysis issues on checkout-web for two weeks"
CQP_TOKEN="$(token x approver checkout/checkout-web)" cqp waiver --service checkout-web --rule baseline.static.quality \
  --reason "legacy module, rewrite planned" --ticket CHK-42 --days 14

echo "-- Break-glass: on-call lets a blocked ledger-service build through during an incident"
change ledger-service "sha256:ledger-$RANDOM"
upload tests.unit junit "$JUNIT_PASS" junit.xml
upload tests.contract junit "$JUNIT_PASS" contract.xml
upload scan.sca cyclonedx "$SBOM_CRITICAL" sbom.json
upload scan.secrets sarif "$SARIF_CLEAN" secrets.sarif
decision="$(gate build | grep -o 'decision [0-9a-f]*' | cut -d' ' -f2)"
CQP_TOKEN="$(token "$SHA" oncall)" cqp break-glass --decision "$decision" \
  --reason "ledger reconciliation down; hotfix needed" --ticket "INC-$RANDOM"
gate build

echo "-- Kill switch: platform switches off the rollback rule for 2h (misconfigured canary analysis)"
CQP_TOKEN="$(token x platform)" cqp kill-switch --rule baseline.recent_rollback \
  --reason "canary analysis misconfigured, fix in progress" --hours 2

echo "-- Pact Broker webhook: a contract verification that failed"
hook pact-broker "{\"provider\":\"checkout-web\",\"provider_version\":\"$(newsha)\",\"consumer\":\"mobile-app\",\"verification_url\":\"v/$RANDOM\",\"success\":false}"

sleep 12  # let the notifier's sweep run once
echo
docker compose -f "$root/docker-compose.yml" exec -T postgres psql -U cqp -c "
  SELECT 'signals' AS table_name, count(*) FROM signals
  UNION ALL SELECT 'builds', count(*) FROM builds
  UNION ALL SELECT 'decisions', count(*) FROM decisions
  UNION ALL SELECT 'waiting_pipelines', count(*) FROM waiting_pipelines
  UNION ALL SELECT 'team_policies', count(*) FROM team_policies
  UNION ALL SELECT 'waivers', count(*) FROM waivers
  UNION ALL SELECT 'kill_switches', count(*) FROM kill_switches
  UNION ALL SELECT 'break_glass', count(*) FROM break_glass;"
