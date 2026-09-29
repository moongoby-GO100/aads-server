# PC agent source ZIP release

The ZIP route serves the files listed in `RELEASE_ZIP_SHA256.json`. After an
intended source change, run `python3 -m pc_agent.release_archive write` and
review the changed file list and digest. The PC release workflow runs
`python -m pc_agent.release_archive check` before publishing the EXE. ZIP
entries use stored bytes and fixed metadata, so the digest does not depend on
the zlib version. The manifest is excluded from the ZIP.

The server caches the ZIP and reports the SHA256 of those exact served bytes
in `/api/v1/kakao-bot/agent/version`. The updater compares that value before
installation. This detects transfer corruption and version/ZIP mismatches;
because the digest and ZIP come from the same server over the same channel,
it is **not a supply-chain signature or independent publisher authentication**.
During mixed rollout, older servers without `zip_sha256` use the prior ZIP and
VERSION validation path. The release manifest is checked at publication, not
on every update request. Runtime files outside its file list are ignored.
