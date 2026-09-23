# Error note — Claude chat model/CLI mismatch, 2026-09-23

Signature: `API Error: 400 Claude Code 2.1.259 does not support this model; version 2.1.280 or newer is required`.

Confirmed cause: `claude-opus-5-5` is accepted by the model contract and persisted as the chat session's selected model, while the relay and Agent SDK wrappers execute the container's SDK-bundled Claude Code 2.1.259. The host's `/usr/bin/claude` is 2.1.280 but was not used. Two account attempts repeated the same deterministic 400; switching credentials cannot fix binary compatibility. The initial fallback Sonnet response incorrectly denied actual chat tool access and used zero tools; the output validator rejected it and the partial plus raw CLI errors were displayed. Later retries can append further text to the same message, so the displayed answer is not a clean, single verified diagnosis.

Fix: embed official 2.1.280 with manifest SHA256 `1e08503dbdf3c2cb0d706d32f3408277388d1c76ef108673e8fe42c1b322925b` in the release image; direct both wrappers to it; enforce build/test gates. This removes the version 400, not the separate chat fallback/presentation defect. Do not treat the chat's GO100 trading claims as independently verified without their tool receipts.
