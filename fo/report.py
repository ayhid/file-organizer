#!/usr/bin/env python3
"""Step 9. Human friendly HTML report of the plan (one self-contained page, no network).

Writes reports/report-YYYY-MM-DD.html and reports/report.html (latest). Each row of the plan
is a line of the table; clicking it opens every detail: full source and destination, rule,
reason, dates, unit content, duplicate group.

  --notes FILE          short text shown at the top (paragraphs and "- " bullets), e.g. the
                        notes Claude writes after reviewing the flagged rows
  --path-alias FROM=TO  rewrite path prefixes in what is displayed (repeatable), so a report
                        built through a mount shows the paths of the real machine
  --repo-dir DIR        repository path used in the commands at the end (default: this repo)

"New this week" and "decided this week" compare with the previous report. Several reports on
the same day share the same baseline, so re-running the routine after a review keeps the
weekly view.
"""
from __future__ import annotations
import argparse
import collections as C
import datetime as dt
import html
import json
import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import REPO_DIR, Run, human, meta_get, meta_set, now_iso, setup  # noqa: E402

REASONS_FR = {
    "no rule matched": "aucune règle ne correspond",
    "no date found": "aucune date trouvée",
    "meaningless name": "nom sans signification",
    "mixed folder: choose a place": "dossier mixte : choisir un emplacement",
    "no installable font: moved instead": "aucune police installable : déplacé à la place",
    "identical content": "contenu identique",
    "credentials: move to a password manager or a secure location":
        "identifiants : à ranger dans un gestionnaire de mots de passe ou un endroit sûr",
}
ACTIONS_FR = {"move": "déplacer", "delete": "à supprimer", "skip": "ignorer", "install": "installer"}


def add_args(ap):
    ap.add_argument("--notes", help="text file shown at the top of the report")
    ap.add_argument("--path-alias", action="append", default=[], metavar="FROM=TO",
                    help="rewrite a path prefix in displayed paths (repeatable)")
    ap.add_argument("--repo-dir", help="repository path shown in the commands")
    ap.add_argument("--today", help=argparse.SUPPRESS)  # tests: fix the report date


def parse_aliases(items: list[str]) -> list[tuple[str, str]]:
    out = []
    for it in items:
        if "=" not in it:
            sys.exit(f"--path-alias needs FROM=TO, got {it!r}")
        a, b = it.split("=", 1)
        if a:
            out.append((a.rstrip("/") or "/", b.rstrip("/")))
    return sorted(out, key=lambda x: -len(x[0]))  # longest prefix first


def alias(path: str | os.PathLike | None, aliases: list[tuple[str, str]]) -> str:
    if path is None:
        return ""
    p = str(path)
    for a, b in aliases:
        if p == a or p.startswith(a + "/"):
            return b + p[len(a):]
    home = os.path.expanduser("~")
    if p == home or p.startswith(home + "/"):
        return "~" + p[len(home):]
    return p


def translate_reason(reason: str | None) -> str:
    if not reason:
        return ""
    parts = [r.strip() for r in reason.split(";") if r.strip()]
    return " ; ".join(REASONS_FR.get(r, r) for r in parts)


def status_of(r, since: str) -> str:
    """One label per row, used for the badge and the filters."""
    reason = r["reason"] or ""
    if r["target_dir"] == "_sensitive" or reason.startswith("credentials"):
        return "sensitive"
    if r["needs_review"]:
        return "review"
    if r["locked"] and since and (r["updated_at"] or "") > since:
        return "decided"
    if r["action"] == "delete":
        return "delete"
    if r["action"] == "install":
        return "install"
    return "ready"


def notes_html(path: str | None) -> str:
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return ""
    out, items = [], []

    def flush():
        if items:
            out.append("<ul>" + "".join(f"<li>{html.escape(i)}</li>" for i in items) + "</ul>")
            items.clear()

    for block in text.replace("\r\n", "\n").split("\n\n"):
        lines = [l.rstrip() for l in block.split("\n") if l.strip()]
        para = []
        for l in lines:
            s = l.strip()
            if s.startswith(("- ", "* ")):
                if para:
                    out.append(f"<p>{html.escape(' '.join(para))}</p>")
                    para = []
                items.append(s[2:].strip())
            elif s.startswith("#"):
                flush()
                out.append(f"<h3>{html.escape(s.lstrip('#').strip())}</h3>")
            else:
                flush()
                para.append(s)
        flush()
        if para:
            out.append(f"<p>{html.escape(' '.join(para))}</p>")
    return "\n".join(out)


def baseline(db, today: str) -> dict:
    """Previous report's marks. Same-day re-runs reuse the baseline of the first run that day."""
    cur = json.loads(meta_get(db, "report_cur") or "null")
    if cur and cur.get("date") == today:
        base = json.loads(meta_get(db, "report_prev") or "null")
    else:
        base = cur
        meta_set(db, "report_prev", json.dumps(base))
    return base or {}


def collect(db, aliases, since: str, base_file_id: int, target_root: str):
    units = {u["id"]: u for u in db.execute("SELECT * FROM units")}
    files = {f["id"]: f for f in db.execute("SELECT id, path, size, mtime FROM files")}
    # duplicate groups by member
    members = C.defaultdict(list)
    item_group = {}
    for m in db.execute("SELECT m.group_id, m.item_type, m.item_id, m.role, m.reason, g.kind gkind, g.size gsize "
                        "FROM dup_members m JOIN dup_groups g ON g.id=m.group_id"):
        members[m["group_id"]].append(m)
        item_group[(m["item_type"], m["item_id"])] = m["group_id"]

    def item_path(itype, iid, reason=None):
        if itype == "unit" and iid in units:
            return units[iid]["root"]
        if itype == "file" and iid in files:
            return files[iid]["path"]
        return reason or "?"

    rows = []
    for r in db.execute("SELECT * FROM plan WHERE status='pending' ORDER BY needs_review DESC, target_dir, src"):
        key = (r["item_type"], r["item_id"])
        if r["item_type"] == "unit":
            u = units.get(r["item_id"])
            size = u["size"] if u else 0
            mtime = None
            unit = {"kind": u["kind"], "files": u["file_count"], "why": u["reason"] or ""} if u else None
            new = False
        else:
            f = files.get(r["item_id"])
            size = f["size"] if f else 0
            mtime = f["mtime"] if f else None
            unit = None
            new = bool(base_file_id) and r["item_id"] > base_file_id
        name = r["new_name"] or r["src"].rsplit("/", 1)[-1]
        dest = "/".join(p for p in (r["target_dir"], name) if p)
        dup = None
        gid = item_group.get(key)
        if gid is not None:
            ms = members[gid]
            dup = {"kind": ms[0]["gkind"], "size": human(ms[0]["gsize"]),
                   "members": [{"role": m["role"], "path": item_path(m["item_type"], m["item_id"], m["reason"]),
                                "self": (m["item_type"], m["item_id"]) == key} for m in ms]}
        rows.append({
            "id": r["id"], "st": status_of(r, since), "new": new,
            "src": r["src"], "name": r["src"].rsplit("/", 1)[-1], "dest": dest,
            "abs": alias(os.path.join(target_root, dest), aliases) if target_root else dest,
            "top": (r["target_dir"] or "").split("/")[0],
            "action": r["action"], "type": r["item_type"], "family": r["family"] or "",
            "rule": r["rule"] or "", "reason": translate_reason(r["reason"]),
            "date": r["date_found"] or "", "dsrc": r["date_source"] or "",
            "size": size, "hsize": human(size),
            "mtime": dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else "",
            "locked": bool(r["locked"]), "unit": unit, "dup": dup,
        })
    return rows


def main():
    ctx = setup(__doc__, add_args)
    db, a = ctx.db, ctx.args
    aliases = parse_aliases(a.path_alias)
    today = a.today or dt.date.today().isoformat()

    with Run(ctx, "report") as run:
        base = baseline(db, today)
        since = base.get("at", "")
        base_file_id = int(base.get("file_id") or 0)

        tot = db.execute("SELECT COUNT(*) n, COALESCE(SUM(size),0) s, COALESCE(MAX(id),0) mx FROM files "
                         "WHERE missing=0").fetchone()
        gone = [r["path"] for r in db.execute("SELECT path FROM files WHERE missing=1 ORDER BY path")]
        dg = db.execute("SELECT COUNT(*) g, COALESCE(SUM(members-1),0) x, COALESCE(SUM(wasted),0) w "
                        "FROM dup_groups").fetchone()
        rows = collect(db, aliases, since, base_file_id, str(ctx.target) if ctx.target else "")

        st = C.Counter(r["st"] for r in rows)
        by_top = C.Counter(r["top"] for r in rows if r["action"] in ("move", "install"))
        new_count = sum(1 for r in rows if r["new"])
        wasted_pct = f"{dg['w'] / tot['s'] * 100:.1f} %" if tot["s"] else "0 %"

        src_d = alias(ctx.source, aliases)
        tgt_d = alias(ctx.target, aliases)
        flags = " ".join(f"--{k} {shlex.quote(v)}" if " " in v else f"--{k} {v}" for k, v in (
            ("source", src_d), ("target", tgt_d), ("state", alias(ctx.state, aliases)),
            ("local-rules", alias(a.local_rules or "~/.file-organizer/rules.local.toml", aliases))))
        repo = alias(a.repo_dir or REPO_DIR, aliases)
        commands = [
            ("Simulation (ne touche à rien)", f"cd {repo} && python3 fo/apply.py {flags}"),
            ("Appliquer le plan", f"cd {repo} && python3 fo/apply.py {flags} --apply"),
            ("Annuler la dernière exécution", f"cd {repo} && python3 fo/undo.py --last --yes {flags}"),
        ]

        data = {
            "generated": now_iso(), "today": today, "source": src_d, "target": tgt_d,
            "since": since[:10] if since else "",
            "kpi": {"files": tot["n"], "size": human(tot["s"]), "new": new_count, "gone": len(gone),
                    "dup_groups": dg["g"], "dup_extra": dg["x"], "wasted": human(dg["w"]), "wasted_pct": wasted_pct,
                    "planned": len(rows), "review": st["review"], "decided": st["decided"],
                    "sensitive": st["sensitive"], "install": st["install"], "delete": st["delete"]},
            "tops": by_top.most_common(), "gone": gone[:500], "gone_more": max(0, len(gone) - 500),
            "rows": rows, "commands": commands,
        }
        page = render(data, notes_html(a.notes))
        ctx.reports.mkdir(parents=True, exist_ok=True)
        dated = ctx.reports / f"report-{today}.html"
        for p in (dated, ctx.reports / "report.html"):
            p.write_text(page, encoding="utf-8")

        meta_set(db, "report_cur", json.dumps({"date": today, "at": now_iso(), "file_id": tot["mx"]}))
        db.commit()
        run.notes = f"rows={len(rows)} review={st['review']}"
        print(f"report: {len(rows)} rows ({st['review']} to review, {st['decided']} decided this week) -> {dated}")


def render(data: dict, notes: str) -> str:
    # JSON inside <script>: "</" and the two JS line separators must be escaped; em dashes in
    # file names are escaped too, so the page text never shows one while the data stays exact
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    for ch in ("\u2028", "\u2029", "\u2014"):
        payload = payload.replace(ch, "\\u%04x" % ord(ch))
    notes = notes.replace("\u2014", "-")  # house style: never an em dash
    return TEMPLATE.replace("/*DATA*/", payload).replace("<!--NOTES-->", notes) \
        .replace("{{TODAY}}", html.escape(data["today"]))


TEMPLATE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rangement Downloads {{TODAY}}</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--ink:#1d1c1a;--mute:#6b6862;--line:#e4e1da;--soft:#efede8;
--accent:#2f5d50;--review:#b3541e;--review-bg:#fbeee5;--decided:#2f5d50;--decided-bg:#e6f0ec;
--del:#8a3b3b;--del-bg:#f6e8e8;--font:#4b4f9c;--font-bg:#ebecf8;--sens:#7a2e6b;--sens-bg:#f5e6f1;
--ready:#55534e;--ready-bg:#efede8;--new:#1f6fb2}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#171716;--card:#201f1e;--ink:#ecebe7;
--mute:#a09d96;--line:#33312e;--soft:#2a2927;--accent:#7fbfa9;--review:#f0a070;--review-bg:#3a2a1f;
--decided:#8fd0b8;--decided-bg:#1f332c;--del:#e59a9a;--del-bg:#3a2424;--font:#a9adf0;--font-bg:#26283f;
--sens:#e2a0d3;--sens-bg:#3a2235;--ready:#c9c6bf;--ready-bg:#2a2927;--new:#7ab8ef}}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:28px 16px 64px}
header h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
header p{margin:0;color:var(--mute)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:22px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.kpi b{display:block;font-size:22px;font-variant-numeric:tabular-nums}
.kpi span{color:var(--mute);font-size:13px}
.kpi.hot b{color:var(--review)}
section{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin:16px 0}
section h2{font-size:17px;margin:0 0 12px}
.notes p,.notes ul{margin:6px 0}.notes h3{font-size:15px;margin:12px 0 4px}
.tops{display:grid;gap:6px}
.top{display:grid;grid-template-columns:160px 1fr 56px;gap:10px;align-items:center;font-size:14px}
.bar{height:8px;border-radius:4px;background:var(--soft);overflow:hidden}.bar i{display:block;height:100%;background:var(--accent)}
.top em{font-style:normal;text-align:right;color:var(--mute);font-variant-numeric:tabular-nums}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
.chip{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:5px 12px;font:inherit;font-size:13px;cursor:pointer}
.chip[aria-pressed="true"]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.chip small{opacity:.7;margin-left:4px}
input[type=search],select{font:inherit;font-size:14px;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
input[type=search]{flex:1;min-width:200px}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;font-weight:600;color:var(--mute);font-size:12px;text-transform:uppercase;letter-spacing:.04em;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr.row{cursor:pointer}tr.row:hover td{background:var(--soft)}
tr.row td:first-child{width:118px}
.name{font-weight:550;word-break:break-word}.path{color:var(--mute);font-size:12.5px;word-break:break-all}
.arrow{color:var(--mute);margin-right:4px}
td.size{text-align:right;white-space:nowrap;color:var(--mute);font-variant-numeric:tabular-nums}
.badge{display:inline-block;border-radius:6px;padding:2px 8px;font-size:12px;font-weight:600;white-space:nowrap}
.b-review{color:var(--review);background:var(--review-bg)}.b-decided{color:var(--decided);background:var(--decided-bg)}
.b-delete{color:var(--del);background:var(--del-bg)}.b-install{color:var(--font);background:var(--font-bg)}
.b-sensitive{color:var(--sens);background:var(--sens-bg)}.b-ready{color:var(--ready);background:var(--ready-bg)}
.new{color:var(--new);font-size:11px;font-weight:700;margin-left:6px;text-transform:uppercase}
tr.detail td{background:var(--soft);padding:14px 16px}
dl{display:grid;grid-template-columns:170px 1fr;gap:6px 14px;margin:0;font-size:13.5px}
dt{color:var(--mute)}dd{margin:0;word-break:break-all}
.dups{margin:4px 0 0;padding:0;list-style:none}.dups li{margin:2px 0}
.dups .keep{color:var(--decided);font-weight:600}.dups .remove{color:var(--del);font-weight:600}
.dups .me{text-decoration:underline}
.more{display:block;margin:14px auto 0;padding:8px 18px}
.empty{color:var(--mute);padding:18px 8px}
.cmd{display:flex;gap:8px;align-items:flex-start;margin:8px 0}
.cmd div{flex:1}.cmd p{margin:0 0 4px;font-size:13px;color:var(--mute)}
code{display:block;background:var(--soft);border-radius:8px;padding:9px 11px;font:12.5px/1.5 ui-monospace,Menlo,monospace;word-break:break-all;white-space:pre-wrap}
.copy{margin-top:20px}
details summary{cursor:pointer;color:var(--mute)}
@media (max-width:720px){thead{display:none}table,tbody,tr,td{display:block;width:100%}
tr.row{border-bottom:1px solid var(--line);padding:6px 0}tr.row td{border:0;padding:3px 4px}
tr.row td:first-child{width:auto}td.size{text-align:left}dl{grid-template-columns:1fr}dt{margin-top:6px}
.top{grid-template-columns:110px 1fr 44px}}
</style>
</head>
<body>
<main>
<header>
  <h1>Rangement de Downloads</h1>
  <p id="sub"></p>
</header>
<div class="kpis" id="kpis"></div>
<section class="notes" id="notes"><h2>Notes de la revue</h2><!--NOTES--></section>
<section>
  <h2>Plan</h2>
  <div class="toolbar">
    <div id="chips" style="display:flex;flex-wrap:wrap;gap:6px"></div>
    <input type="search" id="q" placeholder="Rechercher un nom, un dossier, une règle...">
    <select id="top"><option value="">Tous les dossiers</option></select>
  </div>
  <table>
    <thead><tr><th>Statut</th><th>Fichier</th><th>Destination</th><th style="text-align:right">Taille</th></tr></thead>
    <tbody id="tb"></tbody>
  </table>
  <button class="chip more" id="more" hidden>Afficher plus</button>
</section>
<section><h2>Répartition par dossier de destination</h2><div class="tops" id="tops"></div></section>
<section id="gone-s" hidden><h2>Disparus depuis le dernier scan</h2><details><summary id="gone-sum"></summary><ul id="gone"></ul></details></section>
<section><h2>Commandes à lancer sur le Mac</h2><div id="cmds"></div>
<p style="color:var(--mute);font-size:13px;margin:10px 0 0">Les suppressions vont dans <b>_to_delete</b>, rien n'est effacé. Tout est annulable.</p></section>
</main>
<script id="data" type="application/json">/*DATA*/</script>
<script>
(function(){
const D=JSON.parse(document.getElementById('data').textContent);
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const LBL={review:'À revoir',decided:'Décidé',sensitive:'Sensible',delete:'Doublon',install:'Police',ready:'Prêt'};
const ACT={move:'déplacer',delete:'déplacer vers _to_delete',skip:'ignorer',install:'installer puis mettre à la corbeille'};
const k=D.kpi;
$('sub').textContent=`Rapport du ${D.today}`+(D.since?`, comparé au ${D.since}`:'')+` · ${D.source} → ${D.target}`;
const kp=[[k.files.toLocaleString('fr-FR'),'fichiers, '+k.size],[k.new,'nouveaux depuis le dernier rapport'],
[k.wasted_pct,'gaspillé en doublons ('+k.wasted+')',],[k.planned.toLocaleString('fr-FR'),'éléments planifiés'],
[k.review,'attendent ta décision',k.review>0],[k.decided,'décidés par la revue'],[k.install,'polices à installer'],
[k.sensitive,'fichiers sensibles',k.sensitive>0]];
$('kpis').innerHTML=kp.map(([v,l,h])=>`<div class="kpi${h?' hot':''}"><b>${esc(v)}</b><span>${esc(l)}</span></div>`).join('');
if(!$('notes').querySelector('p,ul,h3'))$('notes').hidden=true;
const counts={all:D.rows.length};D.rows.forEach(r=>counts[r.st]=(counts[r.st]||0)+1);
counts.new=D.rows.filter(r=>r.new).length;
const FILTERS=[['all','Tout'],['review','À revoir'],['decided','Décidés'],['sensitive','Sensibles'],['new','Nouveaux'],['delete','Doublons'],['install','Polices'],['ready','Prêts']];
let F=counts.review?'review':'all',Q='',T='',LIMIT=200,open=new Set();
$('chips').innerHTML=FILTERS.filter(([f])=>f==='all'||counts[f]).map(([f,l])=>`<button class="chip" data-f="${f}">${l}<small>${counts[f]||0}</small></button>`).join('');
$('chips').onclick=e=>{const b=e.target.closest('button');if(!b)return;F=b.dataset.f;LIMIT=200;draw()};
const tops=[...new Set(D.rows.map(r=>r.top))].filter(Boolean).sort();
$('top').innerHTML+=tops.map(t=>`<option>${esc(t)}</option>`).join('');
$('top').onchange=e=>{T=e.target.value;LIMIT=200;draw()};
let tm;$('q').oninput=e=>{clearTimeout(tm);tm=setTimeout(()=>{Q=e.target.value.toLowerCase().trim();LIMIT=200;draw()},120)};
$('more').onclick=()=>{LIMIT+=300;draw()};
function match(r){if(F==='new'?!r.new:(F!=='all'&&r.st!==F))return false;if(T&&r.top!==T)return false;
if(Q){const h=(r.src+' '+r.dest+' '+r.rule+' '+r.reason+' '+r.family).toLowerCase();if(!h.includes(Q))return false}return true}
function detail(r){const f=[['Source',r.src],['Destination',r.abs],['Action',ACT[r.action]||r.action],
['Type',r.type==='unit'?`dossier traité en bloc${r.unit?' ('+r.unit.kind+', '+r.unit.files+' fichiers)':''}`:'fichier'],
r.unit&&r.unit.why?['Pourquoi en bloc',r.unit.why]:null,['Famille',r.family],['Règle',r.rule],['Raison',r.reason],
r.date?['Date trouvée',r.date+(r.dsrc?' (via '+r.dsrc+')':'')]:null,r.mtime?['Modifié le',r.mtime]:null,['Taille',r.hsize],
['Modifié à la main',r.locked?'oui (verrouillé, jamais réécrit par le classement)':'non']].filter(x=>x&&x[1]!=='');
let h='<dl>'+f.map(([a,b])=>`<dt>${esc(a)}</dt><dd>${esc(b)}</dd>`).join('');
if(r.dup)h+=`<dt>Doublons</dt><dd>${r.dup.members.length} copies de ${esc(r.dup.size)}<ul class="dups">`+
r.dup.members.map(m=>`<li><span class="${m.role}">${m.role==='keep'?'gardé':'retiré'}</span> <span class="${m.self?'me':''}">${esc(m.path)}</span></li>`).join('')+'</ul></dd>';
return h+'</dl>'}
function draw(){[...$('chips').children].forEach(b=>b.setAttribute('aria-pressed',b.dataset.f===F));
const rows=D.rows.filter(match),tb=$('tb');
if(!rows.length){tb.innerHTML='<tr><td colspan="4" class="empty">Rien à afficher pour ce filtre.</td></tr>';$('more').hidden=true;return}
tb.innerHTML=rows.slice(0,LIMIT).map(r=>{const o=open.has(r.id);
return `<tr class="row" data-id="${r.id}" aria-expanded="${o}"><td><span class="badge b-${r.st}">${LBL[r.st]}</span></td>`+
`<td><div class="name">${esc(r.name)}${r.new?'<span class="new">nouveau</span>':''}</div><div class="path">${esc(r.src)}</div></td>`+
`<td><span class="arrow">→</span>${esc(r.dest)}${r.reason?`<div class="path">${esc(r.reason)}</div>`:''}</td>`+
`<td class="size">${esc(r.hsize)}</td></tr>`+(o?`<tr class="detail"><td colspan="4">${detail(r)}</td></tr>`:'')}).join('');
$('more').hidden=rows.length<=LIMIT;$('more').textContent=`Afficher plus (${rows.length-LIMIT} restants)`}
$('tb').onclick=e=>{const tr=e.target.closest('tr.row');if(!tr)return;const id=+tr.dataset.id;open.has(id)?open.delete(id):open.add(id);draw()};
const mx=Math.max(1,...D.tops.map(t=>t[1]));
$('tops').innerHTML=D.tops.map(([t,n])=>`<div class="top"><span>${esc(t||'(racine)')}</span><div class="bar"><i style="width:${n/mx*100}%"></i></div><em>${n}</em></div>`).join('');
if(D.gone.length){$('gone-s').hidden=false;$('gone-sum').textContent=`${k.gone} fichiers ne sont plus dans le dossier`;
$('gone').innerHTML=D.gone.map(g=>`<li>${esc(g)}</li>`).join('')+(D.gone_more?`<li>et ${D.gone_more} autres</li>`:'')}
$('cmds').innerHTML=D.commands.map(([l,c],i)=>`<div class="cmd"><div><p>${esc(l)}</p><code id="c${i}">${esc(c)}</code></div><button class="chip copy" data-c="${i}">Copier</button></div>`).join('');
$('cmds').onclick=e=>{const b=e.target.closest('button');if(!b)return;const t=$('c'+b.dataset.c).textContent;
(navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(()=>{b.textContent='Copié';setTimeout(()=>b.textContent='Copier',1500)}).catch(()=>{const r=document.createRange();r.selectNode($('c'+b.dataset.c));getSelection().removeAllRanges();getSelection().addRange(r)})};
draw();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
