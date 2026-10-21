#!/usr/bin/env python3
"""Render a self-contained dashboard from curated tracks and SMB mappings."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path


GROUP_LABELS = {
    "chip_design": "芯片设计",
    "semiconductor_mfg": "半导体制造",
    "display_mfg": "新型显示",
    "digital_hardware": "数字与通信硬件",
    "aerospace": "航空航天与船舶",
    "industrial_equipment": "高端装备与仪器",
    "vehicle": "智能汽车",
    "battery_energy": "新能源与电池",
    "life_science": "生命科学",
    "advanced_material": "先进材料",
}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def group_map() -> dict[str, str]:
    from curate_smb_product_mappings import GROUPS
    return {name: GROUP_LABELS[group] for group, names in GROUPS.items() for name in names}


def render(tracks_path: Path, mappings_path: Path, output_path: Path) -> None:
    tracks_payload = load_json(tracks_path)
    tracks = tracks_payload["tracks"] if isinstance(tracks_payload, dict) else tracks_payload
    mapping_payload = load_json(mappings_path)
    mappings = {x["track_id"]: x["result"] for x in mapping_payload["mappings"]}
    groups = group_map()
    missing = [x["name"] for x in tracks if x["track_id"] not in mappings]
    if missing:
        raise ValueError(f"Missing SMB mappings: {missing}")
    rows = []
    for track in tracks:
        row = {
            "track_id": track["track_id"],
            "name": track["name"],
            "definition": track["definition"],
            "included_activities": track.get("included_activities", []),
            "excluded_activities": track.get("excluded_activities", []),
            "observable_company_features": track.get("observable_company_features", []),
            "evidence_grade": track.get("evidence_grade"),
            "evidence_source_count": track.get("evidence_source_count", 0),
            "group": groups[track["name"]],
            "smb_analysis": mappings[track["track_id"]],
        }
        rows.append(row)
    payload = {
        "metadata": {
            "track_count": len(rows),
            "opportunity_count": sum(len(x["smb_analysis"]["product_opportunities"]) for x in rows),
            "group_count": len(set(x["group"] for x in rows)),
            "mapping_method": mapping_payload["metadata"].get("method"),
        },
        "tracks": rows,
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    title = "制造业热门赛道与联想 SMB 产品映射"
    template = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title><style>
:root{--bg:#f3f5f2;--panel:#fff;--ink:#17201c;--muted:#66716b;--line:#dce2de;--green:#174f3a;--mint:#dff1e8;--amber:#9a5b12;--blue:#285b78;--shadow:0 14px 40px rgba(24,45,35,.08)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.65 "Microsoft YaHei",system-ui,sans-serif}button,input,select{font:inherit}.hero{background:linear-gradient(125deg,#133f31,#1d6550 58%,#b6d6c4);color:#fff;padding:42px max(28px,calc((100vw - 1380px)/2)) 34px}.eyebrow{font-size:12px;letter-spacing:.18em;opacity:.75}.hero h1{margin:8px 0 7px;font-size:clamp(26px,4vw,46px);line-height:1.18}.hero p{margin:0;max-width:760px;color:#d9ebe3}.stats{display:flex;gap:28px;margin-top:28px}.stat b{display:block;font:700 28px/1.1 Georgia,serif}.stat span{font-size:12px;color:#c5ded3}.toolbar{position:sticky;top:0;z-index:5;background:rgba(243,245,242,.94);backdrop-filter:blur(12px);border-bottom:1px solid var(--line);padding:14px max(24px,calc((100vw - 1380px)/2));display:grid;grid-template-columns:minmax(250px,1fr) 210px 170px auto;gap:10px}.toolbar input,.toolbar select{border:1px solid var(--line);background:#fff;border-radius:9px;padding:10px 12px;color:var(--ink)}.count{align-self:center;text-align:right;color:var(--muted)}main{max-width:1380px;margin:0 auto;padding:24px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(370px,1fr));gap:16px}.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;box-shadow:var(--shadow);cursor:pointer;transition:.18s}.card:hover{transform:translateY(-2px);border-color:#9db7aa}.card-top{display:flex;justify-content:space-between;gap:12px}.badge{display:inline-flex;background:var(--mint);color:var(--green);border-radius:999px;padding:3px 9px;font-size:12px}.grade{font-weight:700;color:var(--blue)}h2{font-size:19px;margin:11px 0 8px}.definition{color:#47534d;min-height:46px}.mini{display:flex;gap:14px;border-top:1px solid var(--line);margin-top:14px;padding-top:13px;color:var(--muted)}.mini b{color:var(--ink)}.products{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}.product{font-size:12px;background:#f0f3f1;border-radius:6px;padding:3px 7px}.empty{padding:70px;text-align:center;color:var(--muted)}dialog{border:0;border-radius:16px;padding:0;width:min(1040px,calc(100vw - 30px));max-height:90vh;box-shadow:0 28px 90px #15271f55}dialog::backdrop{background:#13251e99}.modal-head{position:sticky;top:0;background:#fff;border-bottom:1px solid var(--line);padding:22px 26px;display:flex;justify-content:space-between;z-index:2}.modal-head h2{margin:0}.close{border:0;background:#edf1ee;border-radius:50%;width:36px;height:36px;cursor:pointer}.modal-body{padding:24px 26px}.section{margin-bottom:26px}.section h3{font-size:15px;margin:0 0 10px;color:var(--green)}.chips{display:flex;flex-wrap:wrap;gap:7px}.chip{border:1px solid var(--line);border-radius:999px;padding:4px 9px}.cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}.opp{border:1px solid var(--line);border-radius:11px;padding:15px;margin-bottom:10px}.opp-title{display:flex;justify-content:space-between;gap:10px;font-weight:700}.relation{font-size:11px;color:var(--blue)}.opp p{margin:8px 0;color:#46524c}.label{font-size:12px;color:var(--muted)}.rule{padding:10px 12px;background:#f5f7f5;border-radius:8px;margin:7px 0}.direct{color:var(--green)}.enabler{color:var(--amber)}@media(max-width:760px){.toolbar{grid-template-columns:1fr 1fr}.toolbar input{grid-column:1/-1}.count{text-align:left}.grid{grid-template-columns:1fr}.cols{grid-template-columns:1fr}.stats{gap:16px}.hero{padding:30px 22px}main{padding:16px}}
</style></head><body>
<header class="hero"><div class="eyebrow">MANUFACTURING TRACK INTELLIGENCE</div><h1>__TITLE__</h1><p>面向企业赛道分类的 55 个平铺赛道，以及基于真实工作负载重新审定的联想 SMB 产品机会。</p><div class="stats"><div class="stat"><b id="sTracks"></b><span>有效赛道</span></div><div class="stat"><b id="sOpps"></b><span>产品机会</span></div><div class="stat"><b id="sGroups"></b><span>赛道类型</span></div></div></header>
<div class="toolbar"><input id="search" placeholder="搜索赛道、产品、关键词…"><select id="group"><option value="">全部赛道类型</option></select><select id="grade"><option value="">全部证据等级</option><option>A</option><option>B</option><option>C</option></select><div class="count" id="count"></div></div>
<main><div class="grid" id="grid"></div><div class="empty" id="empty" hidden>没有符合条件的赛道</div></main>
<dialog id="detail"><div class="modal-head"><div><span class="badge" id="dGroup"></span><h2 id="dName"></h2></div><button class="close" aria-label="关闭">×</button></div><div class="modal-body" id="dBody"></div></dialog>
<script id="dashboard-data" type="application/json">__DATA__</script><script>
const DATA=JSON.parse(document.getElementById('dashboard-data').textContent),tracks=DATA.tracks;
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
$('#sTracks').textContent=DATA.metadata.track_count;$('#sOpps').textContent=DATA.metadata.opportunity_count;$('#sGroups').textContent=DATA.metadata.group_count;
[...new Set(tracks.map(x=>x.group))].sort().forEach(x=>$('#group').insertAdjacentHTML('beforeend',`<option>${esc(x)}</option>`));
function productNames(t){return [...new Set(t.smb_analysis.product_opportunities.flatMap(o=>o.catalog_items.map(x=>x.product_service)))]}
function render(){const q=$('#search').value.trim().toLowerCase(),g=$('#group').value,gr=$('#grade').value;const rows=tracks.filter(t=>(!g||t.group===g)&&(!gr||t.evidence_grade===gr)&&(!q||JSON.stringify(t).toLowerCase().includes(q)));$('#count').textContent=`显示 ${rows.length} / ${tracks.length}`;$('#empty').hidden=!!rows.length;$('#grid').innerHTML=rows.map(t=>`<article class="card" data-id="${esc(t.track_id)}"><div class="card-top"><span class="badge">${esc(t.group)}</span><span class="grade">证据 ${esc(t.evidence_grade||'—')}</span></div><h2>${esc(t.name)}</h2><div class="definition">${esc(t.definition)}</div><div class="mini"><span><b>${t.smb_analysis.product_opportunities.length}</b> 产品机会</span><span><b>${t.smb_analysis.ecosystem_needs.length}</b> 生态需求</span><span><b>${t.evidence_source_count||0}</b> 来源</span></div><div class="products">${productNames(t).map(x=>`<span class="product">${esc(x)}</span>`).join('')}</div></article>`).join('');document.querySelectorAll('.card').forEach(x=>x.onclick=()=>openTrack(x.dataset.id))}
function chips(xs){return `<div class="chips">${(xs||[]).map(x=>`<span class="chip">${esc(x)}</span>`).join('')}</div>`}
function openTrack(id){const t=tracks.find(x=>x.track_id===id),a=t.smb_analysis;$('#dGroup').textContent=t.group;$('#dName').textContent=t.name;$('#dBody').innerHTML=`<section class="section"><h3>赛道定义</h3><p>${esc(t.definition)}</p><div class="cols"><div><h3>纳入活动</h3>${chips(t.included_activities)}</div><div><h3>排除活动</h3>${chips(t.excluded_activities)}</div></div></section><section class="section"><h3>企业识别特征</h3>${chips(t.observable_company_features)}</section><section class="section"><h3>SMB 工作负载判断</h3>${chips(a.digital_workload_profile)}<p>${esc(a.analysis_summary)}</p></section><section class="section"><h3>联想 SMB 产品机会</h3>${a.product_opportunities.map(o=>`<div class="opp"><div class="opp-title"><span>${esc(o.opportunity_name)}</span><span class="relation ${o.relationship==='DIRECT_SOLUTION'?'direct':'enabler'}">${o.relationship==='DIRECT_SOLUTION'?'直接方案':'基础设施承载'} · ${esc(o.priority)}</span></div><div class="products">${o.catalog_items.map(x=>`<span class="product">${esc(x.catalog_item_id)} · ${esc(x.product_service)}</span>`).join('')}</div><p>${esc(o.fit_reason)}</p><div class="label">采购触发：${esc(o.buying_triggers.join('；'))}</div></div>`).join('')}</section><section class="section"><h3>目录外生态需求</h3>${a.ecosystem_needs.map(x=>`<div class="opp"><b>${esc(x.need)}</b><p>${esc(x.reason)}</p><div class="label">联想角色：${esc(x.possible_lenovo_role)}</div></div>`).join('')}</section>`;$('#detail').showModal()}
$('#search').oninput=render;$('#group').onchange=render;$('#grade').onchange=render;$('.close').onclick=()=>$('#detail').close();$('#detail').onclick=e=>{if(e.target===$('#detail'))$('#detail').close()};render();
</script></body></html>'''
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(template.replace("__TITLE__", html.escape(title)).replace("__DATA__", data), encoding="utf-8")
    print(f"Rendered {len(rows)} tracks to {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracks", type=Path, default=Path("data/processed/tracks_curated/tracks_classification_ready.json"))
    parser.add_argument("--mappings", type=Path, default=Path("outputs/smb_product_remapping/smb_product_mappings.json"))
    parser.add_argument("--output", type=Path, default=Path("outputs/dashboard/track_dashboard.html"))
    args = parser.parse_args()
    render(args.tracks, args.mappings, args.output)


if __name__ == "__main__":
    main()
