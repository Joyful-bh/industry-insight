# ruff: noqa: E501
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from track_insight.infrastructure.models import Event, ResearchPlan, Topic, TopicEvent
from track_insight.tracks.queries import list_tracks


def build_track_dashboard(
    session: Session, plan_id, *, output: Path = Path("reports/track_dashboard.html")
) -> dict[str, Any]:
    plan = session.get(ResearchPlan, plan_id)
    if plan is None:
        raise ValueError("Research plan not found")
    tracks = [
        track
        for track in list_tracks(session, plan_id, limit=10_000)
        if track["status"] in {"candidate", "watchlist"}
    ]
    topic_count = int(
        session.scalar(
            select(func.count(Topic.id)).where(
                Topic.research_plan_id == plan_id, Topic.status == "candidate"
            )
        )
        or 0
    )
    event_count = int(
        session.scalar(
            select(func.count(func.distinct(Event.id)))
            .join(TopicEvent, TopicEvent.event_id == Event.id)
            .join(Topic, Topic.id == TopicEvent.topic_id)
            .where(Topic.research_plan_id == plan_id, Topic.status == "candidate")
        )
        or 0
    )
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "plan": {
            "plan_id": str(plan.id),
            "regions": plan.regions,
            "industry_scopes": plan.industry_scopes,
            "start_date": plan.start_date.isoformat(),
            "end_date": plan.end_date.isoformat(),
        },
        "metrics": {
            "track_count": len(tracks),
            "candidate_count": sum(x["status"] == "candidate" for x in tracks),
            "watchlist_count": sum(x["status"] == "watchlist" for x in tracks),
            "analyzed_count": sum(x["analysis"] is not None for x in tracks),
            "topic_count": topic_count,
            "event_count": event_count,
        },
        "tracks": [_dashboard_track(track) for track in tracks],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_dashboard(payload), encoding="utf-8", newline="\n")
    return {"dashboard": str(output.resolve()), **payload["metrics"]}


def _dashboard_track(track: dict[str, Any]) -> dict[str, Any]:
    analysis = track.get("analysis") or {}
    stats = analysis.get("signal_statistics") or {}
    events = int(
        stats.get("independent_event_count")
        or len(track.get("supporting_event_ids") or [])
    )
    sources = int(stats.get("source_count") or 0)
    if events >= 5 and sources >= 3:
        heat = "高热度"
    elif events >= 2:
        heat = "活跃"
    else:
        heat = "观察"
    return {**track, "heat": heat, "event_count": events, "source_count": sources}


def render_dashboard(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return _TEMPLATE.replace("__DASHBOARD_DATA__", data)


_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>热门赛道洞察</title><style>
:root{--bg:#f4f6f8;--card:#fff;--ink:#111827;--muted:#667085;--line:#dde3ea;--red:#e2231a;--green:#17865b;--blue:#2563a8;--amber:#a15c00}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:"Microsoft YaHei","PingFang SC",sans-serif}.wrap{max-width:1500px;margin:auto;padding:32px}.hero{display:flex;justify-content:space-between;gap:24px;align-items:end}.hero h1{font-size:34px;margin:0 0 8px}.hero p{margin:0;color:var(--muted)}.scope{text-align:right;color:var(--muted);font-size:14px}.metrics{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin:26px 0}.metric,.panel,.track-card{background:var(--card);border:1px solid var(--line);border-radius:12px}.metric{padding:18px}.metric b{display:block;font-size:27px}.metric span{color:var(--muted);font-size:13px}.panel{overflow:hidden}.toolbar{display:flex;gap:12px;padding:16px;border-bottom:1px solid var(--line)}input,select{border:1px solid #cbd3dc;border-radius:8px;padding:9px 12px;background:#fff}input{flex:1}.table{width:100%;border-collapse:collapse}.table th,.table td{text-align:left;padding:15px;border-bottom:1px solid var(--line);vertical-align:top}.table th{background:#eef1f4;font-size:13px}.rank{color:var(--red);font-size:22px;font-weight:800}.name{font-weight:700}.badge{display:inline-block;border-radius:999px;padding:4px 9px;font-size:12px;font-weight:700}.高热度{background:#e5f5ed;color:var(--green)}.活跃{background:#e8f0fa;color:var(--blue)}.观察{background:#fff2dc;color:var(--amber)}.detail{margin-top:28px}.detail h2{font-size:24px}.track-card{padding:24px;margin:16px 0}.track-head{display:flex;justify-content:space-between;gap:20px}.track-head h3{margin:0 0 8px;font-size:23px}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:18px}.block h4{margin:0 0 8px;font-size:15px}.block p{margin:0;line-height:1.75}.chips{display:flex;flex-wrap:wrap;gap:7px}.chip{background:#eef2f6;border-radius:6px;padding:5px 8px;font-size:13px}.needs{width:100%;border-collapse:collapse}.needs td,.needs th{border:1px solid var(--line);padding:9px;text-align:left}.empty{padding:36px;text-align:center;color:var(--muted)}@media(max-width:900px){.metrics{grid-template-columns:repeat(2,1fr)}.grid{grid-template-columns:1fr}.wrap{padding:18px}.table{font-size:13px}.scope{display:none}}
</style></head><body><main class="wrap"><section class="hero"><div><h1>🔥 热门赛道总览</h1><p>比较赛道热度、企业定位与 SMB 潜在需求</p></div><div class="scope" id="scope"></div></section><section class="metrics" id="metrics"></section><section class="panel"><div class="toolbar"><input id="search" placeholder="搜索赛道、产品或企业类型"><select id="status"><option value="">全部状态</option><option value="candidate">候选</option><option value="watchlist">观察中</option></select></div><div id="table"></div></section><section class="detail"><h2>赛道解读与分析</h2><div id="details"></div></section></main><script>
const DATA=__DASHBOARD_DATA__;const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const arr=v=>Array.isArray(v)?v:[];const join=v=>arr(v).map(x=>`<span class="chip">${esc(x)}</span>`).join('');
document.getElementById('scope').innerHTML=`${esc(DATA.plan.regions.join('、'))} · ${esc(DATA.plan.industry_scopes.join('、'))}<br>${esc(DATA.plan.start_date)} 至 ${esc(DATA.plan.end_date)}`;
const labels=[['track_count','赛道'],['candidate_count','候选'],['watchlist_count','观察中'],['analyzed_count','已分析'],['topic_count','Topic'],['event_count','Event']];document.getElementById('metrics').innerHTML=labels.map(([k,l])=>`<div class="metric"><b>${DATA.metrics[k]}</b><span>${l}</span></div>`).join('');
function filtered(){const q=document.getElementById('search').value.trim().toLowerCase(),s=document.getElementById('status').value;return DATA.tracks.filter(t=>(!s||t.status===s)&&(!q||JSON.stringify(t).toLowerCase().includes(q)))}
function render(){const tracks=filtered();document.getElementById('table').innerHTML=tracks.length?`<table class="table"><thead><tr><th>序号</th><th>赛道</th><th>热度</th><th>赛道定位</th><th>Event</th><th>来源</th><th>状态</th></tr></thead><tbody>${tracks.map((t,i)=>`<tr><td class="rank">${String(i+1).padStart(2,'0')}</td><td class="name">${esc(t.name)}</td><td><span class="badge ${t.heat}">${t.heat}</span></td><td>${esc(t.definition)}</td><td>${t.event_count}</td><td>${t.source_count}</td><td>${t.status==='candidate'?'候选':'观察中'}</td></tr>`).join('')}</tbody></table>`:'<div class="empty">没有符合条件的赛道</div>';
document.getElementById('details').innerHTML=tracks.map(t=>{const a=t.analysis||{},needs=arr(a.smb_value_analysis);return `<article class="track-card"><div class="track-head"><div><h3>${esc(t.name)}</h3><div class="muted">${esc(t.definition)}</div></div><span class="badge ${t.heat}">${t.heat}</span></div><div class="grid"><div class="block"><h4>企业集合</h4><p>${esc(a.enterprise_archetype||t.enterprise_archetype||'待分析')}</p></div><div class="block"><h4>近期变化</h4><p>${esc(a.why_now||'待分析')}</p></div><div class="block"><h4>核心产品与服务</h4><div class="chips">${join(a.core_products_services||t.core_products_services)}</div></div><div class="block"><h4>企业类型</h4><div class="chips">${join(a.core_company_types||t.core_company_types)}</div></div><div class="block"><h4>可观察特征</h4><div class="chips">${join(a.observable_company_features||t.observable_company_features)}</div></div><div class="block"><h4>关联 Topic</h4><div class="chips">${join(arr(t.topics).map(x=>x.label))}</div></div></div>${needs.length?`<h4>SMB 需求与覆盖路径</h4><table class="needs"><tr><th>经营活动</th><th>工作负载</th><th>IT 需求</th><th>覆盖路径</th></tr>${needs.map(n=>`<tr><td>${esc(n.business_activity)}</td><td>${esc(n.workload)}</td><td>${esc(n.it_need)}</td><td>${esc(n.coverage_path)}</td></tr>`).join('')}</table>`:''}${arr(a.uncertainties).length?`<p class="muted">不确定性：${esc(a.uncertainties.join('；'))}</p>`:''}</article>`}).join('')||'<div class="empty">没有赛道详情</div>'}
document.getElementById('search').addEventListener('input',render);document.getElementById('status').addEventListener('change',render);render();
</script></body></html>'''
