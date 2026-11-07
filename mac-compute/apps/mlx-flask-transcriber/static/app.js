'use strict';
const $ = id => document.getElementById(id);
const TERMINAL = new Set(['done','error','cancelled']);
const STATUSES = {
  queued:'排队中',preparing:'提取音频',running:'语音识别中',
  formatting:'整理文本',cancelling:'取消中',cancelled:'已取消',
  done:'已完成',error:'失败'
};
const MODES = ['article','captions','raw'];
let selectedFile = null;
let jobId = localStorage.getItem('mlx-selected-job') || null;
let currentMode = 'article';
let serverText = {article:'',captions:'',raw:''};
const edits = new Map();
let latestJobs = [];
const selectedJobs = new Set();
let retrySource = null, latestFailedId = null, clearableCount = 0;
let uploadBusy = false, historyBusy = false, retryBusy = false;
let listRequest = null, detailRequest = null;

function show(id){$(id).classList.remove('hidden');}
function hide(id){$(id).classList.add('hidden');}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function prettyTime(sec){
  if(sec===undefined || sec===null || !Number.isFinite(Number(sec)))return '—';
  const v=Math.max(0,Math.round(Number(sec)));
  const h=Math.floor(v/3600),m=Math.floor((v%3600)/60),s=v%60;
  return h ? `${h}小时${m}分${s}秒` : `${m}分${String(s).padStart(2,'0')}秒`;
}
function dateTime(epoch){return epoch ? new Date(epoch*1000).toLocaleString('zh-CN',{hour12:false}) : '—';}
function stateText(job){
  if(job.status==='queued' && job.queue_position)return `排队第 ${job.queue_position} 位`;
  return STATUSES[job.status]||job.status;
}
function selectedEdits(){
  if(!jobId)return {};
  if(!edits.has(jobId))edits.set(jobId, {article:null,captions:null,raw:null});
  return edits.get(jobId);
}
function renderText(){
  const editsForJob=selectedEdits();
  const value=editsForJob[currentMode]===null ? serverText[currentMode] : editsForJob[currentMode];
  $('result').value=value||'';
  $('char-info').textContent=(value||'').replace(/\s/g,'').length+' 字符';
}
function switchMode(mode){
  if(!MODES.includes(mode))return;
  currentMode=mode;
  for(const tab of document.querySelectorAll('[data-mode]')){
    const active=tab.dataset.mode===mode;
    tab.classList.toggle('active',active);
    tab.setAttribute('aria-selected',active?'true':'false');
  }
  renderText();
}
for(const tab of document.querySelectorAll('[data-mode]'))tab.addEventListener('click',()=>switchMode(tab.dataset.mode));
$('result').addEventListener('input',()=>{
  selectedEdits()[currentMode]=$('result').value;
  $('char-info').textContent=$('result').value.replace(/\s/g,'').length+' 字符';
});

$('file').addEventListener('change',e=>chooseFile(e.target.files[0]));
$('drop').addEventListener('dragover',e=>{e.preventDefault();$('drop').classList.add('active');});
$('drop').addEventListener('dragleave',()=> $('drop').classList.remove('active'));
$('drop').addEventListener('drop',e=>{e.preventDefault();$('drop').classList.remove('active');chooseFile(e.dataTransfer.files[0]);});
function chooseFile(file){
  if(!file || uploadBusy || retryBusy)return;
  resetUploadSelection();
  selectedFile=file;
  $('filename').textContent=`${file.name} · ${(file.size/1048576).toFixed(1)} MB`;
}
function resetUploadSelection(){
  retrySource=null;
  selectedFile=null;
  $('file').value='';
  $('filename').textContent='未选择文件';
  $('upload-status').textContent='';
  hide('reset-retry');
  $('start').textContent='提交转录任务';
}
$('reset-retry').addEventListener('click',()=>{if(!uploadBusy && !retryBusy)resetUploadSelection();});
$('start').addEventListener('click',()=>{
  if(uploadBusy || retryBusy || historyBusy)return;
  if(retrySource)return retryJob();
  if(!selectedFile){alert('请先选择音视频文件');return;}
  uploadBusy=true;
  $('start').disabled=true;
  updateHistoryActions();
  $('start').textContent='正在上传…';
  $('upload-status').textContent='正在传输文件…';
  const data=new FormData();
  data.append('file',selectedFile);
  data.append('model_profile',$('model-profile').value);
  const xhr=new XMLHttpRequest();
  xhr.open('POST','/api/jobs');
  xhr.upload.onprogress=e=>{
    if(e.lengthComputable)$('upload-status').textContent=`上传 ${Math.round(100*e.loaded/e.total)}%`;
  };
  xhr.onload=async()=>{
    uploadBusy=false;
    $('start').disabled=false;
    $('start').textContent='提交转录任务';
    updateHistoryActions();
    let response;
    try{response=JSON.parse(xhr.responseText);}catch{response={error:'服务器返回的内容无效'};}
    if(xhr.status!==201){$('upload-status').textContent=response.error||'提交失败';return;}
    resetUploadSelection();
    $('upload-status').textContent='提交成功，已加入后台队列；可关闭或刷新本页面';
    await selectJob(response.id);
    await refreshList();
  };
  xhr.onerror=()=>{
    uploadBusy=false;
    $('start').disabled=false;
    $('start').textContent='提交转录任务';
    updateHistoryActions();
    $('upload-status').textContent='上传连接中断。如果上传已经完成，可在任务列表中找回';
  };
  xhr.send(data);
});

function updateHistoryActions(){
  const busy=historyBusy || retryBusy || uploadBusy;
  const eligible=latestJobs.filter(job=>TERMINAL.has(job.status));
  $('delete-selected').disabled=busy || !selectedJobs.size;
  $('delete-selected').textContent=selectedJobs.size?`删除所选（${selectedJobs.size}）`:'删除所选';
  $('clear-history').disabled=busy || !clearableCount;
  $('load-latest-failed').disabled=busy || !latestFailedId;
  $('retry-current').disabled=busy;
  $('refresh').disabled=busy;
  $('reset-retry').disabled=busy;
  $('start').disabled=busy;
  $('select-all-jobs').disabled=busy || !eligible.length;
  $('select-all-jobs').checked=eligible.length>0 && selectedJobs.size===eligible.length;
  $('select-all-jobs').indeterminate=selectedJobs.size>0 && selectedJobs.size<eligible.length;
}
function setCurrentJob(id){
  if(jobId!==id){
    serverText={article:'',captions:'',raw:''};
    $('copy-feedback').textContent='';
    $('result').value='';
    $('char-info').textContent='';
    hide('progress-panel');hide('stats-panel');hide('result-panel');
  }
  jobId=id;
  if(id)localStorage.setItem('mlx-selected-job',id);
  else localStorage.removeItem('mlx-selected-job');
}
function renderList(jobs){
  for(const id of selectedJobs){
    if(!jobs.some(job=>job.id===id && TERMINAL.has(job.status)))selectedJobs.delete(id);
  }
  updateHistoryActions();
  const body=$('jobs-body');
  if(!jobs.length){body.innerHTML='<tr><td colspan="6" class="empty">暂无任务，先上传一个文件。</td></tr>';return;}
  body.innerHTML=jobs.map(job=>{
    const selected=job.id===jobId ? ' selected' : '';
    const canCancel=!TERMINAL.has(job.status) && job.status!=='cancelling';
    const canDelete=TERMINAL.has(job.status);
    const name=escapeHtml(job.filename);
    return `<tr class="${selected}">
      <td class="select-cell"><input type="checkbox" data-select="${job.id}" aria-label="选择 ${name}" ${selectedJobs.has(job.id)?'checked':''} ${canDelete?'':'disabled'}></td>
      <td class="filecell" title="${name}">${name}<div class="jobid">${escapeHtml(job.model_label||'历史任务')} · ${job.id.slice(0,10)}</div></td>
      <td><span class="status-tag ${escapeHtml(job.status)}">${escapeHtml(stateText(job))}</span></td>
      <td>${Math.round(job.percent||0)}%</td>
      <td class="nowrap">${escapeHtml(dateTime(job.created_at))}</td>
      <td class="nowrap actions-cell">
        <button class="small secondary" data-view="${job.id}">查看</button>
        ${job.status==='error'?`<button class="small secondary" data-retry="${job.id}">加载重试</button>`:''}
        ${canCancel?`<button class="small danger" data-cancel="${job.id}">终止</button>`:''}
        ${canDelete?`<button class="small ghost" data-delete="${job.id}">删除</button>`:''}
      </td>
    </tr>`;
  }).join('');
}
$('jobs-body').addEventListener('click',async e=>{
  const button=e.target.closest('button');
  if(!button)return;
  if(button.dataset.view)await selectJob(button.dataset.view);
  if(button.dataset.retry)await loadFailedJob(button.dataset.retry);
  if(button.dataset.cancel)await cancelJob(button.dataset.cancel);
  if(button.dataset.delete)await deleteJob(button.dataset.delete);
});
$('jobs-body').addEventListener('change',e=>{
  const id=e.target.dataset.select;
  if(!id || historyBusy || retryBusy || uploadBusy)return;
  if(e.target.checked)selectedJobs.add(id);
  else selectedJobs.delete(id);
  updateHistoryActions();
});
$('select-all-jobs').addEventListener('change',e=>{
  if(historyBusy || retryBusy || uploadBusy)return;
  selectedJobs.clear();
  if(e.target.checked)for(const job of latestJobs)if(TERMINAL.has(job.status))selectedJobs.add(job.id);
  renderList(latestJobs);
});
$('delete-selected').addEventListener('click',()=>deleteJobs([...selectedJobs]));
$('clear-history').addEventListener('click',()=>deleteJobs());
$('load-latest-failed').addEventListener('click',()=>loadFailedJob());
$('retry-current').addEventListener('click',()=>jobId&&loadFailedJob(jobId));
$('refresh').addEventListener('click',()=>refreshList());
function refreshList(){
  if(listRequest)return listRequest;
  listRequest=fetchList().finally(()=>{listRequest=null;});
  return listRequest;
}
async function fetchList(){
  try{
    const r=await fetch('/api/jobs',{cache:'no-store'});
    if(!r.ok)throw Error('服务器无法返回任务列表');
    const data=await r.json();
    latestJobs=data.jobs||[];
    clearableCount=data.clearable_count||0;
    latestFailedId=data.latest_failed_id||null;
    // A selected job may be outside this page, or newer than this response.
    if(!jobId)setCurrentJob(latestJobs[0]?.id||null);
    renderList(latestJobs);
    $('history-message').textContent=`共 ${data.total??latestJobs.length} 条任务记录${data.total>latestJobs.length?`，显示最近 ${latestJobs.length} 条`:''} · ${clearableCount} 条可清理`;
  }catch(err){$('history-message').textContent=err.message;}
}
async function selectJob(id){
  if(!id)return;
  setCurrentJob(id);
  renderList(latestJobs);
  if(detailRequest)await detailRequest;
  await refreshDetail();
}
function updateStats(d){
  show('stats-panel');
  $('s-duration').textContent=d.duration?prettyTime(d.duration):'解析中';
  $('s-total').textContent=prettyTime(d.elapsed);
  $('s-recognition').textContent=prettyTime(d.transcribe_elapsed);
  $('s-preprocess').textContent=prettyTime(d.preprocess_elapsed);
  $('s-speed').textContent=d.speed_x?`${Number(d.speed_x).toFixed(2)}×`:'—';
  $('s-words').textContent=(d.char_count||0).toLocaleString();
  $('s-lines').textContent=`${d.cue_count||0} / ${d.paragraph_count||0}`;
  $('s-quality').textContent=String(d.suspect_count||0);
  $('s-model-load').textContent=prettyTime(d.model_load_elapsed);
  $('s-model').textContent=d.model_label||d.model||'历史任务';
  $('s-token-limit').textContent=d.token_limit_chunk_count===undefined?'—':String(d.token_limit_chunk_count);
  $('s-hard-cuts').textContent=String(d.hard_split_count||0);
  const qualityNotes=[Number(d.suspect_count)>0
    ? `仍有 ${d.suspect_count} 个片段建议回听核对。`
    : '暂无异常提示；这不代表内容已经验证。'];
  if(Number(d.token_limit_chunk_count)>0)qualityNotes.push(`其中 ${d.token_limit_chunk_count} 块达到生成长度上限，可能存在缺字，请核对分块记录。`);
  if(d.timestamp_mode==='audio_block')qualityNotes.push('时间轴为音频块范围，未做逐词对齐。');
  qualityNotes.push('可下载统计和分块识别记录查看详情。');
  $('quality-note').textContent=qualityNotes.join('');
  $('format-mode').textContent=d.format_mode||'';
}
function refreshDetail(){
  if(!jobId)return Promise.resolve();
  if(detailRequest)return detailRequest;
  detailRequest=fetchDetail().finally(()=>{detailRequest=null;});
  return detailRequest;
}
async function fetchDetail(){
  const requestedId=jobId;
  try{
    const r=await fetch(`/api/jobs/${requestedId}`,{cache:'no-store'});
    if(r.status===404 && requestedId===jobId){
      if(retrySource?.id===requestedId){resetUploadSelection();$('upload-status').textContent='已加载的失败记录已被删除，请重新选择文件';}
      setCurrentJob(latestJobs.find(job=>job.id!==requestedId)?.id||null);
      throw Error('当前任务已被删除');
    }
    if(!r.ok)throw Error('无法读取当前任务');
    const d=await r.json();
    if(requestedId!==jobId)return;
    show('progress-panel');
    const percent=Math.min(100,Math.max(0,Number(d.percent)||0));
    $('task-title').textContent=d.filename||'当前任务';
    $('task-id').textContent='任务 ID：'+d.id;
    $('stage').textContent=stateText(d);
    $('percent').textContent=Math.round(percent)+'%';
    $('bar').style.width=percent+'%';
    $('progress-track').setAttribute('aria-valuenow',String(percent));
    $('message').textContent=d.message||'';
    $('progress-detail').textContent=`已处理 ${prettyTime(d.processed)} / ${prettyTime(d.duration)} · 已经过 ${prettyTime(d.elapsed)}`;
    const canCancel=!TERMINAL.has(d.status) && d.status!=='cancelling';
    $('cancel-current').classList.toggle('hidden',!canCancel);
    $('retry-current').classList.toggle('hidden',d.status!=='error');
    updateStats(d);
    for(const mode of MODES)serverText[mode]=d[mode]||'';
    const ended=TERMINAL.has(d.status);
    const available=Array.isArray(d.available_downloads)?d.available_downloads:[];
    const hasText=MODES.some(mode=>serverText[mode]);
    if(hasText || (ended && available.length)){
      show('result-panel');
      // Do not overwrite user editing, even during automatic progress polling.
      if(document.activeElement!==$('result'))renderText();
    }else hide('result-panel');
    for(const id of ['download-txt','download-srt','download-captions','download-raw'])$(id).disabled=!(ended && hasText);
    $('download-stats').disabled=!(ended && available.includes('stats'));
    $('download-asr').disabled=!(ended && available.includes('asr'));
    if(d.text_mismatch && ended)$('copy-feedback').textContent='提示：阅读稿与识别原稿的文字存在差异，请核对识别原稿。';
  }catch(err){$('history-message').textContent=err.message;}
}
async function cancelJob(id){
  if(!confirm('确认终止此任务？运行中的 MLX 推理会在当前音频片段结束后停止。'))return;
  try{
    const r=await fetch(`/api/jobs/${id}/cancel`,{method:'POST'});
    const d=await r.json();
    if(!r.ok)throw Error(d.error||'取消失败');
    await refreshList();await refreshDetail();
  }catch(err){alert(err.message);}
}
$('cancel-current').addEventListener('click',()=>jobId&&cancelJob(jobId));
async function deleteJob(id){
  return deleteJobs([id]);
}
async function deleteJobs(ids=null){
  if(historyBusy || retryBusy || uploadBusy || (ids && !ids.length))return;
  const label=ids?`所选 ${ids.length} 条任务记录`:`全部 ${clearableCount} 条已结束的历史记录`;
  if(!confirm(`永久删除${label}及其原始文件、生成文本？排队和运行中的任务会保留。`))return;
  historyBusy=true;
  updateHistoryActions();
  try{
    if(listRequest)await listRequest;
    const r=await fetch('/api/jobs',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify(ids?{ids}:{all:true})});
    const d=await r.json();
    if(!r.ok)throw Error(d.error||'删除失败');
    const deleted=new Set(d.deleted_ids||[]);
    for(const id of deleted){selectedJobs.delete(id);edits.delete(id);}
    if(deleted.has(jobId))setCurrentJob(null);
    if(retrySource && deleted.has(retrySource.id)){
      resetUploadSelection();
      $('upload-status').textContent='已加载的失败记录已删除，请重新选择文件';
    }
    await refreshList();await refreshDetail();
    $('history-action-message').textContent=`已删除 ${d.deleted_count} 条历史记录`;
  }catch(err){$('history-action-message').textContent=err.message;}
  finally{historyBusy=false;updateHistoryActions();}
}
async function loadFailedJob(id=null){
  if(historyBusy || retryBusy || uploadBusy)return;
  retryBusy=true;
  updateHistoryActions();
  try{
    if(listRequest)await listRequest;
    const r=await fetch(id?`/api/jobs/${id}`:'/api/jobs/latest-failed',{cache:'no-store'});
    const d=await r.json();
    if(!r.ok)throw Error(d.error||'无法加载失败记录');
    if(d.status!=='error')throw Error('该任务不是失败记录');
    resetUploadSelection();
    if(d.model_profile)$('model-profile').value=d.model_profile;
    retrySource={id:d.id,filename:d.filename};
    await selectJob(d.id);
    if(retrySource?.id!==d.id)throw Error('失败记录已被删除，请重新选择文件');
    if(!d.can_retry){resetUploadSelection();throw Error('原始文件已不存在，请重新上传音视频文件');}
    $('filename').textContent=`已加载失败记录：${d.filename}`;
    $('start').textContent='重试转录任务';
    show('reset-retry');
    $('upload-status').textContent='已加载原始文件和识别模式，可调整模式后点击“重试转录任务”；将从头识别';
    $('start').scrollIntoView({behavior:'smooth',block:'center'});
  }catch(err){$('upload-status').textContent=err.message;}
  finally{retryBusy=false;updateHistoryActions();}
}
async function retryJob(){
  const source=retrySource;
  if(!source || uploadBusy)return;
  uploadBusy=true;
  updateHistoryActions();
  $('start').textContent='正在提交重试…';
  $('upload-status').textContent='正在复用原始文件并提交重试任务…';
  try{
    if(listRequest)await listRequest;
    const r=await fetch(`/api/jobs/${source.id}/retry`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model_profile:$('model-profile').value})});
    const d=await r.json();
    if(!r.ok)throw Error(d.error||'重试提交失败');
    resetUploadSelection();
    $('upload-status').textContent='重试已加入后台队列，原失败记录和部分结果仍保留';
    await selectJob(d.id);
    await refreshList();
  }catch(err){$('upload-status').textContent=err.message;}
  finally{
    uploadBusy=false;
    $('start').textContent=retrySource?'重试转录任务':'提交转录任务';
    updateHistoryActions();
  }
}
$('copy').addEventListener('click',async()=>{
  const text=$('result').value;
  if(!text){$('copy-feedback').textContent='当前没有可复制的内容';return;}
  try{
    if(navigator.clipboard && window.isSecureContext)await navigator.clipboard.writeText(text);
    else{$('result').focus();$('result').select();if(!document.execCommand('copy'))throw Error('copy');}
    $('copy-feedback').textContent='已复制当前标签的全部文本';
  }catch{$('copy-feedback').textContent='复制失败，请选中文字并按 Command + C';}
});
for(const [id,kind] of [['download-txt','txt'],['download-srt','srt'],['download-captions','captions'],['download-raw','raw'],['download-stats','stats'],['download-asr','asr']]){
  $(id).addEventListener('click',()=>{if(jobId)window.location.href=`/api/jobs/${jobId}/download/${kind}`;});
}
async function tick(){if(historyBusy || retryBusy)return;await refreshList();if(!historyBusy && !retryBusy)await refreshDetail();}
tick();
setInterval(tick,2000);
