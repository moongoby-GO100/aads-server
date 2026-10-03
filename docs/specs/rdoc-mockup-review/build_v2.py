#!/usr/bin/env python3
"""v2 시안 생성기: preview-v1/v2.html, mockup-v2.html, artifact-v2.html 을 같은 소스에서 만든다.

재실행하면 같은 바이트가 나온다(시각·난수 미사용). v1 파일은 건드리지 않는다.
"""
from __future__ import annotations

import hashlib
import html
from pathlib import Path

ROOT = Path(__file__).resolve().parent

PREVIEW_CSS = (
    "*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,sans-serif;color:#1f2937;background:#f8fafc}"
    "header{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 14px;background:#fff;border-bottom:1px solid #d1d5db}"
    "header b{font-size:15px}.tag{padding:2px 8px;border-radius:99px;background:#eef2ff;color:#3730a3;font-size:12px}"
    ".warn{background:#fef3c7;color:#92400e}main{display:grid;grid-template-columns:1fr 220px;gap:12px;padding:12px}"
    "section,aside{background:#fff;border:1px solid #d1d5db;border-radius:8px;padding:12px;min-width:0}"
    "h2{margin:0 0 8px;font-size:14px}ul{margin:0;padding-left:18px}.wire{height:84px;border:2px dashed #94a3b8;border-radius:6px;display:grid;place-items:center;color:#64748b;margin-bottom:8px}"
    ".btn{display:block;width:100%;margin:6px 0;padding:8px;border-radius:6px;border:1px solid #1d4ed8;background:#2563eb;color:#fff;font-weight:600;text-align:center}"
    ".btn.alt{background:#fff;color:#1d4ed8}.bar{position:sticky;bottom:0;display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 14px;background:#fff;border-top:2px solid #2563eb}"
    ".bar .btn{width:auto;margin:0;padding:8px 14px}.bar span{flex:1;min-width:160px;font-size:13px}"
    "@media(max-width:520px){main{grid-template-columns:1fr}}"
)

PREVIEW_V1 = (
    '<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
    f"<title>MR-01 v1</title><style>{PREVIEW_CSS}</style></head><body>"
    '<header><b>목업 검토 · MR-01</b><span class="tag">revision 1</span><span class="tag warn">검토 가능 · 미승인</span></header>'
    '<main><div><section><h2>화면 미리보기</h2><div class="wire">전체 목업 영역(합성 예시)</div>'
    "<h2>변경점</h2><ul><li>신규 화면: 목업 검토</li><li>데스크톱·모바일 캡처 연결</li></ul></section></div>"
    '<aside><h2>결정 (v1: 오른쪽 위)</h2><span class="btn" title="정적 미리보기라 동작하지 않음">승인</span>'
    '<span class="btn alt" title="정적 미리보기라 동작하지 않음">수정 요청</span><p>승인 대상: MR-01 · revision 1</p></aside></main>'
    "</body></html>"
)

PREVIEW_V2 = (
    '<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
    f"<title>MR-01 v2</title><style>{PREVIEW_CSS}</style></head><body>"
    '<header><b>목업 검토 · MR-01</b><span class="tag">revision 2</span><span class="tag warn">검토 가능 · 미승인(승계 없음)</span></header>'
    '<main style="grid-template-columns:1fr"><div><section><h2>화면 미리보기</h2><div class="wire">전체 목업 영역(합성 예시)</div>'
    "<h2>변경점</h2><ul><li>신규 화면: 목업 검토</li><li>데스크톱·모바일 캡처 연결</li>"
    "<li><b>CR-0001 반영:</b> 승인·수정 요청 버튼을 화면 아래 고정 바로 이동</li></ul></section></div></main>"
    '<div class="bar"><span>승인 대상: MR-01 · revision 2</span><span class="btn alt" title="정적 미리보기라 동작하지 않음">수정 요청</span>'
    '<span class="btn" title="정적 미리보기라 동작하지 않음">승인</span></div></body></html>'
)

H1 = hashlib.sha256(PREVIEW_V1.encode("utf-8")).hexdigest()
H2 = hashlib.sha256(PREVIEW_V2.encode("utf-8")).hexdigest()


def attr(text: str) -> str:
    return html.escape(text, quote=True)


MOCKUP_TEMPLATE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>R-DOC 목업 검토 v2 · 채팅 아티팩트 중심 시연</title>
<style>
*{box-sizing:border-box}html,body{margin:0;height:100%}body{font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:#111827;background:#eef1f6;overflow-wrap:anywhere}
button,select,textarea,input{font:inherit}button{cursor:pointer}:focus-visible{outline:3px solid #f59e0b;outline-offset:2px}
.demo{background:#7c2d12;color:#fff;padding:6px 12px;font-weight:600;font-size:13px}
.steps{display:flex;flex-wrap:wrap;gap:6px;padding:8px 12px;background:#fff;border-bottom:1px solid #d1d5db;margin:0;list-style:none}
.steps li{padding:2px 8px;border-radius:99px;background:#e5e7eb;color:#374151;font-size:12px}.steps li.done{background:#dcfce7;color:#14532d}.steps li.now{background:#dbeafe;color:#1e3a8a;font-weight:700}
.app{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.05fr);height:calc(100vh - 118px);min-height:560px}
.chat,.panel{display:flex;flex-direction:column;min-height:0;min-width:0;background:#fff}.chat{border-right:1px solid #d1d5db}
h1{font-size:15px;margin:0;padding:10px 12px;border-bottom:1px solid #e5e7eb}
.msgs{flex:1;overflow:auto;padding:12px;display:flex;flex-direction:column;gap:10px;background:#f9fafb}
.m{max-width:94%;padding:8px 10px;border-radius:10px;border:1px solid #d1d5db;background:#fff}.m.me{align-self:flex-end;background:#dbeafe;border-color:#93c5fd}
.m small{display:block;color:#4b5563}.card{margin-top:6px;padding:8px;border:1px solid #6366f1;border-radius:8px;background:#eef2ff}
.card.old{opacity:.85;border-style:dashed}.kv{font:12px/1.5 ui-monospace,monospace;color:#374151}
.badge{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;background:#fef3c7;color:#78350f}.badge.ok{background:#dcfce7;color:#14532d}.badge.run{background:#dbeafe;color:#1e3a8a}.badge.bad{background:#fee2e2;color:#7f1d1d}
.btn{padding:6px 10px;border-radius:6px;border:1px solid #1d4ed8;background:#2563eb;color:#fff;font-weight:600}.btn.alt{background:#fff;color:#1d4ed8}.btn:disabled{opacity:.45;cursor:not-allowed}
.row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:6px}
.composer{border-top:1px solid #d1d5db;padding:8px 12px;background:#fff}.chip{display:inline-block;padding:2px 8px;border-radius:99px;background:#ede9fe;color:#4c1d95;font-size:12px}
textarea{width:100%;min-height:54px;resize:vertical;border:1px solid #9ca3af;border-radius:6px;padding:6px}
.notice{min-height:20px;color:#7f1d1d;font-size:13px}
.tabs{display:flex;gap:4px;padding:8px 12px 0;border-bottom:1px solid #e5e7eb;flex-wrap:wrap}.tabs button{border:1px solid #d1d5db;border-bottom:0;border-radius:6px 6px 0 0;background:#f3f4f6;padding:5px 10px}.tabs button[aria-selected=true]{background:#fff;font-weight:700;border-color:#6366f1}
.pbody{flex:1;overflow:auto;padding:10px 12px;min-height:0}.pbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:8px 12px;border-bottom:1px solid #e5e7eb}
.back{display:none}.frames{display:grid;gap:8px}.frames.two{grid-template-columns:1fr 1fr}
iframe{width:100%;height:300px;border:1px solid #9ca3af;border-radius:6px;background:#fff}
.trust{border-top:3px solid #047857;background:#ecfdf5;padding:8px 12px}.trust .lab{font-size:12px;color:#065f46;font-weight:700}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid #d1d5db;padding:4px 6px;text-align:left;vertical-align:top}
.edge{background:#fffbeb;border-top:1px solid #fcd34d;padding:6px 12px;font-size:13px}.edge summary{font-weight:700;cursor:pointer}
.log{max-height:92px;overflow:auto;background:#111827;color:#d1fae5;font:12px/1.5 ui-monospace,monospace;padding:6px;border-radius:6px;margin-top:6px}
dialog{max-width:min(520px,94vw);border:2px solid #047857;border-radius:10px;padding:14px}dialog::backdrop{background:rgba(0,0,0,.45)}
.overlay{position:fixed;inset:0;z-index:50;background:rgba(17,24,39,.8);color:#fff;display:none;place-items:center;text-align:center;padding:20px}.overlay.on{display:grid}
@media(max-width:860px){.app{grid-template-columns:1fr;height:auto;min-height:0}.chat{height:calc(100vh - 150px);min-height:480px}
.panel{position:fixed;inset:0;z-index:30;display:none}.panel.open{display:flex}.back{display:inline-block}.frames.two{grid-template-columns:1fr}iframe{height:260px}}
</style></head><body>
<div class="demo" role="note">시연(데모) — 모든 동작은 이 페이지 메모리에서만 일어납니다. 운영 승인·DB 저장·작업 실행·네트워크 요청 없음. 데이터는 합성 예시입니다.</div>
<ol class="steps" id="steps" aria-label="시연 단계"></ol>
<div class="app">
 <section class="chat" aria-label="채팅 대화">
  <h1>채팅 · 같은 대화 문맥 <span class="badge">기존 /chat 화면 개정안(After)</span></h1>
  <div class="msgs" id="msgs" aria-live="polite"></div>
  <div class="composer">
   <div class="row" style="margin:0 0 6px"><span class="chip" id="replyChip"></span><button class="btn alt" id="fillReq" type="button">예시 요청 채우기: 승인 버튼을 아래로 옮겨줘</button></div>
   <label for="draft" class="kv">추가 지시 (수정 요청은 승인·구현 명령이 아님)</label>
   <textarea id="draft" placeholder="예: 승인 버튼을 아래로 옮겨줘"></textarea>
   <div class="row"><button class="btn" id="send" type="button">보내기</button><span class="notice" id="notice" role="alert"></span></div>
  </div>
  <details class="edge"><summary>예외 시연 (명세 계약을 눌러서 확인)</summary>
   <div class="row"><label><input type="checkbox" id="ambig"> 복수 후보(다른 세션 화면 포함)</label><label><input type="checkbox" id="fail"> 승인 저장 실패 켜기</label></div>
   <div class="row"><button class="btn alt" id="eDup" type="button">중복 전송</button><button class="btn alt" id="eLate" type="button">늦게 온 구버전 결과</button><button class="btn alt" id="eReconn" type="button">연결 끊김→재접속</button><button class="btn alt" id="eMore" type="button">생성 중 추가 요청 채우기</button></div>
   <div class="log" id="log" aria-label="시연 이벤트 로그(네트워크 없음)"></div>
  </details>
 </section>
 <section class="panel" id="panel" aria-label="아티팩트 패널">
  <div class="pbar"><button class="btn alt back" id="back" type="button">← 대화로</button><b>아티팩트 패널</b><label>버전 <select id="ver" aria-label="표시할 버전"></select></label><span id="pstat"></span></div>
  <div class="tabs" role="tablist"><button role="tab" data-tab="preview">미리보기</button><button role="tab" data-tab="compare">v1/v2 비교</button><button role="tab" data-tab="changes">변경점</button></div>
  <div class="pbody" id="pbody"></div>
  <div class="trust"><div class="lab">신뢰 영역 (채팅 UI/서버 제공 · 시안 HTML 밖)</div><div class="kv" id="target"></div>
   <div class="row"><button class="btn alt" id="reqBtn" type="button">수정 요청 (채팅 입력으로)</button><button class="btn" id="apprBtn" type="button">승인 확인 (시연)</button></div><div class="notice" id="pnotice" role="alert"></div></div>
 </section>
</div>
<dialog id="dlg" aria-labelledby="dlgT"><h2 id="dlgT" style="margin-top:0"></h2><div id="dlgB"></div><div class="row" id="dlgA"></div></dialog>
<div class="overlay" id="off" role="alert">연결이 끊겼습니다. 입력은 이 기기에 보존됩니다…</div>
<script>
"use strict";
const P={1:@@PV1JS@@,2:@@PV2JS@@};
const HASH={1:"@@H1@@",2:"@@H2@@"};
const ART={1:"A-0001",2:"A-0002"};
const S={mid:415,latest:1,view:1,tab:"preview",phase:"idle",msgs:[],crs:[],approved:{},steps:{},target:null,cn:0,log:[],gen:false,queue:[],apprKey:null};
const $=id=>document.getElementById(id);
const sh=v=>"sha256:"+(HASH[v]||HASH[2]).slice(0,12)+"…";
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const mobile=()=>matchMedia("(max-width:860px)").matches;
function pn(t){$("pnotice").textContent=t}
function lg(t){S.log.push(t);$("log").textContent=S.log.slice(-40).join("\n");$("log").scrollTop=1e6}
function step(n){S.steps[n]=1}
function nid(){return "M-0"+(S.mid++)}
function say(m){S.msgs.push(m)}
function card(v){return {v}}
say({id:"M-0412",who:"ai",t:"12:31",html:"R-DOC 목업 검토 보고 — 판정: 검토 가능. 아래 카드는 이 대화의 아티팩트 패널에서 같은 화면으로 열립니다.",card:1});
function cardHtml(v){const old=v<S.latest;return '<div class="card'+(old?" old":"")+'"><b>MR-01 목업 검토 화면 · v'+v+'</b> <span class="badge'+(S.approved[v]?" ok":"")+'">'+(S.approved[v]?"시연 승인":(old?"이전 버전(최신 v"+S.latest+")":(S.phase==="editing"?"수정중":"미승인")))+'</span>'+
'<div class="kv">review rv-demo-0001 · screen MR-01 · revision '+v+'<br>artifact '+ART[v]+' · '+sh(v)+'</div><div class="row"><button class="btn" data-act="open" data-v="'+v+'">패널에서 열기</button>'+(v>1?'<button class="btn alt" data-act="cmp">v1/v2 비교</button>':"")+'</div></div>'}
function render(){
 $("steps").innerHTML=[["1","보고된 v1 보기"],["2","추가 요청"],["3","대상 v1 확정"],["4","수정중"],["5","v2 변경점 재보고"],["6","v1/v2 비교"],["7","v2 승인 확인/재수정"]].map(([n,l])=>'<li class="'+(S.steps[n]?"done":(S.steps[n-1]||n==="1")&&!S.steps[n]?"now":"")+'">'+n+". "+l+"</li>").join("");
 $("msgs").innerHTML=S.msgs.map(m=>'<div class="m'+(m.who==="me"?" me":"")+'"><small>'+m.id+" · "+m.t+(m.reply?" · ↩ 답장 "+m.reply:"")+"</small>"+m.html+(m.card?cardHtml(m.card):"")+"</div>").join("");
 $("msgs").scrollTop=1e6;
 $("ver").innerHTML=Object.keys(P).filter(v=>v<=S.latest).map(v=>'<option value="'+v+'"'+(+v===S.view?" selected":"")+">v"+v+(+v===S.latest?" (최신)":"")+"</option>").join("");
 document.querySelectorAll(".tabs button").forEach(b=>b.setAttribute("aria-selected",String(b.dataset.tab===S.tab)));
 const v=S.view,old=v<S.latest;
 $("pstat").innerHTML=S.phase==="editing"?'<span class="badge run">v'+(S.latest+1)+' 작성 중 · v'+S.latest+' 보존(불변)</span>':(old?'<span class="badge bad">구버전 열람 중</span>':'<span class="badge">최신</span>');
 let b="";
 if(S.latest>v&&S.tab==="preview")b+='<p class="badge ok">새 버전 v'+S.latest+' 있음 <button class="btn alt" data-act="open" data-v="'+S.latest+'">v'+S.latest+' 보기</button></p>';
 if(S.tab==="preview")b+='<p class="kv">정적 미리보기(iframe sandbox="", 스크립트 없음) — 안의 버튼은 눌러도 동작하지 않습니다.</p><iframe sandbox="" title="MR-01 v'+v+' 미리보기" srcdoc="'+esc(P[v]||P[2])+'"></iframe>';
 else if(S.tab==="compare")b+=S.latest<2?'<p>v2 가 생성되면 비교할 수 있습니다.</p>':'<div class="frames two"><div><b>v1 (원본 보존)</b><iframe sandbox="" title="v1" srcdoc="'+esc(P[1])+'"></iframe></div><div><b>v'+S.latest+' (수정본)</b><iframe sandbox="" title="v2" srcdoc="'+esc(P[2])+'"></iframe></div></div><ul><li>변경: 결정 영역 오른쪽 위 → 화면 아래 고정 바</li><li>불변: 목업 본문·변경점 목록·캡처 연결</li></ul>';
 else b+='<table><tr><th>요청</th><th>원문(source_message)</th><th>base</th><th>결과</th></tr>'+(S.crs.length?S.crs.map(c=>"<tr><td>"+c.id+"</td><td>"+esc(c.text)+"<br><span class='kv'>"+c.src+"</span></td><td>rev "+c.base+"</td><td>"+c.res+"</td></tr>").join(""):'<tr><td colspan="4">접수된 수정 요청 없음</td></tr>')+"</table>";
 $("pbody").innerHTML=b;
 $("target").innerHTML="승인 대상(정확한 버전): <b>MR-01 · v"+v+" · "+ART[v]+" · "+sh(v)+"</b>"+(S.approved[v]?" · 시연 승인됨":" · 미승인");
 $("apprBtn").disabled=S.phase==="editing";
 $("replyChip").textContent="↩ 답장 대상: MR-01 v"+S.view+" · "+ART[S.view]+" · M-0412";
 $("panel").classList.toggle("open",!!S.pOpen&&mobile());
}
function openArt(v){pn("");S.view=v;S.tab="preview";S.pOpen=true;step(1);render()}
function ask(title,body,btns){return new Promise(res=>{$("dlgT").textContent=title;$("dlgB").innerHTML=body;$("dlgA").innerHTML="";btns.forEach(([l,val,cls])=>{const b=document.createElement("button");b.type="button";b.className="btn"+(cls||"");b.textContent=l;b.onclick=()=>{$("dlg").close();res(val)};$("dlgA").appendChild(b)});$("dlg").onclose=()=>res(undefined);$("dlg").showModal()})}
async function resolveTarget(){
 const c=[];
 if(S.view<S.latest){c.push(["MR-01 v"+S.view+" 기준으로 수정 (base_revision="+S.view+")",{v:S.view}],["MR-01 v"+S.latest+" 기준으로 수정 (최신, base_revision="+S.latest+")",{v:S.latest}])}
 if($("ambig").checked){if(!c.length)c.push(["MR-01 v"+S.view+" (이 대화 · M-0412)",{v:S.view}]);c.push(["MR-02 v3 (다른 세션 s-71c2 · 자동 선택 안 함)",{other:true}])}
 if(!c.length){return {v:S.view}}
 const r=await ask("수정할 대상을 선택해 주세요 (1회)","<p>대상 후보가 여러 개입니다. 선택한 버전만 수정하며, 다른 세션·화면은 최신이라는 이유로 임의 수정하지 않습니다.</p>",c.map(([l,v],i)=>[l,v,i?" alt":""]).concat([["취소",null," alt"]]));
 return r}
async function send(dup){
 const text=dup?S.lastText:$("draft").value.trim();
 if(!text){$("notice").textContent="요청 내용을 입력해 주세요. 빈 요청은 접수하지 않습니다.";return}
 $("notice").textContent="";
 if(dup){lg("중복 전송: change_request_id="+S.lastCr+" 재사용 → 새 버전 0건, 접수 카드 1개 유지");say({id:nid(),who:"ai",t:"12:36",html:'이미 접수된 요청입니다 (<span class="kv">'+S.lastCr+"</span>). 같은 요청에서 새 버전을 만들지 않았습니다."});render();return}
 let t=await resolveTarget();
 if(t===null||t===undefined){$("notice").textContent="대상 선택이 취소되어 접수하지 않았습니다. 입력은 보존됩니다.";return}
 if(t.other){say({id:nid(),who:"ai",t:"12:33",html:"다른 세션 화면(MR-02)은 이 요청의 대상이 아닙니다. 이 대화의 MR-01 v"+S.view+" 로 다시 선택해 주세요. 입력은 보존됩니다."});render();return}
 S.cn++;const id="CR-"+String(S.cn).padStart(4,"0"),mid=nid();
 S.lastText=text;S.lastCr=id;$("draft").value="";step(2);
 say({id:mid,who:"me",t:"12:34",reply:"M-0412",html:esc(text)});
 const cr={id,text,src:mid,base:t.v,res:S.gen?"대기: v"+(S.latest+1)+" 완료 뒤 base_revision="+(S.latest+1)+" 로 처리":"처리 중"};S.crs.push(cr);
 lg("POST change_request {id:"+id+", source_message_id:"+mid+", base_revision:"+t.v+", idempotency_key:"+id+"@"+t.v+"}");
 say({id:nid(),who:"ai",t:"12:34",html:'<b>수정 요청 접수 '+id+'</b><div class="kv">선택한 대상: MR-01 v'+t.v+" · "+ART[t.v]+" · "+sh(t.v)+"<br>base_revision "+t.v+" · source "+mid+"</div>원본 v"+t.v+" 는 그대로 보존합니다. 이 요청은 <b>승인·구현 명령이 아닙니다</b>. "+(S.gen?"생성 진행 중이라 완료 뒤 별도로 처리합니다.":"")});
 step(3);
 if(S.gen){S.queue.push(cr);render();return}
 if(S.latest>1)step(7);S.gen=true;S.phase="editing";step(4);S.view=t.v;render();
 setTimeout(()=>finish(cr,t.v),1400)}
function finish(cr,base){
 S.latest=S.latest+1;const nv=S.latest;S.gen=false;S.phase="idle";cr.res="반영 — 결정 영역을 화면 아래 고정 바로 이동";
 S.approved[nv]=false;step(5);
 lg("새 불변 버전 v"+nv+" 생성 · parent=v"+base+" · 승인 이력 승계 0건 · head.generation 증가");
 say({id:nid(),who:"ai",t:"12:36",html:"<b>v"+nv+" 변경점 재보고</b> — 판정: 검토 가능(미승인)<br>요청별 처리: "+cr.id+" 반영 / 미반영 0건. 원본 v"+base+" 은 비교용으로 남아 있습니다.",card:nv});
 if(S.queue.length){const q=S.queue.shift();q.res="접수됨 — v"+nv+" 기준 새 작업 대기(이 시연에서는 생성하지 않음)";lg(q.id+" base_revision "+q.base+" → "+nv+" 로 재정렬 필요: 사용자 확인 1회")}
 render()}
async function approve(){
 if(S.phase==="editing")return;
 const v=S.view;S.apprKey=S.apprKey&&S.apprKey.v===v?S.apprKey:{v,k:"ap-"+ART[v]+"-"+(++S.cn)};
 const r=await ask("승인 확인 (시연)","<p class='kv'>대상: MR-01 · v"+v+" · "+ART[v]+" · "+sh(v)+"<br>idempotency_key "+S.apprKey.k+"</p><p>위 버전을 확인했습니다. 이 클릭은 시연이며 운영 승인으로 저장되지 않습니다.</p>",[["확정(시연)",1],["취소",0," alt"]]);
 if(!r)return;
 if(v<S.latest){lg("승인 거절 409 stale_revision: 요청 v"+v+" ≠ 최신 v"+S.latest+" · 이벤트 0건");pn("거절 409 stale_revision — v"+v+" 은 최신(v"+S.latest+")이 아닙니다. 승인 기록 없음.");say({id:nid(),who:"ai",t:"12:38",html:'<span class="badge bad">거절 409 stale_revision</span> 승인 대상 v'+v+' 은 최신이 아닙니다(최신 v'+S.latest+'). 승인 기록은 만들지 않았습니다. <button class="btn alt" data-act="open" data-v="'+S.latest+'">최신 v'+S.latest+' 열기</button>'});render();return}
 if($("fail").checked){lg("승인 저장 실패(시연) · 이벤트 0건 · 같은 idempotency_key 재시도 가능");pn("저장 실패 — 승인이 기록되지 않았습니다. 같은 키로 재시도할 수 있습니다.");say({id:nid(),who:"ai",t:"12:38",html:'<span class="badge bad">저장 실패</span> 승인이 기록되지 않았습니다. 입력과 대상(v'+v+')은 유지됩니다. 실패 시연을 끄고 다시 누르면 같은 키 '+S.apprKey.k+' 로 재시도합니다.'});render();return}
 S.approved[v]=true;S.apprKey=null;step(7);pn("");lg("승인 확인(시연) v"+v+" · 운영 저장 없음");
 say({id:nid(),who:"ai",t:"12:39",html:'<span class="badge ok">시연 승인 확인</span> MR-01 v'+v+' 의 시연 승인입니다. <b>운영 승인·DB 저장·작업 실행은 일어나지 않았습니다.</b> 이전 버전의 승인·요청 이력은 승계하지 않습니다.'});render()}
document.addEventListener("click",e=>{const a=e.target.closest("[data-act]");if(!a)return;if(a.dataset.act==="open")openArt(+a.dataset.v);if(a.dataset.act==="cmp"){S.tab="compare";S.pOpen=true;step(6);render()}});
document.querySelectorAll(".tabs button").forEach(b=>b.onclick=()=>{S.tab=b.dataset.tab;if(S.tab==="compare"&&S.latest>1)step(6);render()});
$("ver").onchange=e=>{S.view=+e.target.value;render()};
$("send").onclick=()=>send(false);$("apprBtn").onclick=approve;$("back").onclick=()=>{S.pOpen=false;render()};
$("fillReq").onclick=()=>{$("draft").value="승인 버튼을 아래로 옮겨줘";$("draft").focus()};
$("reqBtn").onclick=()=>{S.pOpen=false;render();$("draft").focus()};
$("eDup").onclick=()=>{if(!S.lastText){$("notice").textContent="먼저 수정 요청을 한 번 보내 주세요.";return}send(true)};
$("eMore").onclick=()=>{$("draft").value="버튼 문구도 '승인 확인'으로 바꿔줘";lg("생성 중에 이 문구를 보내면 CR 이 대기열로 들어갑니다")};
$("eLate").onclick=()=>{lg("늦게 온 결과: generation=1(base v1) 결과 도착, head.generation="+(S.latest)+" → 최신 포인터 덮어쓰기 거절, 보관만");say({id:nid(),who:"ai",t:"12:40",html:'구버전 생성 결과가 늦게 도착했지만 최신(v'+S.latest+')을 덮어쓰지 않았습니다. 보관만 했습니다.'});render()};
$("eReconn").onclick=()=>{$("off").classList.add("on");lg("연결 끊김");setTimeout(()=>{$("off").classList.remove("on");lg("재접속: head 재조회 → latest v"+S.latest+" · 대상 선택·미전송 입력 복원");render()},900)};
render();
</script></body></html>
"""


def js_string(text: str) -> str:
    out = []
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "<":
            out.append("\\u003c")
        elif ch == ">":
            out.append("\\u003e")
        elif ch == "&":
            out.append("\\u0026")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def build_mockup() -> str:
    return (
        MOCKUP_TEMPLATE.replace("@@PV1JS@@", js_string(PREVIEW_V1))
        .replace("@@PV2JS@@", js_string(PREVIEW_V2))
        .replace("@@H1@@", H1)
        .replace("@@H2@@", H2)
    )


def main() -> None:
    (ROOT / "preview-v1.html").write_text(PREVIEW_V1, encoding="utf-8")
    (ROOT / "preview-v2.html").write_text(PREVIEW_V2, encoding="utf-8")
    (ROOT / "mockup-v2.html").write_text(build_mockup(), encoding="utf-8")
    artifact = ROOT / "artifact-v2.src.html"
    if artifact.exists():
        text = artifact.read_text(encoding="utf-8").replace("@@H1@@", H1[:12]).replace("@@H2@@", H2[:12])
        (ROOT / "artifact-v2.html").write_text(text, encoding="utf-8")
    print("preview-v1", H1)
    print("preview-v2", H2)


if __name__ == "__main__":
    main()
