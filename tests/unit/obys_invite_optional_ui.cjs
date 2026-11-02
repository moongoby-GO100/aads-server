const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const html=fs.readFileSync('app/static/apps/obys/admin.html','utf8');
const source=html.slice(html.indexOf('    function renderEmployeeOnboardingV41()'),html.indexOf('    async function loadJoinRequestsV41'));
const els=Object.fromEntries(['empInviteResultV41','empInviteCopyV41','empInviteCopyStatusV41','empInviteLinkV41'].map(id=>[id,{focus(){},select(){this.selected=true}}]));
const writes=[],copies=[],toasts=[];
let fail=false;
const ctx=vm.createContext({location:{origin:'https://fixture.test'},document:{getElementById:id=>els[id]},navigator:{clipboard:{writeText:async s=>{if(fail)throw Error('denied');copies.push(s)}}},showToast:s=>toasts.push(s),escapeHtml:s=>String(s),financeApiV41:async(u,o)=>{writes.push(JSON.parse(o.body));return {invite:{token:'fixture-token',branch_labels:['테스트 매장']}}},console});
vm.runInContext(source+';loadInvitesV41=async()=>{}',ctx);
const form={email:{value:''},name:{value:'테스트 직원'},phone:{value:''},querySelectorAll:()=>[{dataset:{businessId:'fixture-biz',branch:'',label:'테스트 매장'}}],reset(){}};
(async()=>{
 ctx.form=form;await vm.runInContext('createInviteV41(form)',ctx);
 assert.equal(writes[0].email,'');assert(!els.empInviteResultV41.innerHTML.includes('yf_invite_email'));assert.equal(copies.length,0);
 await els.empInviteCopyV41.onclick();assert.equal(copies.length,1);assert.match(copies[0],/yf_invite_token=fixture-token$/);
 fail=true;await els.empInviteCopyV41.onclick();assert.equal(els.empInviteLinkV41.selected,true);assert.match(els.empInviteCopyStatusV41.textContent,/Ctrl\+C/);
 fail=false;await els.empInviteCopyV41.onclick();assert.equal(copies.length,2);
 form.email.value='bad';await vm.runInContext('createInviteV41(form)',ctx);assert.equal(writes.length,1);assert.match(toasts.at(-1),/形式|형식/);
 form.email.value=' Staff@Example.com ';await vm.runInContext('createInviteV41(form)',ctx);assert.equal(writes[1].email,'staff@example.com');assert.match(els.empInviteResultV41.innerHTML,/yf_invite_email=staff%40example.com/);
 console.log('PASS: real createInviteV41 function: blank/invalid/valid email, conditional hint, explicit copy/retry/manual selection');
})().catch(e=>{console.error(e);process.exitCode=1});
