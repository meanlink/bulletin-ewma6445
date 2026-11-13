import {datasets} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {table} from '../components/common/table.js';
import {esc,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';
import {parseCSV} from '../utils/csv.js';

const rejectColumns=[
 {key:'batch_seq',label:'拒收批次'},
 {key:'index',label:'批内行号'},
 {key:'reason',label:'拒收原因'},
 {key:'record',label:'原始记录'},
];
const ignoreColumns=[
 {key:'batch_seq',label:'接收批次'},
 {key:'reason',label:'忽略原因'},
 {key:'record',label:'被忽略记录'},
];
const logColumns=[
 {key:'seq',label:'批次'},
 {key:'source',label:'来源'},
 {key:'received',label:'接收'},
 {key:'accepted',label:'并入/修订'},
 {key:'ignored',label:'忽略'},
 {key:'rejected',label:'拒收'},
];

function counters(summary){
 if(!summary)return '';
 return '<div class="intake-counters">'+
  [['当前有效记录',summary.current_rows],['累计接收',summary.received_total],
   ['累计并入/修订',summary.accepted_total],['累计忽略（重复/旧修订）',summary.ignored_total],
   ['累计拒收（坏记录）',summary.rejected_total],['接收批次',summary.batches]]
   .map(([label,value])=>'<div><span>'+esc(label)+'</span><strong>'+esc(value)+'</strong></div>').join('')+
  '</div>';
}

function rejectRows(items){
 return (items||[]).filter(item=>item.kind!=='ignored').slice().reverse().map(item=>({
  batch_seq:item.batch_seq??1,index:item.index??'—',reason:item.reason,
  record:JSON.stringify(item.record),
 }));
}
function ignoreRows(items){
 return (items||[]).filter(item=>item.kind==='ignored').slice().reverse().map(item=>({
  batch_seq:item.batch_seq??'—',reason:item.reason,record:JSON.stringify(item.record),
 }));
}

function detailPanel(d){
 if(!d)return '<div class="empty">尚无批次。</div>';
 const locked=session.user.role==='viewer';
 return '<div class="section-title"><h2 id="preview-name">'+esc(d.name)+'</h2><span>修订第 '+esc(d.revision)+' 版 · 预览前 30 条</span></div>'+
  counters(d.summary)+
  '<p class="hint">核对恒等式：累计接收 = 并入/修订 + 忽略 + 拒收。已批准简报与本地收件箱分发内容为固定快照，补报不会改动它们；需按新数据出结果请新建分析任务。</p>'+
  '<div id="data-preview">'+table(workspace.meta.columns,(d.records||[]).slice(0,30))+'</div>'+
  '<div class="compact"><h3>接收批次留痕</h3>'+table(logColumns,d.ingest_log||[],'暂无批次留痕。')+'</div>'+
  '<div class="compact"><h3>忽略留痕（重复或旧修订，未改变结果）</h3>'+table(ignoreColumns,ignoreRows(d.rejected_records),'没有被忽略的记录。')+'</div>'+
  '<div class="compact"><h3>拒收坏记录（列明原因，不参与任何计算）</h3>'+table(rejectColumns,rejectRows(d.rejected_records),'没有坏记录。')+'</div>'+
  '<div class="compact section-title"><h3>补报 / 更正日记</h3><span>按链路与播出日合并，同日以较新修订为准</span></div>'+
  '<form id="supplement-form">'+
   '<label>补报来源<input name="source" placeholder="例如：late-20261108.csv" value="supplement.json"></label>'+
   '<label>上传 CSV 或 JSON<input type="file" id="supplement-file" accept=".json,.csv"></label>'+
   '<label>补报记录 JSON<textarea name="records" rows="6" placeholder="粘贴晚到日记或同日更正记录" required></textarea></label>'+
   '<p class="hint">同链路同播出日：内容相同视为重复并忽略；内容不同视为更正，以本批较新修订覆盖旧版。坏记录逐条列明原因并拒收。</p>'+
   '<button type="submit" class="primary" '+(locked?'disabled':'')+'>并入既有批次</button>'+
  '</form>';
}

export async function dataPage(){
 workspace.datasets=await datasets.list();
 let selected=workspace.datasets[0]?await datasets.get(workspace.datasets[0].id):null;
 let m=workspace.meta;
 return heading('02 / DATA INTAKE','数据接入、补报与批次追溯','保留原始业务字段、来源和内容指纹。晚到日记按链路与播出日并入既有批次，同日以较新修订为准；分析任务始终绑定一个不可变数据批次。')+
 '<div class="dataset-grid"><section class="panel"><div class="section-title"><h2>业务批次</h2><span>'+workspace.datasets.length+' 批</span></div><div class="batch-list">'+
 workspace.datasets.map(d=>'<button class="batch-item" data-dataset="'+d.id+'"><strong>'+esc(d.name)+'</strong><span>'+d.rows+' 条有效 · 修订第'+esc(d.revision)+'版 · '+esc(d.source)+'</span><small>SHA256 '+esc(d.checksum.slice(0,18))+'…'+(d.rejected_rows?' · 拒收 '+d.rejected_rows:'')+'</small></button>').join('')+
 '</div><div class="section-title"><h2>接入新数据</h2></div><form id="dataset-form"><label>批次名称<input name="name" required minlength="2" placeholder="例如：11 月复测批次"></label><label>上传 CSV 或 JSON<input type="file" id="file-input" accept=".json,.csv"></label><label>记录 JSON<textarea name="records" rows="6" placeholder="粘贴 JSON 数组，或先选择上方文件" required></textarea></label><p class="hint">字段：'+m.columns.map(c=>esc(c.key)+' / '+esc(c.label)).join('，')+'。5–5,000 条记录。</p><button type="submit" class="primary" '+(session.user.role==='viewer'?'disabled':'')+'>校验并接入批次</button></form></section>'+
 '<section class="panel" id="dataset-detail">'+detailPanel(selected)+'</section></div>';
}

function bindFile(inputId,textareaName){
 const input=document.querySelector(inputId);
 if(!input)return;
 input.addEventListener('change',async e=>{
  try{
   const file=e.target.files[0];if(!file)return;
   const text=await file.text();
   const records=file.name.endsWith('.csv')?parseCSV(text):JSON.parse(text);
   const form=input.closest('form');
   form.querySelector('[name=records]').value=JSON.stringify(records,null,2);
   const sourceInput=form.querySelector('[name=source]');
   if(sourceInput&&!sourceInput.dataset.touched)sourceInput.value=file.name;
  }catch(error){notify(error.message,true);}
 });
 const form=input.closest('form');
 const sourceInput=form?.querySelector('[name=source]');
 if(sourceInput)sourceInput.addEventListener('input',()=>sourceInput.dataset.touched='1');
}

export function bindData(refresh){
 bindFile('#file-input');
 bindFile('#supplement-file');
 document.querySelectorAll('[data-dataset]').forEach(button=>button.onclick=async()=>{
  try{
   const d=await datasets.get(button.dataset.dataset);
   document.querySelector('#dataset-detail').innerHTML=detailPanel(d);
   bindFile('#supplement-file');
   bindSupplement(refresh,d.id);
  }catch(error){notify(error.message,true);}
 });
 submit(document.querySelector('#dataset-form'),async f=>{
  await datasets.create({name:f.get('name'),source:f.get('source')||'manual.json',records:JSON.parse(f.get('records'))});
  notify('数据已接入，可前往工作台执行分析');await refresh();
 });
 const first=workspace.datasets[0];
 if(first)bindSupplement(refresh,first.id);
}

function bindSupplement(refresh,datasetId){
 const form=document.querySelector('#supplement-form');
 if(!form)return;
 submit(form,async f=>{
  const result=await datasets.supplement(datasetId,{
   source:f.get('source')||'supplement.json',
   records:JSON.parse(f.get('records')),
  });
  const b=result.batch;
  notify('第'+b.seq+'批已并入：接收'+b.received+'，并入/修订'+b.accepted+'，忽略'+b.ignored+'，拒收'+b.rejected);
  await refresh();
 });
}
