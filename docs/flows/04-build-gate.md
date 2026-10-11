# 4. Build gate: waiver and break-glass

[← All flows](README.md) · Step 4 of 14

**Callers:** build pipeline jobs, then security (`cqp waiver`) and on-call (`cqp break-glass`).

1. The build job writes the image digest to `.cqp/image-digest` and uploads `build.provenance`
   (format `json`) with it. From now on every upload and the gate carry `"image": "sha256:…"`.
2. `tests.unit`, `tests.contract`, `scan.sca` (an SBOM with a critical CVE) and `scan.secrets` are
   uploaded. The build gate answers `BLOCKED` by `baseline.scan.sca`, items `["CVE-2026-1234"]`.
3. **Waiver.** A security approver accepts that CVE for a while:

   ```json
   POST /v1/waivers                      (token with cqp_role: security)
   {
     "service": "ledger-service",
     "rule": "baseline.scan.sca",
     "items": ["CVE-2026-1234"],
     "reason": "no fixed version yet; not reachable from our code",
     "ticket": "SEC-118",
     "days": 7
   }
   ```

   Response `201` with `waiver_id`, `approver` (from the token's `user_login`) and `expires_at`. The
   rules in `baseline.rego` refuse it with a 403 and a reason when: the caller isn't an approver;
   it's a security rule and the caller isn't `security`; a scan waiver names no items; it's over 30
   days; or the rule is `baseline.evidence_required` or `baseline.change_freeze`.
4. The gate now answers `PROCEED_WITH_WARNING`: the finding still shows, but only warns
   (`waived until …, SEC-118`). A new critical CVE that isn't in `items` blocks again.
5. `GET /v1/waivers?service=ledger-service` lists active waivers; `DELETE /v1/waivers/{id}` (an
   approver) revokes one (`204`). The gate is `BLOCKED` again.
6. **Break-glass.** On-call lets this one change through this stage for 24 hours:

   ```json
   POST /v1/decisions/{decision_id}/break-glass      (cqp_role: oncall, approver or security)
   {"reason": "ledger reconciliation down; hotfix needed", "ticket": "INC-77"}
   ```

   Response `201` with `break_glass_id`. It is tied to that decision's service, image or commit, and
   stage. The gate job must be **re-run by hand**; it then answers `PROCEED_WITH_WARNING`, with every
   finding turned into a warning and a `baseline.break_glass` finding added.

**Test it in Postman** ([setup](13-postman-testing.md)): folder *04 · Build gate*.

---

[← 3. PR gate: blocked, then fixed](03-pr-gate.md) · [5. Team policy →](05-team-policy.md)
