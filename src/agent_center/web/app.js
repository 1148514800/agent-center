'use strict';
const $ = s => document.querySelector(s);
const token = $('meta[name="agent-center-token"]').content;
let state = {tasks: [], agents: [], limits: {}}, selected = null, detail = null, tab = 'overview', workspace = '', pendingApproval = null, busy = false, lastView = '', lastDetail = '';
const labels = {created:'已创建',planning:'规划中',running:'执行中',verifying:'验证中',waiting_approval:'等待审批',paused:'已暂停',blocked:'已阻塞',interrupted:'已停止',succeeded:'已完成',failed:'失败',cancelled:'已取消'};
const node = (tag, cls, text) => { const e = document.createElement(tag); if(cls) e.className = cls; if(text !== undefined) e.textContent = text; return e; };
function toast(text) { $('#toast').textContent = text; $('#toast').hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $('#toast').hidden = true, 6000); }
async function api(path, body) { const r = await fetch(path, {method: body ? 'POST' : 'GET', headers: {'Content-Type':'application/json', 'X-Agent-Center-Token':token}, ...(body ? {body:JSON.stringify(body)} : {})}); const data = await r.json(); if(!r.ok) throw Error(data.error || '请求失败'); return data; }
function payload(event) { try {return typeof event.payload === 'string' ? JSON.parse(event.payload) : event.payload;} catch {return {};} }
function renderTasks() {
  const query = $('#search').value.toLowerCase(); $('#task-count').textContent = state.tasks.length;
  const list = $('#tasks'); list.replaceChildren();
  state.tasks.filter(t => t.goal.toLowerCase().includes(query)).forEach(t => {const b=node('button','task-item'+(selected===t.task_id?' selected':'')); b.append(node('strong','',t.goal)); const meta=node('small'); meta.append(node('span','',labels[t.status]||t.status),node('span','',new Date(t.created_at).toLocaleDateString('zh-CN',{month:'short',day:'numeric'}))); b.append(meta); b.onclick=()=>selectTask(t.task_id); list.append(b);});
  if(!list.children.length) list.append(node('div','empty-history',query?'未找到匹配的任务':'还没有任务。\n开始你的第一个想法。'));
}
async function selectTask(id) {selected=id; lastView=''; $('#sidebar').classList.remove('open'); try {detail=await api('/api/tasks/'+id); render();renderTasks();}catch(e){toast(e.message);} }
function render() {
  const active=!!detail; $('#welcome').hidden=active; $('#thread').hidden=!active; $('#task-controls').hidden=!active;
  $('#page-title').textContent=active?detail.task.goal:'新任务'; $('#prompt').placeholder=active?'追加需求，下一次运行边界生效…':'描述任务，或提出一个问题…';
  $('#workspace-button').disabled=active;
  if(!active) {$('#thread').replaceChildren();renderDetails();return;}
  const t=detail.task; $('#workspace-caption').textContent=t.workspace; $('#workspace-button').title=t.workspace;
  $('#task-status').textContent=labels[t.status]||t.status;
  document.querySelectorAll('[data-action]').forEach(b=>{const a=b.dataset.action; b.disabled=a==='resume'?(!['created','paused','interrupted','planning'].includes(t.status)||detail.running):a==='cancel'?['succeeded','failed','cancelled'].includes(t.status):!detail.running;});
  const signature=JSON.stringify([detail.messages,detail.events,t.last_error]);
  if(signature!==lastView){ const thread=$('#thread'), conversation=$('.conversation'); const nearBottom=conversation.scrollHeight-conversation.scrollTop-conversation.clientHeight<180; thread.replaceChildren();
    detail.messages.forEach(m=>thread.append(node('div','message user',m.text)));
    const responses=detail.events.filter(e=>e.type==='web_response'||e.type==='readonly_question');
    responses.forEach(e=>{const p=payload(e), msg=node('div','message agent'); msg.append(node('div','message-label','✳  AGENT CENTER'),node('div','',p.text||p.answer||''));thread.append(msg);});
    const actions=detail.events.filter(e=>e.type==='supervisor_action');
    if(actions.length){const p=payload(actions.at(-1));const msg=node('div','message agent');msg.append(node('div','message-label','✳  任务进展'),node('div','',p.reason_summary||'统筹模型正在处理任务。'));thread.append(msg);}
    if(t.last_error)thread.append(node('div','notice',t.last_error));
    if(detail.running)thread.append(node('div','message-label','●  正在推进任务，状态自动更新…'));
    if(!responses.length&&!actions.length&&!t.last_error)thread.append(node('div','message agent','任务已保存。可以在右侧查看计划、运行与验证证据。'));
    lastView=signature;if(nearBottom)requestAnimationFrame(()=>conversation.scrollTop=conversation.scrollHeight);
  }
  renderDetails();
}
function block(title) { const b=node('section','detail-block');b.append(node('h3','',title));return b; }
function row(parent,key,value) {const r=node('div','detail-row');r.append(node('span','',key),node('b','',value));parent.append(r);}
function renderDetails() {
 const signature=JSON.stringify([selected,tab,detail,state.limits]);if(signature===lastDetail)return;lastDetail=signature;
 const out=$('#detail-content');out.replaceChildren();if(!detail){out.append(node('p','empty-history','创建或选择任务后，在这里查看计划、运行和验证结果。'));return;}
 const t=detail.task;
 if(tab==='overview'){
 const summary=block('当前任务');row(summary,'状态',labels[t.status]||t.status);row(summary,'需求版本','v'+t.requirements_version);summary.append(node('pre','',t.workspace));out.append(summary);
 const budget=block('调用预算');row(budget,'Agent',`${t.agent_calls} / ${state.limits.max_agent_calls}`);row(budget,'统筹模型',`${t.supervisor_calls} / ${state.limits.max_supervisor_calls}`);const bar=node('div','progress-bar'),fill=node('div');fill.style.width=Math.min(100,100*t.agent_calls/state.limits.max_agent_calls)+'%';bar.append(fill);budget.append(bar);out.append(budget);
 const plan=block('执行计划');if(!detail.plan.length)plan.append(node('p','','尚未生成计划'));detail.plan.forEach((s,i)=>{const card=node('div','detail-card');card.append(node('strong','',`${String(i+1).padStart(2,'0')}  ${s.objective}`));s.acceptance_criteria.forEach(c=>card.append(node('small','',`${c.id} · ${c.description}`)));plan.append(card);});out.append(plan);
 const runs=block('Agent 运行');if(!detail.runs.length)runs.append(node('p','','尚未启动 Agent'));detail.runs.forEach(r=>{const card=node('div','detail-card');card.append(node('strong','',r.agent_id+' · '+r.status),node('small','',r.run_id),node('small','',`退出码 ${r.exit_code??'—'}`));if(r.error)card.append(node('small','',r.error));runs.append(card);});out.append(runs);
 const approvals=detail.approvals.filter(a=>a.status==='pending');if(approvals.length){const b=block('等待审批');approvals.forEach(a=>{const c=node('div','detail-card');c.append(node('strong','',a.operation),node('small','',a.risk));const button=node('button','','查看请求 →');button.onclick=()=>showApproval(a);c.append(button);b.append(c);});out.prepend(b);}
 }else if(tab==='evidence'){
 const b=block('验证证据');if(!detail.evidence.length)b.append(node('p','','尚无验证证据。Agent 的最终文字不会作为独立测试通过证据。'));detail.evidence.forEach(e=>{const c=node('div','detail-card');c.append(node('strong','',e.verification_status||({agent_report:'Agent 自述',agent_event:'执行事件',supervisor:'统筹回复'}[e.source]||e.source)),node('small','',e.evidence_id),node('small','',`需求 v${e.requirements_version} · ${e.run_id||'无 Run'}`));const d=node('details');d.append(node('summary','','查看证据'),node('pre','',e.summary));c.append(d);b.append(c);});out.append(b);
 }else{const b=block('最近 200 条事件');detail.events.slice().reverse().forEach(e=>{const el=node('div','event');el.append(node('span','',`#${e.sequence} · ${new Date(e.timestamp).toLocaleTimeString('zh-CN')}`),node('strong','',e.type),node('pre','',JSON.stringify(payload(e),null,2)));b.append(el);});out.append(b);}
}
async function refresh() {try{state=await api('/api/state');$('#connection').replaceChildren(node('i'),document.createTextNode('已连接'));renderTasks();if(selected){detail=await api('/api/tasks/'+selected);render();}}catch(e){$('#connection').textContent='连接已断开';}}
$('#new-task').onclick=()=>{selected=null;detail=null;lastView='';$('#workspace-caption').textContent=workspace||'自动创建工作区';render();renderTasks();$('#prompt').focus();$('#sidebar').classList.remove('open');};
$('#search').oninput=renderTasks;
$('#composer').onsubmit=async e=>{e.preventDefault();if(busy)return;const text=$('#prompt').value.trim();if(!text)return;busy=true;$('#send').disabled=true;try{if(selected){detail=await api('/api/tasks/'+selected+'/message',{text});toast('追加需求已保存');}else{detail=await api('/api/tasks',{goal:text,workspace});selected=detail.task.task_id;}$('#prompt').value='';lastView='';render();await refresh();}catch(err){toast(err.message);}finally{busy=false;$('#send').disabled=false;}};
$('#prompt').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$('#composer').requestSubmit();}};
document.querySelectorAll('[data-prompt]').forEach(b=>b.onclick=()=>{$('#prompt').value=b.dataset.prompt;$('#prompt').focus();});
document.querySelectorAll('[data-action]').forEach(b=>b.onclick=async()=>{if(!selected)return;const action=b.dataset.action;if(action==='cancel'&&!confirm('取消此任务？已有记录和工作区会保留。'))return;try{detail=await api('/api/tasks/'+selected+'/'+action,{});render();await refresh();toast({pause:'已请求暂停，将在运行边界生效',stop:'已请求停止',cancel:'任务已取消',resume:'任务已继续'}[action]);}catch(e){toast(e.message);}});
$('#details-toggle').onclick=()=>$('#inspector').hidden=!$('#inspector').hidden;$('#close-details').onclick=()=>$('#inspector').hidden=true;
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{tab=b.dataset.tab;document.querySelectorAll('[data-tab]').forEach(t=>t.classList.toggle('active',t===b));renderDetails();});
$('#workspace-button').onclick=()=>{$('#workspace-path').value=workspace;$('#workspace-dialog').showModal();};
$('#workspace-dialog').addEventListener('close',()=>{if($('#workspace-dialog').returnValue==='save'){workspace=$('#workspace-path').value.trim();$('#workspace-caption').textContent=workspace||'自动创建工作区';}});
$('#settings').onclick=()=>{const out=$('#agent-settings');out.replaceChildren();out.append(node('div','detail-card','统筹模型 · '+state.supervisor));state.agents.forEach(a=>{const c=node('div','detail-card');c.append(node('strong','',a.id+' · '+(a.enabled?'已启用':'执行关闭')),node('small','',a.model+' · '+a.permission));Object.entries(a.capabilities).filter(([k])=>k.endsWith('supported')||k.endsWith('verified')).forEach(([k,v])=>{const r=node('div','capability');r.append(node('span','',k),node('span','',v));c.append(r);});out.append(c);});$('#settings-dialog').showModal();};
$('#close-settings').onclick=()=>$('#settings-dialog').close();
function showApproval(a){pendingApproval=a;const out=$('#approval-scope');out.replaceChildren(node('div','notice',a.operation+'\n'+a.target_path+'\n风险：'+a.risk),node('pre','',a.approval_id));$('#approval-confirm').value='';$('#approval-dialog').showModal();}
$('#approval-dialog').addEventListener('close',async()=>{const decision=$('#approval-dialog').returnValue;if(!['approve','deny'].includes(decision)||!pendingApproval)return;try{detail=await api('/api/tasks/'+selected+'/approval',{approval_id:pendingApproval.approval_id,confirmation:$('#approval-confirm').value,decision});render();await refresh();toast('审批决定已记录，原生权限仍需后端核验');}catch(e){toast(e.message);}});
$('#menu').onclick=()=>$('#sidebar').classList.toggle('open');
document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='k'){e.preventDefault();$('#new-task').click();}});
refresh();setInterval(refresh,2500);
