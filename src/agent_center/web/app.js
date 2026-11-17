'use strict';
const $ = s => document.querySelector(s);
const token = $('meta[name="agent-center-token"]').content;
let state = {tasks: [], agents: [], limits: {}}, selected = null, detail = null, tab = 'overview', workspace = '', pendingApproval = null, busy = false, lastView = '', lastDetail = '';
const terminal = status => ['succeeded','failed','cancelled'].includes(status);
const labels = {created:'已创建',planning:'规划中',running:'执行中',verifying:'验证中',waiting_approval:'等待审批',paused:'已暂停',blocked:'已阻塞',interrupted:'已停止',succeeded:'已完成',failed:'失败',cancelled:'已取消'};
const node = (tag, cls, text) => { const e = document.createElement(tag); if(cls) e.className = cls; if(text !== undefined) e.textContent = text; return e; };
function toast(text) { $('#toast').textContent = text; $('#toast').hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $('#toast').hidden = true, 6000); }
async function api(path, body) { const r = await fetch(path, {method: body ? 'POST' : 'GET', headers: {'Content-Type':'application/json', 'X-Agent-Center-Token':token}, ...(body ? {body:JSON.stringify(body)} : {})}); const data = await r.json(); if(!r.ok) throw Error(data.error==='接口不存在'?'当前后端版本较旧，请重启 agent-center web 后刷新页面。':data.error || '请求失败'); return data; }
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
  $('#page-title').textContent=active?detail.task.goal:'新任务'; $('#prompt').placeholder=active?(terminal(detail.task.status)?'任务已结束，请新建任务':'追加需求，下一次运行边界生效…'):'描述任务，或提出一个问题…';
  $('#prompt').disabled=active&&terminal(detail.task.status);$('#send').disabled=busy||(active&&terminal(detail.task.status));
  renderRecovery();
  $('#workspace-button').disabled=active;
  if(!active) {$('#thread').replaceChildren();renderDetails();return;}
  const t=detail.task; $('#workspace-caption').textContent=t.workspace; $('#workspace-button').title=t.workspace;
  $('#task-status').textContent=labels[t.status]||t.status;
  document.querySelectorAll('[data-action]').forEach(b=>{const a=b.dataset.action; b.disabled=a==='resume'?!detail.recovery.can_resume:a==='cancel'?['succeeded','failed','cancelled'].includes(t.status):!detail.running;});
  const signature=JSON.stringify([detail.messages,detail.events,t.last_error,detail.running]);
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
async function refresh() {try{state=await api('/api/state');if(state.api_revision!==3){$('#connection').textContent='后端需重启';$('#prompt').disabled=true;$('#send').disabled=true;toast('检测到旧后端：请重启 agent-center web，再刷新页面使用统筹配置与任务恢复。');return;}$('#connection').replaceChildren(node('i'),document.createTextNode('已连接'));renderTasks();if(!selected&&(state.active_task_id||state.unresolved_task_ids?.length)){selected=state.active_task_id||state.unresolved_task_ids[0];lastView='';}if(selected){detail=await api('/api/tasks/'+selected);render();}}catch(e){$('#connection').textContent='连接已断开';}}
$('#new-task').onclick=async()=>{await refresh();const active=state.active_task_id||state.unresolved_task_ids?.[0];if(active){await selectTask(active);toast('当前任务尚未结束：可在此追加需求，或检查对账并取消后新建。');$('#prompt').focus();return;}selected=null;detail=null;lastView='';$('#workspace-caption').textContent=workspace||'自动创建工作区';render();renderTasks();$('#prompt').focus();$('#sidebar').classList.remove('open');};
$('#search').oninput=renderTasks;
$('#composer').onsubmit=async e=>{e.preventDefault();if(busy)return;const text=$('#prompt').value.trim();if(!text)return;busy=true;$('#send').disabled=true;try{await refresh();if(selected&&terminal(detail.task.status))throw Error('任务已结束，请点击新建任务。');if(selected){detail=await api('/api/tasks/'+selected+'/message',{text});toast(detail.running?'追加需求已保存，任务正在推进':'追加需求已保存；请查看任务状态，继续或检查对账。');}else{detail=await api('/api/tasks',{goal:text,workspace});selected=detail.task.task_id;}$('#prompt').value='';lastView='';render();await refresh();}catch(err){toast(err.message);}finally{busy=false;$('#send').disabled=false;}};
$('#prompt').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$('#composer').requestSubmit();}};
document.querySelectorAll('[data-prompt]').forEach(b=>b.onclick=()=>{$('#prompt').value=b.dataset.prompt;$('#prompt').focus();});
document.querySelectorAll('[data-action]').forEach(b=>b.onclick=async()=>{if(!selected)return;const action=b.dataset.action;if(action==='cancel'&&!confirm('取消此任务？已有记录和工作区会保留。'))return;try{detail=await api('/api/tasks/'+selected+'/'+action,{});render();await refresh();toast({pause:'已请求暂停，将在运行边界生效',stop:'已请求停止',cancel:'任务已取消',resume:'任务已继续'}[action]);}catch(e){toast(e.message);}});
$('#details-toggle').onclick=()=>$('#inspector').hidden=!$('#inspector').hidden;$('#close-details').onclick=()=>$('#inspector').hidden=true;
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{tab=b.dataset.tab;document.querySelectorAll('[data-tab]').forEach(t=>t.classList.toggle('active',t===b));renderDetails();});
$('#workspace-button').onclick=()=>{$('#workspace-path').value=workspace;$('#workspace-dialog').showModal();};
function applyWorkspace(path){workspace=path.trim();$('#workspace-path').value=workspace;$('#workspace-caption').textContent=workspace||'自动创建工作区';}
$('#workspace-dialog').addEventListener('close',()=>{if($('#workspace-dialog').returnValue==='save')applyWorkspace($('#workspace-path').value);$('#prompt').focus();});
$('#settings').onclick=async()=>{await refresh();await loadHealth();renderSettings();if(!$('#settings-dialog').open)$('#settings-dialog').showModal();};
$('#close-settings').onclick=()=>$('#settings-dialog').close();
function showApproval(a){pendingApproval=a;const out=$('#approval-scope');out.replaceChildren(node('div','notice',a.operation+'\n'+a.target_path+'\n风险：'+a.risk),node('pre','',a.approval_id));$('#approval-confirm').value='';$('#approval-dialog').showModal();}
$('#approval-dialog').addEventListener('close',async()=>{const decision=$('#approval-dialog').returnValue;if(!['approve','deny'].includes(decision)||!pendingApproval)return;try{detail=await api('/api/tasks/'+selected+'/approval',{approval_id:pendingApproval.approval_id,confirmation:$('#approval-confirm').value,decision});render();await refresh();toast('审批决定已记录，原生权限仍需后端核验');}catch(e){toast(e.message);}});
$('#menu').onclick=()=>$('#sidebar').classList.toggle('open');
document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='k'){e.preventDefault();$('#new-task').click();}});


let pathCallback = null, pathFiles = false, pathState = null, pathGeneration = 0;
async function showPath(path, files, callback) {
 pathCallback=callback;pathFiles=files;$('#path-title').textContent=files?'选择可执行文件':'选择文件夹';
 pathState=null;$('#path-entries').replaceChildren();$('#path-select').hidden=files;$('#path-dialog').showModal();await listPath(path);
}
async function listPath(path) {
 const generation=++pathGeneration;pathState=null;$('#path-select').disabled=true;
 try {const result=await api('/api/directories',{path,files:pathFiles,hidden:$('#path-hidden').checked});if(generation!==pathGeneration)return;pathState=result;$('#browse-path').value=pathState.path;
 const out=$('#path-entries');out.replaceChildren();pathState.entries.forEach(e=>{const b=node('button','path-entry',(e.directory?'▱  ':'↗  ')+e.name);b.type='button';b.append(node('span','',e.directory?'›':'选择'));b.onclick=()=>{if(e.directory)listPath(e.path);else{pathCallback(e.path);$('#path-dialog').close();}};out.append(b);});if(!out.children.length)out.append(node('p','empty-history','此目录没有可选择的项目'));$('#path-select').disabled=false;}
 catch(e){if(generation===pathGeneration)$('#path-entries').replaceChildren(node('div','notice',e.message));}
}
$('#browse-workspace').onclick=()=>showPath($('#workspace-path').value,false,p=>{applyWorkspace(p);$('#path-dialog').close();$('#workspace-dialog').close('save');$('#prompt').focus();});
$('#close-path').onclick=()=>$('#path-dialog').close();$('#path-go').onclick=()=>listPath($('#browse-path').value);
$('#browse-path').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();listPath(e.target.value);}};
$('#path-parent').onclick=()=>{if(pathState)listPath(pathState.parent);};
$('#path-select').onclick=()=>{if(pathState){pathCallback(pathState.path);$('#path-dialog').close();}};
$('#browse-executable').onclick=()=>showPath('',true,p=>{$('#agent-executable').value=p;resetModels();loadModels();});
$('#browse-package').onclick=()=>showPath('',false,p=>$('#agent-package').value=p);
function connectionFields(){
 const deep=$('#agent-kind').value==='deepseek';if(deep)$('#agent-auth').value='api';
 $('#agent-auth').disabled=deep;$('#deepseek-fields').hidden=!deep;
 const custom=$('#agent-auth').value==='api';$('#api-fields').hidden=!custom;$('#agent-base-url').required=custom;$('#subscription-note').hidden=custom;$('#key-env-field').hidden=!custom;$('#agent-model').required=false;if(!custom){$('#agent-api-key').value='';}
 [...$('#agent-key-env').options].forEach(o=>o.disabled=deep?o.value!=='DEEPSEEK_API_KEY':o.value==='DEEPSEEK_API_KEY');
 if(deep)$('#agent-key-env').value='DEEPSEEK_API_KEY';else if($('#agent-key-env').value==='DEEPSEEK_API_KEY')$('#agent-key-env').value='OPENAI_API_KEY';
}
async function detectInstallation(){
 const kind=$('#agent-kind').value;$('#installation-status').textContent='正在检测本机安装…';
 try{const found=await api('/api/agents/discover');if(kind!==$('#agent-kind').value)return;const a=found.find(x=>x.agent_id===kind);
 if(!a){$('#installation-status').textContent='未找到安装，请在高级设置中选择';return;}
 $('#agent-executable').value=a.executable;$('#agent-package').value=a.cli_install_dir;loadModels();
 $('#installation-status').textContent='● '+(kind==='codex'?'Codex CLI':'DeepSeek Harness')+' 已检测到';$('#installation-status').title=a.executable;
 }catch(e){$('#installation-status').textContent=e.message;}
}
$('#add-agent').onclick=()=>{$('#agent-form').reset();resetModels();$('#agent-error').hidden=true;connectionFields();$('#agent-dialog').showModal();detectInstallation();};
$('#close-agent').onclick=()=>{$('#agent-api-key').value='';$('#agent-dialog').close();};
$('#agent-kind').onchange=()=>{resetModels();$('#agent-executable').value='';$('#agent-package').value='';$('#agent-base-url').value=$('#agent-kind').value==='deepseek'?'https://api.deepseek.com':'';connectionFields();detectInstallation();};
$('#agent-auth').onchange=()=>{connectionFields();resetModels();if($('#agent-auth').value==='native')loadModels();};$('#detect-agent').onclick=detectInstallation;
$('#path-hidden').onchange=()=>listPath($('#browse-path').value);
$('#agent-form').onsubmit=async e=>{e.preventDefault();$('#agent-error').hidden=true;$('#save-agent').disabled=true;
 try{await api('/api/agents',{agent_id:$('#agent-kind').value,executable:$('#agent-executable').value,model:$('#agent-model').value==='__manual__'?$('#custom-model').value:$('#agent-model').value,reasoning_effort:$('#agent-effort').value,base_url:$('#agent-auth').value==='api'?$('#agent-base-url').value:'',api_key_env:$('#agent-auth').value==='api'?$('#agent-key-env').value:'',api_key:$('#agent-auth').value==='api'?$('#agent-api-key').value:'',provider:$('#agent-kind').value==='deepseek'?$('#agent-provider').value:'',cli_install_dir:$('#agent-package').value,permission_mode:$('#agent-permission').value,replace:$('#agent-replace').checked});$('#agent-api-key').value='';await refresh();$('#agent-dialog').close();$('#settings').click();toast('Agent 已保存；执行等待原生能力验证');}
 catch(err){$('#agent-error').textContent=err.message;$('#agent-error').hidden=false;}finally{$('#save-agent').disabled=false;}};

let modelCatalog=[],modelGeneration=0;
function resetModels(){modelGeneration++;$('#refresh-models').disabled=false;modelCatalog=[];$('#agent-model').replaceChildren(node('option','','使用 CLI 默认模型'));$('#agent-model').firstChild.value='';const m=node('option','','手动输入…');m.value='__manual__';$('#agent-model').append(m);$('#model-source').textContent='';$('#custom-model').hidden=true;renderEfforts();}
function renderEfforts(){const model=modelCatalog.find(m=>m.id===$('#agent-model').value);const select=$('#agent-effort');select.replaceChildren();const d=node('option','','模型默认'+(model?.default_effort?' · '+model.default_effort:''));d.value='';select.append(d);const labels={low:'低',medium:'中',high:'高',xhigh:'很高',max:'最高',ultra:'Ultra',minimal:'极低',none:'无',off:'关闭'};(model?.efforts||[]).forEach(e=>{const o=node('option','',(labels[e]||e)+' · '+e);o.value=e;select.append(o);});select.disabled=!model?.efforts?.length;$('#custom-model').hidden=$('#agent-model').value!=='__manual__';$('#custom-model').required=!$('#custom-model').hidden;}
async function loadModels(){if($('#agent-auth').value==='api'&&!$('#agent-base-url').value){resetModels();$('#model-source').textContent='填写 Base URL 后检测模型';$('#refresh-models').disabled=false;return;}const generation=++modelGeneration;$('#model-source').textContent='正在读取模型目录…';$('#refresh-models').disabled=true;
try{const result=await api('/api/agents/models',{agent_id:$('#agent-kind').value,executable:$('#agent-executable').value,cli_install_dir:$('#agent-package').value,base_url:$('#agent-auth').value==='api'?$('#agent-base-url').value:'',api_key_env:$('#agent-auth').value==='api'?$('#agent-key-env').value:'',api_key:$('#agent-auth').value==='api'?$('#agent-api-key').value:''});if(generation!==modelGeneration)return;modelCatalog=result.models;const select=$('#agent-model');select.replaceChildren();const d=node('option','','使用 CLI 默认模型');d.value='';select.append(d);modelCatalog.forEach(m=>{const name=m.name||m.id;const duplicate=modelCatalog.some(other=>other.id!==m.id&&(other.name||other.id)===name);const o=node('option','',duplicate&&!name.includes(m.id)?name+' · '+m.id:name);o.value=m.id;o.title=m.id;select.append(o);});const manual=node('option','','手动输入…');manual.value='__manual__';select.append(manual);const preferred=modelCatalog.find(m=>m.default);if(preferred)select.value=preferred.id;$('#model-source').textContent=result.source+(modelCatalog.length?'':' · 暂无模型');renderEfforts();}
catch(e){if(generation===modelGeneration){resetModels();$('#model-source').textContent=e.message+'，可稍后刷新或手动输入。';}}
finally{if(generation===modelGeneration||!modelCatalog.length)$('#refresh-models').disabled=false;}}
$('#agent-model').onchange=renderEfforts;$('#refresh-models').onclick=loadModels;
$('#agent-base-url').onchange=()=>{resetModels();if($('#agent-base-url').value)loadModels();};

$('#agent-executable').onchange=()=>{resetModels();loadModels();};

function renderRecovery(){
 const out=$('#recovery-notice');out.replaceChildren();out.hidden=!detail?.recovery?.needs_review;
 if(out.hidden)return;
 out.append(node('span','',detail.recovery.hint));
 const b=node('button','outline',detail.recovery.can_reconcile?'检查与对账':'查看审批请求');b.onclick=detail.recovery.can_reconcile?showReconcile:()=>{$('#inspector').hidden=false;tab='overview';renderDetails();};out.append(b);
}
function showReconcile(){
 $('#reconcile-form').reset();$('#reconcile-error').hidden=true;
 const out=$('#reconcile-info');out.replaceChildren(node('p','',detail.recovery.hint),node('pre','',detail.task.task_id),node('pre','',detail.task.workspace));
 detail.runs.forEach(r=>{const c=node('div','detail-card');c.append(node('strong','',r.run_id+' · '+r.status),node('small','',`PID ${r.pid??'—'} · 退出码 ${r.exit_code??'—'}`),node('pre','',r.log_path));if(r.error)c.append(node('small','',r.error));out.append(c);});
 if(!detail.runs.length)out.append(node('p','','没有 Agent Run。对账将清理中断状态，保留消息、预算和事件记录。'));
 $('#reconcile-dialog').showModal();
}
$('#close-reconcile').onclick=()=>$('#reconcile-dialog').close();
$('#reconcile-form').onsubmit=async e=>{e.preventDefault();$('#save-reconcile').disabled=true;
 try{detail=await api('/api/tasks/'+selected+'/reconcile',{confirmation:$('#reconcile-confirm').value,reviewed:$('#reconcile-reviewed').checked});$('#reconcile-dialog').close();render();await refresh();toast('对账已记录，可继续重新规划，或取消任务。');}
 catch(err){$('#reconcile-error').textContent=err.message;$('#reconcile-error').hidden=false;}finally{$('#save-reconcile').disabled=false;}
};
const capabilityNames={command_execution_supported:'命令执行',read_only_supported:'只读模式',workspace_write_supported:'工作区写入',filesystem_read_boundary_verified:'文件读取边界',filesystem_write_boundary_verified:'文件写入边界',network_boundary_verified:'网络边界',approval_response_supported:'原生审批响应',structured_command_exit_verified:'结构化退出证据',cancellation_verified:'停止进程'};
const capabilityLabels={SUPPORTED:'已验证',UNSUPPORTED:'不支持',UNVERIFIED:'未验证'};
async function loadHealth(){try{const health=await api('/api/agents/health');state.agents.forEach(a=>a.health=health[a.id]);}catch(e){toast(e.message);}}
function renderSettings(){
 const supervisor=state.supervisor_settings, out=$('#supervisor-settings');out.replaceChildren();
 const card=node('div','detail-card');card.append(node('strong','','统筹模型'),node('small','',state.supervisor.includes('REPLACE_WITH')?'尚未配置模型':state.supervisor),node('small','',supervisor.address_configured&&supervisor.key_configured?'连接信息已配置 · API 可用性未验证':'缺少连接地址或密钥，请配置'));
 const edit=node('button','outline','配置统筹模型');edit.onclick=showSupervisor;card.append(edit);out.append(card);
 const blocker=$('#configuration-blocker');blocker.replaceChildren();
 const taskId=state.active_task_id||state.unresolved_task_ids?.[0];
 if(taskId){const c=node('div','notice');c.append(node('p','',`当前任务 ${taskId}：运行中或未对账时不能改配置；空闲时可安全新增接入，替换已有 Agent 可能需要先取消任务。`));const b=node('button','outline','打开任务，继续 / 取消 / 对账');b.onclick=async()=>{$('#settings-dialog').close();await selectTask(taskId);$('#inspector').hidden=false;};c.append(b);blocker.append(c);}
 const agents=$('#agent-settings');agents.replaceChildren();state.agents.forEach(a=>{
 const c=node('div','detail-card'), health=a.health||{status:'未验证',reason:'尚未检查本机安装与能力'};
 c.append(node('strong','',a.id+' · '+(a.enabled?'已启用':'已停用')),node('small','',(a.model.includes('REPLACE_WITH')?'模型待配置':a.model||'CLI 默认模型')+' · '+(a.permission==='read-only'?'只读':'工作区可写')),node('span','health-pill',health.status),node('small','',health.reason));
 const advanced=node('details','agent-advanced');advanced.append(node('summary','','能力与安装详情'));
 Object.entries(a.capabilities).forEach(([k,v])=>{row(advanced,capabilityNames[k]||({installation_identity:'安装指纹',evidence_reference:'验证证据'}[k]||k),capabilityLabels[v]||v||'无');});
 advanced.append(node('pre','',a.executable));if(a.cli_install_dir)advanced.append(node('pre','',a.cli_install_dir));c.append(advanced);agents.append(c);
 });
}
function showSupervisor(){
 const s=state.supervisor_settings;$('#supervisor-form').reset();$('#supervisor-error').hidden=true;
 $('#supervisor-model').value=s.model.includes('REPLACE_WITH')?'':s.model;$('#supervisor-protocol').value=s.protocol;
 $('#supervisor-url-env').value=s.base_url_env;$('#supervisor-key-env').value=s.key_variable;
 $('#supervisor-effort').value=s.reasoning_effort;$('#supervisor-timeout').value=s.request_timeout_seconds;$('#supervisor-tokens').value=s.max_output_tokens;
 $('#supervisor-env-status').textContent=`地址环境：${s.address_configured?'已设置':'未设置'} · 密钥环境：${s.key_configured?'已设置':'未设置'}。已保存的值不会显示。`;
 $('#supervisor-dialog').showModal();
}
$('#composer-supervisor').onclick=()=>$('#settings').click();
$('#close-supervisor').onclick=()=>$('#supervisor-dialog').close();
$('#supervisor-dialog').addEventListener('close',()=>{$('#supervisor-key').value='';$('#supervisor-url').value='';});
$('#supervisor-form').onsubmit=async e=>{e.preventDefault();$('#save-supervisor').disabled=true;
 try{await api('/api/supervisor',{protocol:$('#supervisor-protocol').value,model:$('#supervisor-model').value.trim(),base_url_env:$('#supervisor-url-env').value.trim(),api_key_env:$('#supervisor-key-env').value.trim(),base_url:$('#supervisor-url').value.trim(),api_key:$('#supervisor-key').value,reasoning_effort:$('#supervisor-effort').value,request_timeout_seconds:Number($('#supervisor-timeout').value),max_output_tokens:Number($('#supervisor-tokens').value)});$('#supervisor-dialog').close();await refresh();await loadHealth();renderSettings();toast('统筹配置已生效；地址与密钥仅本次服务有效。');}
 catch(err){$('#supervisor-error').textContent=err.message;$('#supervisor-error').hidden=false;}finally{$('#save-supervisor').disabled=false;}
};
refresh();setInterval(refresh,2500);
