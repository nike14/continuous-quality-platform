#!/usr/bin/env bash
# Runs examples/opa/*.json against OPA: loads policy/baseline.rego, asks each example's rule
# with its input, prints the answer and whether it matches the example's `expect`.
# Uses $OPA_URL if set, otherwise starts a throwaway OPA container. Needs docker, curl and jq.
#   ./scripts/opa-examples.sh                 # every example
#   ./scripts/opa-examples.sh 07 08           # only the ones whose name starts with these
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"

if [ -z "${OPA_URL:-}" ]; then
  # The compose OPA has no published port, so run our own on a free one.
  id="$(docker run -d --rm -p 127.0.0.1::8181 openpolicyagent/opa:1.0.0 run --server --addr 0.0.0.0:8181)"
  trap 'docker stop "$id" >/dev/null' EXIT
  OPA_URL="http://$(docker port "$id" 8181 | head -1)"
  for _ in $(seq 50); do curl -sf "$OPA_URL/health" >/dev/null && break; sleep 0.2; done
fi
curl -sf -X PUT --data-binary "@$root/policy/baseline.rego" "$OPA_URL/v1/policies/cqp-baseline" >/dev/null

failed=0
for f in "$root"/examples/opa/*.json; do
  name="$(basename "$f" .json)"
  if [ $# -gt 0 ]; then
    match=no
    for p in "$@"; do [[ "$name" == "$p"* ]] && match=yes; done
    [ "$match" = yes ] || continue
  fi
  rule="$(jq -r .rule "$f")"
  got="$(jq '{input}' "$f" | curl -sf "$OPA_URL/v1/data/cqp/baseline/$rule" -d @- | jq '.result')"
  # Findings in rule order and error messages sorted, the same way CQP itself reads them.
  got="$(jq 'if (.[0] | type) == "object" then sort_by(.rule) else sort end' <<<"$got")"
  echo "== $name ($rule)"
  jq -r .about "$f"
  jq . <<<"$got"
  if [ "$got" = "$(jq '.expect' "$f")" ]; then echo "OK"; else echo "DIFFERENT from expect"; failed=1; fi
  echo
done
exit $failed
