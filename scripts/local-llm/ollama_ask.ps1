# CEO PC 로컬 LLM(Ollama) 호출 러너.
#
# 왜 이 스크립트가 필요한가 (2026-09-21 실측):
#
#  1. **done:false 를 반드시 본다.** Ollama 는 생성이 취소되면 HTTP 200 에
#     지금까지 만든 텍스트를 담아 `"done":false` 로 닫는다. 이때 done_reason·
#     eval_count·total_duration 같은 metrics 필드가 통째로 없다. 검사하지 않으면
#     "답변이 왔다"고 착각한다 — 실제로는 문장 중간에서 끊긴 사고 과정뿐이다.
#     서버 로그 근거: `srv stop: cancel task, id_task = 3024` →
#     `slot release: ... stop processing: n_tokens = 3382, truncated = 0`.
#     truncated=0 이므로 컨텍스트 절단이 아니고, num_predict 도 안 채웠다.
#     같은 로그에 qwen35 계열 SWA/하이브리드 메모리 캐시 체크포인트 무효화
#     경고가 반복된다(llama.cpp PR#13194) — 취소 트리거로 의심되나 미확정.
#     **취소는 재시도로 해결되지 않는다.** 같은 입력이면 같은 지점에서 끊긴다
#     (temperature 0 기준 바이트 단위까지 동일함을 실측).
#
#  2. 사고형 모델은 출력 토큰의 85% 이상을 <think> 에 쓴다. 예산이 모자라면
#     생각만 하다 잘려 최종 답변이 0자가 된다. 실측: num_predict=1600 →
#     done_reason=length, 최종답변 0자 / 3500 → done_reason=stop, 정상.
#     이쪽(length)은 재시도로 해결되므로 1번과 구분해서 다룬다.
#
#  3. Get-Content -Raw 는 순수 문자열이 아니라 PSObject 를 돌려준다. 그대로
#     ConvertTo-Json 에 넣으면 prompt 가 {"value":...,"ReadCount":1} 객체로
#     직렬화되어 본문이 3.85MB 로 부풀고 Ollama 가 거부한다
#     ("cannot unmarshal object into string"). 반드시
#     [string][IO.File]::ReadAllText(path, UTF8) 로 읽는다.
#
# 사용:
#   ollama_ask.ps1 -PromptFile C:\Temp\p.txt -OutFile C:\Temp\r.json [-NumCtx 8192] [-NumPredict 3500]
#
# 출력 JSON 판정 순서: ok=false 면 실패. ok=true 여도 concluded=false 면
# 최종 답변이 없다(사고만 있음). final 필드는 concluded=true 일 때만 쓴다.

param(
  [Parameter(Mandatory=$true)][string]$PromptFile,
  [Parameter(Mandatory=$true)][string]$OutFile,
  [int]$NumCtx=8192,
  [int]$NumPredict=3500,
  [double]$Temp=0,
  [string]$Model='hf.co/ISTA-DASLab/Qwen3.8-27B-GSQ-RCO-GGUF:IQ2_XS'
)
$ErrorActionPreference='Stop'
Remove-Item $OutFile -ErrorAction SilentlyContinue
$tmp=[IO.Path]::GetTempFileName()

function Ask([int]$np){
  # PSObject 가 아니라 순수 문자열로 읽는다 — 위 주석 3번 사유.
  $p=[string][IO.File]::ReadAllText($PromptFile,[Text.Encoding]::UTF8)
  $b=@{model=$Model;prompt=$p;stream=$false;options=@{num_ctx=$NumCtx;temperature=$Temp;num_predict=$np}} | ConvertTo-Json -Depth 5
  [IO.File]::WriteAllText($tmp,$b,(New-Object Text.UTF8Encoding($false)))
  $t0=Get-Date
  $r=Invoke-RestMethod -Uri 'http://localhost:11434/api/generate' -Method Post -InFile $tmp -ContentType 'application/json' -TimeoutSec 1800
  $tps=0; if($r.eval_duration -gt 0){$tps=[math]::Round($r.eval_count/($r.eval_duration/1e9),2)}
  return [pscustomobject]@{
    elapsed=[math]::Round(((Get-Date)-$t0).TotalSeconds,1)
    load=[math]::Round($r.load_duration/1e9,1)
    ptok=$r.prompt_eval_count; etok=$r.eval_count; tps=$tps
    completed=[bool]$r.done          # ★ 취소 판정의 핵심
    done=$r.done_reason; text=$r.response
  }
}

try{
  $a=Ask $NumPredict
  $retried=$false
  # 잘렸는데 </think> 도 못 닫았으면 = 예산 부족. 2배로 올려 1회만 재시도.
  # 취소(completed=false)는 재시도 대상이 아니다 — 같은 지점에서 또 끊긴다.
  if($a.completed -and $a.done -eq 'length' -and -not $a.text.Contains('</think>')){
    $retried=$true
    $a=Ask ([Math]::Min($NumPredict*2, $NumCtx-$a.ptok-256))
  }
  $final=$a.text; $reasoning=''
  $i=$a.text.LastIndexOf('</think>')
  if($i -ge 0){ $reasoning=$a.text.Substring(0,$i); $final=$a.text.Substring($i+8).Trim() }
  [pscustomobject]@{
    ok=$a.completed                  # ★ done:false 면 실패로 본다
    failure=$(if($a.completed){''}else{'generation_cancelled_by_server'})
    retried=$retried
    elapsed_sec=$a.elapsed; load_sec=$a.load
    prompt_tokens=$a.ptok; eval_tokens=$a.etok; tok_per_sec=$a.tps; done_reason=$a.done
    reasoning_chars=$reasoning.Length; final_chars=$final.Length
    concluded=($i -ge 0)             # </think> 를 닫았는가 = 최종 답변이 있는가
    final=$final
  } | ConvertTo-Json -Depth 4 | Out-File $OutFile -Encoding utf8
}catch{
  [pscustomobject]@{ok=$false;failure='http_error';error=$_.Exception.Message} | ConvertTo-Json | Out-File $OutFile -Encoding utf8
}finally{ Remove-Item $tmp -ErrorAction SilentlyContinue }
