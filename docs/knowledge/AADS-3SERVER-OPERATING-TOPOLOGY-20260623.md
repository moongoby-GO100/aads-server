# AADS 3-Server Operating Topology

Last verified: 2026-06-24 09:24 KST

## Canonical servers

| Service | Server | Provider | Primary path | Runner filter |
| --- | --- | --- | --- | --- |
| AADS | `server-116` / `5.104.86.116` | Contabo | `/root/aads/aads-server` | `AADS` |
| GO100 | `server-14` / `5.104.86.14` | Contabo | `/root/kis-autotrade-v4` | `GO100` |
| NewTalk V2 / SF / NAS | `server-114` / `114.207.244.86:7916` | Cafe24 | `/srv/newtalk-v2`, `/data/shortflow` | `SF,NTV2,NAS` |

구 서버68(`68.183.183.11`)은 롤백 대비 잔류 서버이며 신규 운영 기준 서버가 아니다.

## SSH mesh

Standard aliases are configured on the three active servers:

| From | To | Alias | Verification |
| --- | --- | --- | --- |
| `server-116` | `server-14` | `server-14` | `hostname -I` OK |
| `server-116` | `server-114` | `server-114` | `hostname -I` OK |
| `server-14` | `server-116` | `server-116` | `hostname -I` OK |
| `server-14` | `server-114` | `server-114` | `hostname -I` OK |
| `server-114` | `server-116` | `server-116` | `hostname -I` OK |
| `server-114` | `server-14` | `server-14` | `hostname -I` OK |

Implementation notes:

- `server-116` authorized `server-14`'s migration public key on 2026-06-23.
- `server-14` and `server-114` have an `AADS 3-server mesh` block in `/root/.ssh/config`.
- No private SSH key was copied during this change.

## CLI access

| Server | Claude CLI | Codex CLI | Notes |
| --- | --- | --- | --- |
| `server-116` | `2.1.183`, prompt smoke OK | `0.142.0`, prompt smoke OK | `/usr/local/bin/claude` wrapper injects `CLAUDE_CODE_OAUTH_TOKEN` from `/root/.claude/current.env`. |
| `server-14` | `2.1.183`, prompt smoke OK | `0.141.0`, prompt smoke OK | Codex default model corrected from `gpt-5.4` to `gpt-5.5`. |
| `server-114` | `2.1.183`, prompt smoke OK | `0.136.0`, prompt smoke OK | `/usr/local/bin/claude` wrapper injects `CLAUDE_CODE_OAUTH_TOKEN` from `/root/.claude/current.env`. |

Claude Code must use Claude CLI OAuth only. Do not route Claude CLI through LiteLLM or direct `ANTHROPIC_API_KEY`.

2026-06-24 correction:

- `server-116`, `server-14`, and `server-114` had stale `~/.claude/settings.json` entries pointing `ANTHROPIC_BASE_URL` to the old server68 LiteLLM endpoint.
- `~/.claude/current.env` also exported `ANTHROPIC_API_KEY="$ANTHROPIC_AUTH_TOKEN"`, which sends the OAuth token through the API-key header path.
- These entries were removed. `/usr/local/bin/claude` now sources `/root/.claude/current.env`, exports `CLAUDE_CODE_OAUTH_TOKEN`, unsets `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, and then execs `/usr/bin/claude`.
- Verification prompts returned `AADS_CLAUDE_OK`, `GO100_CLAUDE_OK`, and `NTV2_CLAUDE_OK`.

Rollback for the wrapper:

```bash
rm -f /usr/local/bin/claude
hash -r
```

## Runner topology

| Server | Services | Status at verification | DB path |
| --- | --- | --- | --- |
| `server-116` | `aads-pipeline-runner.service` | active, enabled | local Docker PostgreSQL on `localhost:5433` |
| `server-14` | `aads-db-tunnel.service`, `aads-pipeline-runner.service` | active, enabled | SSH tunnel `127.0.0.1:15433 -> server-116:127.0.0.1:5433` |
| `server-114` | `aads-db-tunnel.service`, `aads-pipeline-runner.service` | active, enabled | SSH tunnel `127.0.0.1:15433 -> server-116:127.0.0.1:5433` |

Important details:

- `server-14` did not have `/root/scripts/pipeline-runner.sh` or `aads-pipeline-runner.service`; both were installed from the server116 runner script.
- `server-114` runner script was refreshed from server116 and its old `68.183.183.11` API reference was replaced.
- Direct public PostgreSQL access to `5.104.86.116:5433` was not opened. Remote runners use SSH tunnels instead.
- `AADS_API_URL` for remote runners is `https://aads.newtalk.kr`, because `5.104.86.116:8100/8102` is not externally reachable from 14/114.

Rollback for runners:

```bash
systemctl stop aads-pipeline-runner
systemctl stop aads-db-tunnel
```

Then restore the latest `.bak_*` files under:

- `/root/.config/aads-runner.env.bak_*`
- `/etc/systemd/system/aads-pipeline-runner.service.bak_*`
- `/etc/systemd/system/aads-db-tunnel.service.bak_*`

## Verification commands

```bash
ssh server-14 'ssh server-116 hostname -I'
ssh server-14 'ssh server-114 hostname -I'
ssh server-114 'ssh server-14 hostname -I'
ssh server-114 'ssh server-116 hostname -I'

ssh server-116 'claude -p "Reply with AADS_CLAUDE_OK only."'
ssh server-14 'claude -p "Reply with GO100_CLAUDE_OK only."'
ssh server-114 'claude -p "Reply with NTV2_CLAUDE_OK only."'

ssh server-116 'codex exec --skip-git-repo-check "Reply with AADS_CODEX_OK only."'
ssh server-14 'codex exec --skip-git-repo-check "Reply with GO100_CODEX_OK only."'
ssh server-114 'codex exec --skip-git-repo-check "Reply with NTV2_CODEX_OK only."'

ssh server-14 'systemctl is-active aads-db-tunnel aads-pipeline-runner'
ssh server-114 'systemctl is-active aads-db-tunnel aads-pipeline-runner'
ssh server-116 'systemctl is-active aads-pipeline-runner'
```

## Current operational note

When `server-14` runner was enabled, it immediately claimed pending GO100 job `runner-25df30f7` for `GO100-DATA-FRESHNESS-P0-20260623`. This confirms the runner is connected to the central AADS DB and able to claim work.

2026-06-24 recheck:

- Six-way SSH mesh succeeded: `116→14`, `116→114`, `14→116`, `14→114`, `114→116`, `114→14`.
- Cross-server SSH is verified through the standard aliases (`server-116`, `server-14`, `server-114`). Direct raw-IP SSH from remote hosts can fail if the host-specific `IdentityFile` is not selected.
- Runner services are active/enabled on `server-116`; `aads-db-tunnel` and `aads-pipeline-runner` are active on `server-14` and `server-114`.
- Codex smoke returned `AADS_CODEX_OK`, `GO100_CODEX_OK`, and `NTV2_CODEX_OK`. `server-114` emitted a bubblewrap prerequisite warning but completed successfully using the bundled bubblewrap.
- Claude smoke returned `AADS_CLAUDE_OK`, `GO100_CLAUDE_OK`, and `NTV2_CLAUDE_OK`.
- Remote DB tunnel TCP checks returned `GO100_DB_TUNNEL_OK` and `NTV2_DB_TUNNEL_OK`.
- Public AADS health returned HTTP 200 with `status=ok` and `graph_ready=true`.
- `health_check` code labeling was corrected from legacy `server_68` to `server_116`; long-running MCP tool processes may show the cached old label until restarted.
- Runner end-to-end smoke jobs completed with no file changes:
  - AADS on `server-116`: `runner-5d7d1c24`, `done/done`.
  - GO100 on `server-14`: `runner-71b967fc`, `done/done`.
  - NTV2 on `server-114`: `runner-1d2c039d`, `done/done`.
- During the AADS smoke, the runner temporarily stashed the dirty main worktree as `pipeline-runner-auto-stash-runner-5d7d1c24`; the tracked worktree changes were restored after the smoke verification.
