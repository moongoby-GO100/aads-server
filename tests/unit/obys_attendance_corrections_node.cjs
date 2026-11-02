/* Execute the production renderer/handlers with a small DOM fixture, no network. */
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('app/static/apps/obys/modules/attendance-corrections.js','utf8');
const element=()=>({disabled:false,hidden:false,textContent:'',style:{},value:'',setAttribute(){}});
function hostFixture(manager) {
 const host={...element(),isConnected:true}, message=element(), refresh=element(), login=element(), parent=element(), list=element();
 const form=manager?null:{...element(),elements:{},reportValidity:()=>true,scrollIntoView(){},querySelector:()=>parent};
 if(form) for(const name of ['record_id','work_date','start_at','end_at','break_minutes','reason']) form.elements[name]=element();
 let actions=[];
 Object.defineProperty(list,'innerHTML',{get(){return this.html||''},set(value){this.html=value;actions=[...value.matchAll(/data-action="([^"]+)"/g)].map(m=>({...element(),action:m[1]}));}});
 Object.defineProperty(host,'innerHTML',{set(value){this.html=value;},get(){return this.html;}});
 host.querySelector=selector=>({'form':form,'[data-list]':list,'[data-message]':message,'[data-refresh]':refresh,'[data-login]':login}[selector]);
 host.querySelectorAll=selector=>selector.includes('data-action')?[...(form?Object.values(form.elements):[]),...actions]:[refresh];
 return {host,form,list,message,refresh,login,actions:()=>actions};
}
class FormDataFixture {constructor(form){this.entries=Object.entries(form.elements).map(([key,input])=>[key,String(input.value)]);} [Symbol.iterator](){return this.entries[Symbol.iterator]();}}
const context={window:{},FormData:FormDataFixture,WeakMap,Date,Promise,Error,encodeURIComponent,
 setTimeout:(fn,delay)=>setTimeout(fn,delay===15000?30:delay),clearTimeout};
vm.runInNewContext(source,context);
const mount=context.window.ObysAttendanceCorrections.mount;
const flush=async()=>{for(let i=0;i<10;i++)await new Promise(resolve=>setImmediate(resolve));};
let items=[],writes=[],mode='ok',saved=0;
const record={id:'r/1',work_date:'2026-11-01',start_at:'09:00:00',end_at:'18:00:00',break_minutes:60};
async function api(url,options={}) {
 if(!options.method){
  if(mode==='read403')throw Object.assign(new Error('권한'),{status:403});
  return url.endsWith('/clock/state')?{recent:[record]}:{items:structuredClone(items)};
 }
 const payload=JSON.parse(options.body);writes.push({url,payload});
 if(mode==='timeout')return new Promise(()=>{});
 if(['401','403','409'].includes(mode))throw Object.assign(new Error('마감·중복·원본변경'),{status:Number(mode)});
 let row;
 if(url.includes('/records/')){
  assert.ok(url.includes('r%2F1'));assert.equal(typeof payload.break_minutes,'number');assert.equal(payload.record_id,undefined);
  row={id:'c'+(items.length+1),record_id:record.id,employee_email:'me@example.test',status:'pending',version:1,reason:payload.reason,requested:payload,original:record,supersedes_id:payload.supersedes_id};items.unshift(row);
 }else{
  row=items.find(row=>url.includes('/'+row.id+'/'));assert.equal(payload.version,row.version);
  row.status=url.endsWith('/approve')?'approved':url.endsWith('/reject')?'rejected':'withdrawn';row.version++;row.decision_reason=payload.reason;
 }
 if(mode==='lost')throw new Error('response lost after commit');
 return mode==='malformed'?{}:structuredClone(row);
}
const setup=async manager=>{const ui=hostFixture(manager);await mount(ui.host,{api,businessId:'b/1',manager,onSaved:()=>{saved++;}});return ui;};
function action(ui,id,name,reason='확인 사유') {
 const article={dataset:{id},querySelector:()=>({value:reason})};
 const button={dataset:{action:name},closest:()=>article};
 ui.list.onclick({target:{closest:()=>button}});
}
function submit(ui){const f=ui.form;f.elements.record_id.value=record.id;f.elements.record_id.onchange();f.elements.work_date.value='2026-11-02';f.elements.break_minutes.value='0';f.elements.reason.value='<img onerror=alert(1)>';f.onsubmit({preventDefault(){}});}
(async()=>{
 let checks=0,ui=await setup(false);submit(ui);submit(ui);await flush();
 assert.equal(writes.length,1);assert.equal(items[0].status,'pending');assert.ok(ui.list.innerHTML.includes('&lt;img'));assert.ok(ui.list.innerHTML.includes('2026-11-02 (월)'));checks+=4;
 action(ui,'c1','withdraw');await flush();assert.equal(items[0].status,'withdrawn');checks++;
 action(ui,'c1','reapply');ui.form.onsubmit({preventDefault(){}});await flush();assert.equal(writes.at(-1).payload.supersedes_id,'c1');checks++;
 ui=await setup(true);action(ui,'c2','reject','반려 사유');await flush();assert.equal(items[0].status,'rejected');assert.ok(ui.list.innerHTML.includes('반려 사유'));checks+=2;
 ui=await setup(false);action(ui,'c2','reapply');ui.form.onsubmit({preventDefault(){}});await flush();
 ui=await setup(true);action(ui,'c3','approve');action(ui,'c3','approve');await flush();assert.equal(items[0].status,'approved');assert.equal(writes.length,6);checks+=2;
 for(const failure of ['401','403','409','lost','malformed','timeout']) {
  items=[];mode='ok';ui=await setup(false);mode=failure;submit(ui);await flush();
  if(failure==='timeout')await new Promise(resolve=>setTimeout(resolve,50));
  assert.equal(ui.form.elements.record_id.disabled,true);const count=writes.length;submit(ui);assert.equal(writes.length,count);checks+=2;
  assert.match(ui.message.textContent,/세션이 만료|권한이 없습니다|충돌|처리 결과를 확인/);checks++;
  mode='ok';await ui.refresh.onclick();assert.equal(writes.length,count);assert.equal(ui.form.elements.record_id.disabled,false);checks+=2;
  if(['lost','malformed'].includes(failure))assert.ok(ui.list.innerHTML.includes('확인대기'));
 }
 mode='read403';await ui.refresh.onclick();assert.equal(ui.login.hidden,false);checks++;
 mode='ok';ui=await setup(true);ui.host.isConnected=false;const count=writes.length;action(ui,items[0]?.id,'approve');assert.equal(writes.length,count);checks++;
 assert.ok(saved>=6);checks++;
 console.log(JSON.stringify({result:'PASS',checks,fixture:true,renderer:'production',browser:false}));
})().catch(error=>{console.error(error);process.exitCode=1});
