#!/usr/bin/env python3
"""스마트브라우저 계약 검증 — smart-browser-work-engine-validation. Requires jsonschema."""
import json,hashlib,pathlib,copy,subprocess
from jsonschema import Draft202012Validator,FormatChecker
P=pathlib.Path(__file__).resolve().parent;R=P.parents[2]
b=P/'fixtures/rev1-source.json';docs=json.loads(b.read_bytes())['documents'];log=[]
def ok(name):log.append({'check':name,'status':'pass'});print('PASS',name)
assert hashlib.sha256(b.read_bytes()).hexdigest()=='be353cb5a1a1496d4c2122a50a8aa55e16793f4904cc47712fbfbd67156c970b';ok('bundle SHA256')
for d,folder,suffix in zip(docs,['plans','prd','design'],['기획서','PRD','설계서']):
 p=R/f'docs/{folder}/20261102_AADS_스마트브라우저_공통업무실행엔진_{suffix}.md';raw=p.read_bytes();assert raw==d['content'].encode();assert hashlib.sha256(raw).hexdigest()==d['content_hash'];ok(d['document_key']+' byte/hash equality')
for d in docs:assert hashlib.sha256(d['content'].encode()).hexdigest()==d['content_hash']
s=json.loads((P/'contract-v1.json').read_bytes());Draft202012Validator.check_schema(s);ok('JSON parse and draft2020-12 meta-schema')
refs=[]
def walk(n):
 if isinstance(n,dict):
  if '$ref' in n:
   r=n['$ref'];assert r.startswith('#/');v=s
   for k in r[2:].split('/'):v=v[k]
   refs.append(r)
  for x in n.values():walk(x)
 elif isinstance(n,list):
  for x in n:walk(x)
walk(s);ok(f'all {len(refs)} local refs resolved')
u='11111111-1111-4111-8111-111111111111';h='a'*64
request={'contract_version':1,'recipe_ref':{'id':u,'version':'1.0.0','hash':h},'inputs':{'period':'2026-10'},'location':{'lane':'server','device_id':None},'limits':{'max_pages':20}}
ticket={'contract_version':1,'execution_id':u,'request_id':u,'status':'queued','merged':False,'next_event_cursor':0,'trace_id':u}
err={'error':{'code':'needs_reconciliation','message':'이전 동작 결과 확인','retryable':False,'retry_after':None,'next_action':'inspect_result'},'execution_id':u}
cp={'execution_id':u,'seq':1,'recipe_hash':h,'verified_step':'step1','safe_values':{'order':'safe'},'artifact_refs':['artifact:1'],'context_fingerprint':h,'postconditions':['ready'],'evidence_ref':'artifact:1','created_at':'2026-11-02T03:00:00Z'}
obs={'contract_version':1,'origin':'https://example.com','url':'https://example.com/orders','document_revision':'r1','tab_ref':'t1','frame_ref':None,'dom_summary':'표','ax_summary':'표','screenshot_ref':None,'visible_controls':[{'target_ref':'button1','role':'button'}],'page_state':'ready','timestamp':'2026-11-02T03:00:00Z','redaction_manifest':[]}
action={'action':'click','target_ref':'button1','evidence_ref':'artifact:1','postcondition':'ready','risk':'READ','confidence':0.9}
plan={'contract_version':1,'document_revision':'r1','action':action}
def validator(n):return Draft202012Validator({'$schema':s['$schema'],'$defs':s['$defs'],'$ref':'#/$defs/'+n},format_checker=FormatChecker())
def check(n,v,valid,label):
 errors=list(validator(n).iter_errors(v));assert bool(errors)!=valid,(n,label,[x.message for x in errors]);ok(n+' '+label)
examples={'Request':request,'Ticket':ticket,'Error':err,'Checkpoint':cp,'Observation':obs,'ActionPlan':plan}
for name,v in examples.items():
 check(name,v,True,'valid')
 bad=copy.deepcopy(v);bad['unexpected']=True;check(name,bad,False,'reject extra field')
 for required in s['$defs'][name]['required']:
  bad=copy.deepcopy(v);del bad[required];check(name,bad,False,'reject missing '+required)
for name in ['Request','Ticket','Observation','ActionPlan']:
 for ver in [0,2,'1',True]:
  bad=copy.deepcopy(examples[name]);bad['contract_version']=ver;check(name,bad,False,'reject version '+repr(ver))
def request_case(label,valid,updates,remove=()):
 candidate=copy.deepcopy(request)
 for key in remove:del candidate[key]
 candidate.update(updates)
 check('Request',candidate,valid,label)
request_case('reject both selectors',False,{'task_intent':'collect'})
request_case('reject no selector',False,{},('recipe_ref',))
request_case('valid intent',True,{'task_intent':'collect'},('recipe_ref',))
for change in [{'tenant_id':u},{'session_id':u},{'project_key':'other'},{'idempotency_key':'x'},{'credential_refs':['plaintext-secret']}]:
 bad=copy.deepcopy(request);bad.update(change);check('Request',bad,False,'reject '+next(iter(change)))
for value in [{'id':'bad','version':'1.0.0','hash':h},{'id':u,'version':'1','hash':h},{'id':u,'version':'1.0.0','hash':'sha256'}]:
 bad=copy.deepcopy(request);bad['recipe_ref']=value;check('Request',bad,False,'reject recipe ref '+str(value))
for limits in [{'max_pages':101},{'max_rows':0},{'max_llm_calls':9},{'other_limit':1}]:
 bad=copy.deepcopy(request);bad['limits']=limits;check('Request',bad,False,'reject limits '+str(limits))
for loc in [{'lane':'pc','device_id':None},{'lane':'server','device_id':'other'},{'lane':'mobile','device_id':None}]:
 bad=copy.deepcopy(request);bad['location']=loc;check('Request',bad,False,'reject location '+str(loc))
request_case('valid PC binding',True,{'location':{'lane':'pc','device_id':'device1'}})
for status in s['$defs']['Ticket']['properties']['status']['enum']:
 bad=copy.deepcopy(ticket);bad['status']=status;check('Ticket',bad,True,'status '+status)
for status in ['done','success','unknown']:
 bad=copy.deepcopy(ticket);bad['status']=status;check('Ticket',bad,False,'reject status '+status)
bad=copy.deepcopy(plan);bad['plan']=[action];check('ActionPlan',bad,False,'reject both branches');del bad['action'];check('ActionPlan',bad,True,'bounded plan');bad['plan']=[];check('ActionPlan',bad,False,'reject empty plan');bad['plan']=[action]*51;check('ActionPlan',bad,False,'reject 51 actions')
for k,v in [('action','arbitrary_exec'),('risk','SAFE'),('confidence',1.1)]:
 bad=copy.deepcopy(plan);bad['action'][k]=v;check('ActionPlan',bad,False,'reject '+k)
for val,valid in [(0,False),(1000,True),(1001,False)]:check('ChangesQuery',{'limit':val},valid,'limit '+str(val))
for val in ['money','number','url']:check('LegacyFieldType',val,True,'legacy '+val)
def coverage(t):
 expected=json.loads((P/'fixtures/assignments.json').read_text())
 assert set(t['slices'])==set(expected)
 snapshot=json.loads((P/'fixtures/review-jobs.json').read_text())
 known={j['job_id'] for j in snapshot['jobs']+snapshot['historical_jobs']}
 for key,job in expected.items():
  assert isinstance(job,str) and job in known
  assert t['slices'][key]['job_id']==job
 ids=[x['id'] for x in t['requirements']]
 assert len(ids)==len(set(ids))==38
 assert set(ids)=={f'{p}-{n:02}' for p,m in [('FR',18),('AC',20)] for n in range(1,m+1)}
 for row in t['requirements']:
  assert row['slice'] in expected
  assert row['job_id'] in known and row['job_id']==expected[row['slice']]
  assert row['acceptance_job_id']==expected[row['acceptance_slice']]
  assert row['status']=='not_run' and row['exit_code'] is None and row['tested_sha'] is None
 for name in ('FR-18','AC-20'):
  row=next(x for x in t['requirements'] if x['id']==name)
  assert row['slice']=='E0' and row['job_id']=='runner-801f00fd'
 assert {x['id'] for x in t['pm_tests']}=={f'PM-C{c}-T{n}' for c in (1,2,3) for n in (1,2)}
 assert len(t['pm_tests'])==6 and all(x['status']=='not_run' for x in t['pm_tests'])
 assert t['result_sha'] is None and t['slices']['D0']['actual_sha'] is None

t=json.loads((P/'traceability.json').read_bytes());coverage(t)
ok('FR18 AC20 exact coverage')
ok('eight slices real jobs planned vs measured and PM six not_run')
# Mutate each assignment independently, including the original null==null false pass.
def rejects(bad,label):
 try:coverage(bad)
 except (AssertionError,KeyError,TypeError,StopIteration):ok('negative coverage '+label)
 else:raise AssertionError('false pass: '+label)
for key in t['slices']:
 for value in (None,'','runner-deadbeef'):
  bad=copy.deepcopy(t);bad['slices'][key]['job_id']=value
  rejects(bad,f'slice {key} {value!r}')
for i,row in enumerate(t['requirements']):
 for value in (None,'','runner-deadbeef'):
  bad=copy.deepcopy(t);bad['requirements'][i]['job_id']=value
  rejects(bad,f"{row['id']} {value!r}")
def reject_deleted_e0(source):
 candidate=copy.deepcopy(source)
 del candidate['slices']['E0']['job_id']
 rejects(candidate,'deleted E0 ID')
reject_deleted_e0(t)
bad=copy.deepcopy(t);bad['slices']['E0']['job_id']=None
for row in bad['requirements']:
 if row['slice']=='E0':row['job_id']=None
rejects(bad,'raw null equality')
for name in ('FR-18','AC-20'):
 bad=copy.deepcopy(t);bad['requirements']=[x for x in bad['requirements'] if x['id']!=name];rejects(bad,'removed '+name)
 bad=copy.deepcopy(t);next(x for x in bad['requirements'] if x['id']==name)['job_id']=t['slices']['B1']['job_id'];rejects(bad,'mismatched '+name)
proc=subprocess.run(['git','diff','--check'],cwd=R,text=True,capture_output=True);assert proc.returncode==0;ok('git diff --check exit0 (tracked only)')
files=[*R.glob('docs/plans/*스마트브라우저*'),*R.glob('docs/prd/*스마트브라우저*'),*R.glob('docs/design/*스마트브라우저*'),*P.rglob('*')]
for p in files:
 if not p.is_file():continue
 for n,line in enumerate(p.read_text().splitlines(),1):assert line.rstrip()==line,(p,n)
ok('all document and fixture whitespace scan')
# Verify registration content and all companion bindings, without DB writes.
registration=json.loads((P/'정본등록요청-smart-browser-work-engine-contract.json').read_text())
contract=next(P.glob('*계약명세.md')).read_bytes()
assert registration['arguments']['content'].encode()==contract
assert registration['content_sha256']==hashlib.sha256(contract).hexdigest()
assert registration['arguments']['source_task_id']=='runner-2c331979'
assert registration['arguments']['kind']=='spec'
assert len(registration['arguments']['change_summary'])>=10
import re
for name,digest in re.findall(r'^- (.+) SHA256: ([a-f0-9]{64})$',contract.decode(),re.M):
 assert hashlib.sha256((P/name).read_bytes()).hexdigest()==digest,name
print('TOTAL',len(log),'checks; document/schema only; acceptance not_run')
