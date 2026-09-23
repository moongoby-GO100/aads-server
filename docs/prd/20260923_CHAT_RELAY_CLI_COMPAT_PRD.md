# Chat relay CLI/model compatibility — PRD

Status: emergency release target, 2026-09-23. Scope: AADS Claude chat relay and Agent SDK CLI execution. No model remapping, account change, or GO100 trading-policy change.

## Incident and root cause

Chat session `5090a247-47f7-4a05-a965-da89f844ad2f` selected `claude-opus-5-5`. The shared model contract accepts and advertises that exact ID, but both the relay Docker wrapper and Agent SDK wrapper execute the SDK 0.2.152 bundled Claude Code CLI 2.1.259. The CLI returned HTTP 400: version 2.1.280 or newer is required. The host has 2.1.280, but the container uses its separate bundled binary. Account fallback repeated the same version error because both slots use the same container binary. The runner's `AI_REVIEW` model-list correction is a separate setting and does not change chat selection.

The failed attempt then fell back to Sonnet and produced a tool-availability refusal with no successful tool call in that initial turn. Output validation rejected the weak report; error text and interrupted partials remained visible in one message. This is a separate chat presentation/retry defect, not proof that the 400 was a credential failure.

## Required behavior

1. Every blue/green API image embeds the official Claude Code 2.1.280 Linux x64 binary with a fixed SHA256 verified against the official release manifest. No mutable host mount or `latest` artifact is allowed.
2. Relay and SDK wrappers execute the same image-pinned binary. They fail closed if it is absent; neither silently falls back to the 2.1.259 SDK bundle.
3. The image build checks the CLI's reported version. Tests lock the image artifact and both wrapper paths.
4. A read-only chat/relay smoke request for `claude-opus-5-5` must yield a verified primary model or a clearly classified entitlement/capacity error, never the 2.1.259 compatibility error. No live-trading order or parameter mutation is part of this check.
5. Release follows candidate health, active cutover, routed health, same-image standby after stream drain, and five-minute P0/P1 monitoring. A deferred standby is reported as uncertified until synchronized.

## Rollback

If the pinned CLI causes a new protocol or authentication failure, restore routing to the prior healthy image and disable `claude-opus-5-5` selection until a compatible binary is available. Do not relabel `claude-opus-5` as 5.5. Preserve chat message and turn evidence for the separate output/partial-display repair.
