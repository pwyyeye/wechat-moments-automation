from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from .schemas import AgentIdentityUpdate, SourceUpsertRequest

if TYPE_CHECKING:
    from ..app import PublisherAgentApp


def create_admin_app(agent: "PublisherAgentApp") -> FastAPI:
    app = FastAPI(
        title="WeChat Publisher Agent Local Admin",
        version="0.2.0",
        docs_url="/api/docs",
        redoc_url=None,
    )

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index():
        return ADMIN_HTML

    @app.get("/api/health")
    def health():
        return {"ok": True, "binding": "loopback-only"}

    @app.get("/api/status")
    def status():
        return agent.status()

    @app.patch("/api/identity")
    def update_identity(body: AgentIdentityUpdate):
        agent.update_identity(body.display_name, body.account_key)
        return agent.status()

    @app.get("/api/sources")
    def sources():
        return agent.source_manager.status()

    @app.post("/api/sources", status_code=201)
    def add_source(body: SourceUpsertRequest):
        try:
            agent.upsert_source(body, create_only=True)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {"saved": True, "sourceId": body.id}

    @app.put("/api/sources/{source_id}")
    def update_source(source_id: str, body: SourceUpsertRequest):
        if body.id != source_id:
            raise HTTPException(422, "source id in path and body must match")
        try:
            agent.upsert_source(body, create_only=False)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {"saved": True, "sourceId": body.id}

    @app.delete("/api/sources/{source_id}")
    def delete_source(source_id: str):
        try:
            agent.delete_source(source_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error
        return {"deleted": True, "sourceId": source_id}

    @app.post("/api/sources/{source_id}/test")
    def test_source(source_id: str):
        try:
            meta = agent.source_manager.test_connection(source_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except Exception as error:
            code = getattr(error, "code", "SOURCE_TEST_FAILED")
            raise HTTPException(502, {"code": code, "message": str(error)}) from error
        return {
            "ok": True,
            "protocol": meta.protocol,
            "versions": meta.versions,
            "sourceName": meta.source_name,
            "serverTime": meta.server_time,
        }

    @app.post("/api/preflight")
    def preflight():
        return agent.preflight()

    @app.get("/api/tasks")
    def tasks(limit: int = 50):
        return agent.ledger.recent_tasks(limit)

    @app.get("/api/outbox")
    def outbox():
        return {
            "backlog": agent.ledger.outbox_backlog(),
            "oldestAgeSeconds": agent.ledger.oldest_outbox_age_seconds(),
        }

    return app


ADMIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>朋友圈发布站</title>
  <style>
    :root{--paper:#f4f0e6;--ink:#18231f;--muted:#69736d;--line:#c8c1b3;--signal:#d84b2f;--ok:#277451;--panel:#fffdf7;--shadow:0 18px 45px #26352a1c}
    *{box-sizing:border-box}body{margin:0;color:var(--ink);font-family:"Microsoft YaHei UI","Noto Sans CJK SC",sans-serif;background:radial-gradient(circle at 8% 10%,#e4d7b8 0 9%,transparent 25%),linear-gradient(115deg,#f7f2e6,#ece9dc 62%,#dfe9df);min-height:100vh}
    body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:.23;background-image:linear-gradient(#26372c12 1px,transparent 1px),linear-gradient(90deg,#26372c12 1px,transparent 1px);background-size:28px 28px}
    main{position:relative;max-width:1180px;margin:auto;padding:38px 24px 70px}.mast{display:flex;justify-content:space-between;gap:20px;align-items:end;border-bottom:2px solid var(--ink);padding-bottom:18px}.kicker{font-size:12px;letter-spacing:.28em;text-transform:uppercase;color:var(--signal);font-weight:800}h1{font-family:"STKaiti","KaiTi",serif;font-size:clamp(38px,6vw,76px);line-height:.95;margin:8px 0}.stamp{border:1px solid var(--ink);padding:10px 14px;background:#fff9;transform:rotate(1deg);font-size:13px}
    .grid{display:grid;grid-template-columns:1.05fr 1.95fr;gap:20px;margin-top:22px}.card{background:var(--panel);border:1px solid var(--line);box-shadow:var(--shadow);padding:20px;animation:rise .45s ease both}.card:nth-child(2){animation-delay:.08s}.card:nth-child(3){animation-delay:.14s}@keyframes rise{from{opacity:0;transform:translateY(12px)}}h2{font-family:"STKaiti","KaiTi",serif;font-size:25px;margin:0 0 15px}.facts{display:grid;gap:10px}.fact{display:flex;justify-content:space-between;border-bottom:1px dashed var(--line);padding:6px 0}.value{font-family:Consolas,monospace;font-size:13px}.ok{color:var(--ok)}.bad{color:var(--signal)}
    button{border:0;background:var(--ink);color:white;padding:9px 14px;cursor:pointer;font-weight:700}button.alt{background:transparent;color:var(--ink);border:1px solid var(--ink)}button.danger{background:var(--signal)}button:disabled{opacity:.45}.toolbar{display:flex;gap:9px;flex-wrap:wrap}.sources{display:grid;gap:12px}.source{border-left:5px solid var(--line);background:#f5f0e5;padding:14px;display:grid;grid-template-columns:1fr auto;gap:8px}.source.healthy{border-color:var(--ok)}.source.auth_error,.source.incompatible{border-color:var(--signal)}.source h3{margin:0;font-size:17px}.meta{color:var(--muted);font-size:12px;word-break:break-all}.actions{display:flex;gap:6px;align-items:start}.actions button{padding:6px 9px;font-size:12px}
    dialog{width:min(680px,calc(100% - 28px));border:1px solid var(--ink);background:var(--paper);box-shadow:0 26px 70px #101b16aa;padding:0}dialog::backdrop{background:#102018a8}.dialog-head{display:flex;justify-content:space-between;padding:18px 22px;border-bottom:1px solid var(--line)}form{padding:20px;display:grid;grid-template-columns:1fr 1fr;gap:14px}label{display:grid;gap:5px;font-size:12px;font-weight:800;letter-spacing:.03em}label.wide{grid-column:1/-1}input,select,textarea{font:inherit;background:#fff;border:1px solid var(--line);padding:9px}textarea{min-height:74px}.form-actions{grid-column:1/-1;display:flex;justify-content:flex-end;gap:8px}.task-table{width:100%;border-collapse:collapse;font-size:12px}.task-table th,.task-table td{text-align:left;padding:8px;border-bottom:1px solid var(--line)}.notice{margin-top:14px;min-height:22px;color:var(--signal);font-size:13px}
    @media(max-width:800px){main{padding:22px 14px 50px}.grid{grid-template-columns:1fr}.mast{align-items:start;flex-direction:column}.stamp{transform:none}.source{grid-template-columns:1fr}.actions{flex-wrap:wrap}form{grid-template-columns:1fr}label.wide,.form-actions{grid-column:1}}
  </style>
</head>
<body><main>
  <section class="mast"><div><div class="kicker">Windows Agent / Local Only</div><h1>朋友圈发布站</h1></div><div class="stamp">只监听 127.0.0.1<br>凭据由 Windows DPAPI 保存</div></section>
  <section class="grid">
    <article class="card"><h2>本机状态</h2><div class="facts" id="facts"></div><div class="toolbar" style="margin-top:16px"><button onclick="preflight()">环境预检</button><button class="alt" onclick="refreshAll()">刷新</button></div><div class="notice" id="notice"></div></article>
    <article class="card"><div style="display:flex;justify-content:space-between;gap:12px"><h2>内容数据源</h2><button onclick="openSource()">+ 添加来源</button></div><div class="sources" id="sources"></div></article>
    <article class="card" style="grid-column:1/-1"><h2>最近任务</h2><div style="overflow:auto"><table class="task-table"><thead><tr><th>来源</th><th>任务</th><th>状态</th><th>尝试</th><th>最终点击</th><th>更新时间</th></tr></thead><tbody id="tasks"></tbody></table></div></article>
  </section>
</main>
<dialog id="sourceDialog"><div class="dialog-head"><strong id="dialogTitle">添加数据源</strong><button class="alt" onclick="sourceDialog.close()">关闭</button></div><form id="sourceForm">
  <label>来源 ID<input name="id" required pattern="[A-Za-z0-9][A-Za-z0-9._:-]*"></label><label>显示名称<input name="name" required></label>
  <label class="wide">协议 Base URL<input name="baseUrl" type="url" required placeholder="https://example.com/openapi/publisher-agent/v1"></label>
  <label>账号别名<input name="accountKey" value="wechat-main" required></label><label>权重 1-10<input name="weight" type="number" min="1" max="10" value="1" required></label>
  <label>认证类型<select name="authType"><option value="api_key_header">API Key Header</option><option value="bearer">Bearer</option></select></label><label>Header 名<input name="headerName" value="x-api-key"></label>
  <label class="wide">凭据（留空表示保持原值）<input name="credential" type="password" autocomplete="new-password"></label>
  <label class="wide">媒体域名白名单（每行一个）<textarea name="allowedHosts" placeholder="files.example.com"></textarea></label>
  <label><input name="enabled" type="checkbox" checked> 启用来源</label><label><input name="allowPrivateNetwork" type="checkbox"> 允许私网媒体</label>
  <div class="form-actions"><button type="button" class="alt" onclick="sourceDialog.close()">取消</button><button type="submit">保存并应用</button></div>
</form></dialog>
<script>
const $=s=>document.querySelector(s), api=async(path,opt={})=>{const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opt});if(!r.ok)throw new Error(JSON.stringify(await r.json()));return r.status===204?null:r.json()};let sourceData=[];
async function refreshAll(){try{const [s,t]=await Promise.all([api('/api/status'),api('/api/tasks')]);$('#facts').innerHTML=[['Agent',s.agent.displayName],['微信',s.wechat.running?'运行中':'未运行'],['登录',s.wechat.loggedIn?'已登录':'待检查'],['桌面',s.wechat.desktopUnlocked?'可交互':'已锁定'],['Worker',s.worker.active?'执行中':'空闲'],['Outbox',s.outbox.backlog+' 条'],['媒体缓存',Math.round(s.mediaCache.bytes/1024/1024)+' MiB'],['告警',s.alerts.length+' 条']].map(x=>`<div class="fact"><span>${x[0]}</span><span class="value">${x[1]}</span></div>`).join('');sourceData=s.sources;renderSources();$('#tasks').innerHTML=t.map(x=>`<tr><td>${esc(x.source_id)}</td><td>${esc(x.task_id)}</td><td>${esc(x.state)}</td><td>${x.attempt}</td><td>${x.final_click_intent_at?'已记录':'-'}</td><td>${esc(x.updated_at)}</td></tr>`).join('')||'<tr><td colspan="6">暂无任务</td></tr>'}catch(e){notice(e.message)}}
function renderSources(){$('#sources').innerHTML=sourceData.map(s=>`<div class="source ${s.healthState}"><div><h3>${esc(s.name)} · ${esc(s.healthState)}</h3><div class="meta">${esc(s.baseUrl)} · 账号 ${esc(s.accountKey)} · 权重 ${s.weight} · 请求 ${s.requestCount} / 错误 ${s.errorCount} · 最近 ${s.lastLatencyMs??'-'} ms${s.lastErrorCode?' · '+esc(s.lastErrorCode):''}</div></div><div class="actions"><button onclick="testSource('${esc(s.id)}')">测试</button><button class="alt" onclick="openSource('${esc(s.id)}')">编辑</button><button class="danger" onclick="removeSource('${esc(s.id)}')">删除</button></div></div>`).join('')||'<div class="meta">还没有数据源。添加第一个 standard-http-v1 来源后即可接收任务。</div>'}
function openSource(id){const f=$('#sourceForm');f.reset();f.enabled.checked=true;f.weight.value=1;f.accountKey.value='wechat-main';f.headerName.value='x-api-key';const s=sourceData.find(x=>x.id===id);$('#dialogTitle').textContent=s?'编辑数据源':'添加数据源';f.dataset.edit=id||'';if(s){f.id.value=s.id;f.id.readOnly=true;f.name.value=s.name;f.baseUrl.value=s.baseUrl;f.accountKey.value=s.accountKey;f.weight.value=s.weight;f.enabled.checked=s.enabled}sourceDialog.showModal()}
$('#sourceForm').addEventListener('submit',async e=>{e.preventDefault();const f=e.currentTarget,d=new FormData(f),id=d.get('id'),editing=f.dataset.edit;const authType=d.get('authType');const body={id,name:d.get('name'),baseUrl:d.get('baseUrl'),enabled:d.get('enabled')==='on',weight:Number(d.get('weight')),accountKey:d.get('accountKey'),auth:{type:authType,credentialRef:'dpapi://'+id,...(authType==='api_key_header'?{headerName:d.get('headerName')}:{})},mediaSecurity:{allowedHosts:String(d.get('allowedHosts')).split(/\s+/).filter(Boolean),allowPrivateNetwork:d.get('allowPrivateNetwork')==='on'}};if(d.get('credential'))body.credential=d.get('credential');try{await api(editing?'/api/sources/'+id:'/api/sources',{method:editing?'PUT':'POST',body:JSON.stringify(body)});sourceDialog.close();notice('配置已保存');await refreshAll()}catch(err){notice(err.message)}});
async function testSource(id){try{const x=await api('/api/sources/'+id+'/test',{method:'POST'});notice('连接成功：'+x.sourceName+' / '+x.versions.join(','));await refreshAll()}catch(e){notice('连接失败：'+e.message)}}async function removeSource(id){if(!confirm('删除来源 '+id+'？本地凭据也会删除。'))return;try{await api('/api/sources/'+id,{method:'DELETE'});notice('来源已删除');await refreshAll()}catch(e){notice(e.message)}}async function preflight(){try{const x=await api('/api/preflight',{method:'POST'});notice(`预检：微信 ${x.running?'运行中':'未运行'}，桌面 ${x.desktopUnlocked?'可交互':'不可交互'}，朋友圈 ${x.momentsWindowReady?'就绪':'未就绪'}`);await refreshAll()}catch(e){notice(e.message)}}function notice(x){$('#notice').textContent=x}function esc(x){return String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}refreshAll();setInterval(refreshAll,15000);
</script></body></html>"""
