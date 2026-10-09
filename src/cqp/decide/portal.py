"""The portal page: waivers, kill switches, break-glass, team policies and recent decisions.

Server-rendered for reading. Creating or revoking a waiver posts to the same API the CLI uses,
with a token the person pastes in (in production: the company SSO session).
"""

from html import escape

STYLE = """
body{font:14px/1.5 system-ui,sans-serif;margin:24px auto;max-width:1100px;padding:0 16px;
color:#1d2433;background:#fff}
h1{font-size:20px}h2{font-size:16px;margin-top:28px}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #e3e6ec;vertical-align:top}
th{font-weight:600;color:#5b6475}code{font-size:12px}.empty{color:#8a92a3}
.BLOCKED{color:#b42318}.REQUIRE_VALIDATION{color:#b54708}.PROCEED_WITH_WARNING{color:#a15c07}.PROCEED{color:#067647}
form{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}input,button{font:inherit;padding:4px 8px}
#msg{margin-top:8px;color:#5b6475}
"""

SCRIPT = """
async function call(method, path, body) {
  const token = document.getElementById('token').value.trim();
  const r = await fetch(path, {method, headers: {'Authorization': 'Bearer ' + token,
    'Content-Type': 'application/json'}, body: body && JSON.stringify(body)});
  const text = r.status === 204 ? '' : await r.text();
  document.getElementById('msg').textContent = r.ok ? 'Done.' : r.status + ': ' + text;
  if (r.ok) setTimeout(() => location.reload(), 500);  // reload so the tables show the change
}
function createWaiver(form) {
  const f = Object.fromEntries(new FormData(form));
  call('POST', '/v1/waivers', {...f, days: Number(f.days)});
  return false;
}
"""


# One plain table. Every value is escaped: reasons and tickets are typed in by people.
def _table(rows: list[dict], columns: list[tuple[str, str]], actions=None) -> str:
    # An empty section says so, rather than showing an empty table.
    if not rows:
        return '<p class="empty">None.</p>'
    head = "".join(f"<th>{escape(label)}</th>" for _, label in columns) + (
        "<th></th>" if actions else ""
    )
    body = ""
    for r in rows:
        cells = ""
        for key, _ in columns:
            value = escape(str(r[key])) if r[key] is not None else ""
            # Outcome cells get a class named after the outcome,
            # so they're coloured by the style sheet.
            cls = f' class="{value}"' if key == "outcome" else ""
            cells += f"<td{cls}>{value}</td>"
        body += f"<tr>{cells}{f'<td>{actions(r)}</td>' if actions else ''}</tr>"
    return f"<table><tr>{head}</tr>{body}</table>"


def render(view: dict, catalog: dict) -> str:
    """The whole portal page as HTML."""
    # Services come from the catalog, joined with the team policy each one has published.
    published = {p["service"]: p for p in view["team_policies"]}
    services = [
        {
            "service": name,
            "tier": c.get("tier", "default"),
            "owner": c.get("owner"),
            "repo": c.get("repo"),
            "team_policy": published.get(name, {}).get("version"),
        }
        for name, c in sorted(catalog.items())
    ]

    def revoke(w):
        path = f"/v1/waivers/{escape(w['waiver_id'])}"
        return f"""<button onclick="call('DELETE', '{path}')">Revoke</button>"""

    # Each table as (rows, [(field, column heading), ...]).
    waivers = _table(
        view["waivers"],
        [
            ("service", "Service"),
            ("rule", "Rule"),
            ("ticket", "Ticket"),
            ("reason", "Reason"),
            ("approver", "Approver"),
            ("expires_at", "Expires"),
        ],
        revoke,
    )
    kill_switches = _table(
        view["kill_switches"],
        [("rule", "Rule"), ("reason", "Reason"), ("created_by", "By"), ("expires_at", "Until")],
    )
    break_glass = _table(
        view["break_glass"],
        [
            ("service", "Service"),
            ("stage", "Stage"),
            ("subject", "Change"),
            ("reason", "Reason"),
            ("ticket", "Ticket"),
            ("created_by", "By"),
            ("created_at", "When"),
        ],
    )
    service_table = _table(
        services,
        [
            ("service", "Service"),
            ("tier", "Tier"),
            ("owner", "Owner"),
            ("repo", "Repo"),
            ("team_policy", "Team policy version"),
        ],
    )
    decisions = _table(
        view["decisions"],
        [
            ("created_at", "When"),
            ("service", "Service"),
            ("stage", "Stage"),
            ("status", "Status"),
            ("outcome", "Outcome"),
            ("decision_id", "Decision"),
        ],
    )

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>CQP portal</title>
<style>{STYLE}</style></head><body>
<h1>Continuous Quality Platform</h1>
<label>Your token (for changes): <input id="token" size="40" placeholder="paste a token"></label>
<div id="msg"></div>

<h2>Active waivers</h2>
{waivers}
<form onsubmit="return createWaiver(this)">
  <input name="service" placeholder="service" required>
  <input name="rule" placeholder="baseline.scan.sca" required>
  <input name="reason" placeholder="reason" required>
  <input name="ticket" placeholder="ticket" required>
  <input name="days" type="number" min="1" max="30" value="14" required>
  <button>Create waiver</button>
</form>

<h2>Rules switched off</h2>
{kill_switches}

<h2>Break-glass, last 7 days</h2>
{break_glass}

<h2>Services</h2>
{service_table}

<h2>Recent decisions</h2>
{decisions}
<script>{SCRIPT}</script></body></html>"""
