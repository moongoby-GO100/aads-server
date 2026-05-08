#!/bin/bash
# GPT-5.4-mini translation test via Codex CLI (non-interactive)
export PATH="/root/.nvm/versions/node/v20.20.0/bin:$PATH"
cd /tmp

codex exec -m gpt-5.4-mini \
  --full-auto \
  --skip-git-repo-check \
  --ephemeral \
  --ignore-rules \
  -o /tmp/gpt54mini_result.txt \
  "你是中国服装批发商品翻译专家。将以下中文商品名准确翻译为韩语，适合韩国网上商城使用。只输出翻译结果，每行一个。不要执行任何命令，直接回答。1. 雪纺印花连衣裙夏季新款 2. 高腰阔腿牛仔裤女宽松显瘦 3. 亚麻混纺短款夹克韩版休闲 4. 韩版宽松大码女装短袖T恤 5. 真丝缎面吊带背心性感内搭 6. 档口爆款碎花半身裙中长款" > /tmp/gpt54mini_stdout.txt 2>&1

echo "EXIT_CODE=$?" >> /tmp/gpt54mini_stdout.txt
