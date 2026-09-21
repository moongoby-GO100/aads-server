# CEO PC 로컬 LLM(Ollama) 호출 러너.
#
# 왜 이 스크립트가 필요한가 (2026-09-21 실측):
#  1. Qwen3.8-27B 같은 사고형(reasoning) 모델은 출력 토큰의 85% 이상을 <think> 블록에
#     쓴다. 토큰 예산이 모자라면 생각만 하다 잘려 최종 답변이 0자로 나온다.
#     실측: num_predict=1600 → done=length, 최종답변 0자 / 3500 → done=stop, 정상.
#  2. Get-Content -Raw 는 순수 문자열이 아니라 PSObject 를 돌려준다. 이걸 그대로
#     ConvertTo-Json 에 넣으면 prompt 가 {"value":...,"ReadCount":1} 객체로 직렬화되어
#     본문이 3.85MB 로 부풀고 Ollama 가 거부한다("cannot unmarshal object into string").
#     반드시 [string][IO.File]::ReadAllText(path, UTF8) 로 읽어야 한다.
#
# 사용:
#   ollama_ask.ps1 -PromptFile C:\Temp\p.txt -OutFile C:\Temp\r.json [-NumCtx 8192] [-NumPredict 3500]
#
# 출력 JSON 의 final 필드가 </think> 이후의 최종 답변이다. reasoning 은 길이만 남긴다.

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
  # PSObject 가 아니라 순수 문자열로 읽는다 — 위 주석 2번 사유.
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
    done=$r.done_reason; text=$r.response
  }
}

try{
  $a=Ask $NumPredict
  $retried=$false
  # 잘렸는데 </think> 도 못 닫았으면 = 사고만 하다 끝났다. 예산을 2배로 올려 1회만 재시도.
  if($a.done -eq 'length' -and -not $a.text.Contains('</think>')){
    $retried=$true
    $a=Ask ([Math]::Min($NumPredict*2, $NumCtx-$a.ptok-256))
  }
  $final=$a.text; $reasoning=''
  $i=$a.text.LastIndexOf('</think>')
  if($i -ge 0){ $reasoning=$a.text.Substring(0,$i); $final=$a.text.Substring($i+8).Trim() }
  [pscustomobject]@{
    ok=$true; retried=$retried
    elapsed_sec=$a.elapsed; load_sec=$a.load
    prompt_tokens=$a.ptok; eval_tokens=$a.etok; tok_per_sec=$a.tps; done_reason=$a.done
    reasoning_chars=$reasoning.Length; final_chars=$final.Length
    # final_chars 가 크고 reasoning_chars 가 0 이면 </think> 를 못 닫은 것 = 답변 없음.
    concluded=($i -ge 0)
    final=$final
  } | ConvertTo-Json -Depth 4 | Out-File $OutFile -Encoding utf8
}catch{
  [pscustomobject]@{ok=$false;error=$_.Exception.Message} | ConvertTo-Json | Out-File $OutFile -Encoding utf8
}finally{ Remove-Item $tmp -ErrorAction SilentlyContinue }
