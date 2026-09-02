"""Review app: see each proposed CRM change with its evidence, approve or reject it.
Approving applies the change through the CRM API immediately. Nothing writes without approval.

    .venv/bin/python review_app/app.py     ->  http://127.0.0.1:5055
"""
import json
import subprocess
import sys
from pathlib import Path

from flask import Flask, render_template_string, request, redirect, url_for, flash

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bellhaven_sync import config  # noqa: E402
from bellhaven_sync.store import Store  # noqa: E402
from bellhaven_sync.crm import CRM  # noqa: E402
from bellhaven_sync.apply import apply_proposal  # noqa: E402

app = Flask(__name__)
app.secret_key = "local-review-only"
store = Store()

KIND_LABELS = {
    "create_account": ("New account", "#1e6e46"),
    "reparent": ("Re-parent", "#1b4f72"),
    "chow_new_account": ("CHOW (billing lock)", "#8a3a9c"),
    "chow_link": ("CHOW link", "#8a3a9c"),
    "chow_outbound": ("Sold / CHOW out", "#9c3a3a"),
    "mark_duplicate": ("Duplicate", "#b45309"),
    "rename": ("Rename", "#0f766e"),
    "fix_address": ("Address fix", "#0f766e"),
    "fix_care_type": ("Care type fix", "#0f766e"),
    "fix_phone": ("Phone fix", "#6b7280"),
    "reactivate": ("Reactivate", "#1e6e46"),
    "flag_missing": ("Not on website", "#b45309"),
    "add_contact": ("Add contact", "#1e6e46"),
    "replace_admin": ("Administrator changed", "#0f766e"),
    "fix_contact": ("Contact fix", "#0f766e"),
    "move_contact": ("Move contact", "#b45309"),
}
KIND_ORDER = list(KIND_LABELS)

PAGE = r"""
<!doctype html><html><head><meta charset="utf-8"><title>Bellhaven CRM sync review</title>
<style>
 :root{--ink:#22303a;--muted:#64748b;--line:#d5dce2;--bg:#eef1f4;--blue:#1b4f72;--green:#1e6e46;--red:#9c3a3a}
 body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
 .top{background:var(--blue);color:#fff;padding:10px 22px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
 .top b{font-size:16px}.top a{color:#cfe3f3;text-decoration:none}.top form{margin:0}
 .wrap{max-width:1180px;margin:0 auto;padding:18px 20px 80px}
 .tabs a{display:inline-block;padding:6px 12px;margin-right:6px;border-radius:999px;background:#fff;border:1px solid var(--line);color:var(--ink);text-decoration:none}
 .tabs a.on{background:var(--blue);color:#fff;border-color:var(--blue)}
 .filters{margin:12px 0;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
 .filters a{font-size:12px;padding:3px 9px;border-radius:999px;border:1px solid var(--line);background:#fff;color:var(--ink);text-decoration:none}
 .filters a.on{background:#dbe7f0;border-color:var(--blue)}
 .card{background:#fff;border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:12px 0}
 .card.failed{border-color:#e3b4b4}.card.applied{opacity:.85}
 .hdr{display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap}
 .badge{display:inline-block;color:#fff;font-size:11px;font-weight:600;padding:2px 9px;border-radius:4px;letter-spacing:.03em}
 .conf{font-size:11px;padding:2px 8px;border-radius:999px;background:#eef1f4;color:var(--muted)}
 .conf.high{background:#e2f2e6;color:var(--green)}.conf.low{background:#f1e4e4;color:var(--red)}
 h3{margin:4px 0 2px;font-size:16px}.sum{margin:4px 0 8px;color:#374151}
 ul.reasons{margin:4px 0 8px 18px;padding:0}ul.reasons li{margin:2px 0}
 table.cmp{border-collapse:collapse;font-size:13px;margin:8px 0;width:100%}
 table.cmp th,table.cmp td{border:1px solid #e8edf1;padding:4px 8px;text-align:left;vertical-align:top}
 table.cmp th{background:#f7f9fa;font-size:11px;text-transform:uppercase;color:var(--muted)}
 td.diff{background:#fff7e6}
 details{margin-top:6px}summary{cursor:pointer;color:var(--blue);font-size:13px}
 pre{background:#f7f9fa;border:1px solid #e8edf1;padding:8px;border-radius:5px;font-size:12px;overflow-x:auto;white-space:pre-wrap}
 .act{display:flex;gap:8px;align-items:center;margin-top:10px;flex-wrap:wrap}
 button{padding:7px 14px;border:0;border-radius:5px;font-size:13px;cursor:pointer;color:#fff;background:var(--blue)}
 button.ok{background:var(--green)}button.no{background:#6b7280}button.warn{background:var(--red)}
 .muted{color:var(--muted);font-size:12px}
 .result{margin-top:8px;font-size:12.5px;padding:6px 10px;border-radius:5px;background:#e2f2e6}
 .result.err{background:#f1e4e4}
 .bulk{position:sticky;top:0;background:#fff;border:1px solid var(--line);border-radius:8px;padding:8px 12px;display:flex;gap:10px;align-items:center;z-index:2}
 .flash{background:#fff7e6;border:1px solid #f0d9a8;padding:8px 12px;border-radius:6px;margin:10px 0}
 .kv{font-size:12.5px;color:#374151}.kv b{color:var(--ink)}
 a.acct{color:var(--blue);text-decoration:none;font-family:ui-monospace,Menlo,monospace;font-size:12px}
</style></head><body>
<div class="top"><b>Bellhaven → CRM sync · review queue</b>
 <span>pending <b>{{counts.get('pending',0)}}</b> · applied <b>{{counts.get('applied',0)}}</b> · rejected <b>{{counts.get('rejected',0)}}</b> · failed <b>{{counts.get('failed',0)}}</b> · stale <b>{{counts.get('stale',0)}}</b></span>
 <form method="post" action="{{url_for('run_now')}}" onsubmit="this.querySelector('button').disabled=true;this.querySelector('button').textContent='Running…'"><button>Run pipeline now</button></form>
 <span class="muted" style="color:#cfe3f3">last run: {{last_run}}</span>
 <a href="{{crm_ui}}" target="_blank">open CRM ↗</a>
</div>
<div class="wrap">
{% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="flash">{{m}}</div>{% endfor %}{% endwith %}
<div class="tabs">
 {% for s in ['pending','applied','rejected','failed','stale'] %}<a class="{{'on' if status==s}}" href="{{url_for('index',status=s)}}">{{s}} ({{counts.get(s,0)}})</a>{% endfor %}
</div>
<div class="filters"><span class="muted">kind:</span><a class="{{'on' if not kind}}" href="{{url_for('index',status=status)}}">all</a>
 {% for k in kinds %}<a class="{{'on' if kind==k}}" href="{{url_for('index',status=status,kind=k)}}">{{labels[k][0]}} ({{kind_counts.get(k,0)}})</a>{% endfor %}
</div>

<form method="post" action="{{url_for('bulk')}}" id="bulk">
<input type="hidden" name="status" value="{{status}}"><input type="hidden" name="kind" value="{{kind or ''}}">
{% if status=='pending' and items %}
<div class="bulk"><label><input type="checkbox" onchange="document.querySelectorAll('.pick').forEach(c=>c.checked=this.checked)"> select all shown</label>
 <button class="ok" name="decision" value="approve" onclick="return confirm('Apply all selected proposals to the CRM?')">Approve &amp; apply selected</button>
 <button class="no" name="decision" value="reject">Reject selected</button>
 <span class="muted">reviewer: <input name="by" value="{{by}}" size="14"></span></div>
{% endif %}

{% for p in items %}
<div class="card {{p.status}}">
 <div class="hdr">
  {% if status=='pending' %}<input type="checkbox" class="pick" name="fp" value="{{p.fingerprint}}">{% endif %}
  <span class="badge" style="background:{{labels[p.kind][1]}}">{{labels[p.kind][0]}}</span>
  <span class="conf {{p.confidence}}">{{p.confidence}} confidence</span>
  <span class="muted">{{p.fingerprint}} · first seen {{p.first_seen[:10]}}{% if p.decided_at %} · decided {{p.decided_at[:16]}} by {{p.decided_by}}{% endif %}</span>
 </div>
 <h3>{{p.title}}</h3>
 <div class="sum">{{p.summary}}</div>
 {% set ev=p.evidence %}
 {% if ev.reasons %}<ul class="reasons">{% for r in ev.reasons %}<li>{{r}}</li>{% endfor %}</ul>{% endif %}

 {% set loc=ev.location %}
 {% set acct=ev.matched_account or ev.this_record or ev.account %}
 {% if loc or acct %}
 <table class="cmp"><tr><th>field</th>{% if loc %}<th>website <a class="acct" href="{{loc.url}}" target="_blank">{{loc.slug}}</a></th>{% endif %}{% if acct %}<th>CRM <a class="acct" href="{{crm_ui}}/{{acct.account_id}}" target="_blank">{{acct.account_id}}</a></th>{% endif %}</tr>
  {% for f,lk,ak in [('name','name','name'),('street','street','billing_street'),('city','city','billing_city'),('state','state','billing_state'),('zip','zip','billing_zip'),('care','care_offerings','care_type'),('phone','phone','phone')] %}
  {% set lv = ((loc[lk]|join(', ')) if lk=='care_offerings' else loc[lk]) if loc else '' %}{% set av = acct[ak] if acct else '' %}
  <tr><td>{{f}}</td>{% if loc %}<td>{{lv}}</td>{% endif %}{% if acct %}<td class="{{'diff' if (loc and (lv|string) != (av|string) and f!='care')}}">{{av}}</td>{% endif %}</tr>
  {% endfor %}
  {% if acct %}
  <tr><td>parent</td>{% if loc %}<td>Bellhaven Senior Living</td>{% endif %}<td class="{{'diff' if acct.parent_name and 'Bellhaven' not in acct.parent_name or not acct.parent_name}}">{{acct.parent_name or '— none —'}}</td></tr>
  <tr><td>status / billing</td>{% if loc %}<td></td>{% endif %}<td>{{acct.status}} · revenue ${{'{:,}'.format(acct.lifetime_revenue or 0)}} · AR ${{'{:,}'.format(acct.outstanding_ar or 0)}}{% if acct.chow_current_account %} · chow→{{acct.chow_current_account}}{% endif %}{% if acct.duplicate_of_account %} · dup of {{acct.duplicate_of_account}}{% endif %}</td></tr>
  {% if acct.note %}<tr><td>note</td>{% if loc %}<td></td>{% endif %}<td class="muted">{{acct.note}}</td></tr>{% endif %}
  {% endif %}
 </table>
 {% endif %}
 {% if ev.surviving_record %}<div class="kv"><b>Surviving record:</b> {{ev.surviving_record.name}} <a class="acct" href="{{crm_ui}}/{{ev.surviving_record.account_id}}" target="_blank">{{ev.surviving_record.account_id}}</a> · {{ev.surviving_record.billing_street}} · parent {{ev.surviving_record.parent_name or 'none'}} · {{ev.surviving_record.status}}</div>{% endif %}
 {% if ev.successor_record %}<div class="kv"><b>Successor record:</b> {{ev.successor_record.name}} <a class="acct" href="{{crm_ui}}/{{ev.successor_record.account_id}}" target="_blank">{{ev.successor_record.account_id}}</a> · {{ev.successor_record.billing_street}} · parent {{ev.successor_record.parent_name}}</div>{% endif %}
 {% if ev.related_records %}<div class="kv"><b>Related but not matching:</b> {% for r in ev.related_records %}{{r.name}} ({{r.billing_street}}, {{r.billing_city}} {{r.billing_state}}; parent {{r.parent_name or 'none'}}; score {{r.score}}){% if not loop.last %}; {% endif %}{% endfor %}</div>{% endif %}
 {% if ev.contacts %}<div class="kv"><b>Contacts on record:</b> {{ev.contacts|join(', ')}}</div>{% endif %}
 {% if ev.site_administrator %}<div class="kv"><b>Website administrator:</b> {{ev.site_administrator}}</div>{% endif %}
 {% if ev.retired_record %}<div class="kv"><b>Retired record:</b> {{ev.retired_record.name}} <a class="acct" href="{{crm_ui}}/{{ev.retired_record.account_id}}" target="_blank">{{ev.retired_record.account_id}}</a> · {{ev.retired_record.status}} · dup of {{ev.retired_record.duplicate_of_account}}</div>{% endif %}
 {% if ev.contact %}<div class="kv"><b>Contact:</b> {{ev.contact.name}} · {{ev.contact.title}} · {{ev.contact.email or 'no email'}} · {{ev.contact.phone or 'no phone'}} · {{'active' if ev.contact.is_active else 'inactive'}} <span class="muted">{{ev.contact.contact_id}}</span></div>{% endif %}
 {% if ev.crm_contacts is defined %}<table class="cmp" style="width:auto"><tr><th>CRM contacts on {{'survivor' if ev.retired_record else 'account'}}</th><th>title</th><th>email</th><th>active</th></tr>
  {% for c in ev.crm_contacts %}<tr><td>{{c.name}}</td><td>{{c.title}}</td><td>{{c.email or ''}}</td><td>{{'yes' if c.is_active else 'no'}}</td></tr>{% else %}<tr><td colspan="4" class="muted">none</td></tr>{% endfor %}</table>{% endif %}
 {% if ev.website_check %}<div class="kv">{{ev.website_check}}</div>{% endif %}
 {% if ev.history %}<div class="flash" style="margin:8px 0">Drift: {{ev.history}}{% if p.reopened %} (re-opened {{p.reopened}}x){% endif %}</div>{% endif %}
 {% if ev.breakdown %}<div class="muted">match score {{ev.score}} · {% for k,v in ev.breakdown.items() %}{{k}}={{v}} {% endfor %}</div>{% endif %}
 <details><summary>API actions that will run on approval</summary><pre>{{p.actions|tojson(indent=1)}}</pre></details>
 {% if p.result %}<div class="result {{'' if p.result.ok else 'err'}}">{% if p.result.ok %}Applied.{% if p.result.created_account_id %} Created account <a class="acct" href="{{crm_ui}}/{{p.result.created_account_id}}" target="_blank">{{p.result.created_account_id}}</a>.{% endif %}{% for l in p.result.log %}{% if l.op=='create_contact' %} Created contact {{l.name}} ({{l.contact_id}}).{% endif %}{% endfor %}{% else %}{{p.result.error}}{% endif %}</div>{% endif %}
 {% if p.status in ('pending','failed','stale') %}
 <div class="act">
  <button class="ok" formaction="{{url_for('decide',fp=p.fingerprint,decision='approve')}}" formmethod="post" onclick="return confirm('Apply this change to the CRM now?')">{{'Retry' if p.status=='failed' else 'Approve & apply'}}</button>
  <button class="no" formaction="{{url_for('decide',fp=p.fingerprint,decision='reject')}}" formmethod="post">Reject</button>
 </div>
 {% endif %}
</div>
{% else %}
<p class="muted">Nothing here.</p>
{% endfor %}
</form>
</div></body></html>
"""


def _crm_ui():
    return config.CRM_BASE.replace("/api/v1", "") + f"/crm/{config.CRM_TOKEN}/accounts"


@app.route("/")
def index():
    status = request.args.get("status", "pending")
    kind = request.args.get("kind") or None
    all_in_status = store.list(status=status)
    kind_counts = {}
    for p in all_in_status:
        kind_counts[p["kind"]] = kind_counts.get(p["kind"], 0) + 1
    items = [p for p in all_in_status if not kind or p["kind"] == kind]
    items.sort(key=lambda p: (KIND_ORDER.index(p["kind"]) if p["kind"] in KIND_ORDER else 99, p["title"]))
    runs = store.runs(1)
    last = runs[0]["finished"] if runs and runs[0]["finished"] else "never"
    return render_template_string(PAGE, items=items, status=status, kind=kind, counts=store.counts(),
                                  kinds=[k for k in KIND_ORDER if k in kind_counts], kind_counts=kind_counts,
                                  labels=KIND_LABELS, crm_ui=_crm_ui(), last_run=last,
                                  by=request.cookies.get("reviewer", "reviewer"))


def _apply_or_reject(fp: str, decision: str, by: str):
    p = store.get(fp)
    if not p:
        return "unknown proposal"
    if decision == "reject":
        store.set_status(fp, "rejected", by=by)
        return f"Rejected: {p['title']}"
    res = apply_proposal(store, CRM(), fp, by=by)
    if res.get("ok"):
        extra = f" (created {res['created_account_id']})" if res.get("created_account_id") else ""
        return f"Applied: {p['title']}{extra}"
    return f"FAILED: {p['title']} — {res.get('error')}"


@app.post("/decide/<fp>/<decision>")
def decide(fp, decision):
    by = request.form.get("by") or request.cookies.get("reviewer", "reviewer")
    flash(_apply_or_reject(fp, decision, by))
    return redirect(request.referrer or url_for("index"))


@app.post("/bulk")
def bulk():
    by = request.form.get("by") or "reviewer"
    fps = request.form.getlist("fp")
    decision = request.form.get("decision")
    if not fps:
        flash("Nothing selected.")
    for fp in fps:
        flash(_apply_or_reject(fp, decision, by))
    resp = redirect(url_for("index", status=request.form.get("status", "pending"), kind=request.form.get("kind") or None))
    resp.set_cookie("reviewer", by)
    return resp


@app.post("/run")
def run_now():
    proc = subprocess.run([sys.executable, str(ROOT / "run_pipeline.py"), "--quiet"], capture_output=True, text=True,
                          cwd=str(ROOT), timeout=600)
    if proc.returncode == 0:
        runs = store.runs(1)
        q = json.loads(runs[0]["summary"]).get("queue", {}) if runs else {}
        flash(f"Pipeline finished. Queue: {q}")
    else:
        flash("Pipeline failed: " + (proc.stderr or proc.stdout)[-800:])
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False)
