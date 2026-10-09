#!/usr/bin/env bash
# End-to-end local demo: upload a JUnit report with one failing test, then ask the PR gate.
# Needs `docker compose up -d --build` first.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cqp() { uv run --project "$root" cqp "$@"; }

export CQP_URL=http://localhost:8080
export CQP_TOKEN="$(uv run --project "$root" python "$root/scripts/dev_token.py")"
export CI_MERGE_REQUEST_IID=7 CI_JOB_ID="demo-$RANDOM"

work="$(mktemp -d)" && cd "$work"
mkdir reports
cat > reports/junit.xml <<'XML'
<testsuite>
  <testcase classname="PaymentRefundTest" name="partial"><failure/></testcase>
  <testcase classname="LedgerTest" name="rounding"/>
</testsuite>
XML

echo "== gather: upload the report"
cqp report --service payments-api --check tests.unit --format junit --files 'reports/*.xml'
cat .cqp/events/*.txt

echo "== decide: ask the PR gate (expect BLOCKED, exit 1)"
set +e
cqp gate --service payments-api --stage pr --max-wait 30
echo "gate exit code: $?"
set -e

echo "== notify: what the notifier would post to GitLab"
sleep 2
docker compose -f "$root/docker-compose.yml" logs notifier --tail 15
