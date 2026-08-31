# AADS Server Agent Rules

Read and obey `/root/aads/AGENTS.md` before changing or deploying this repository.

For every API release, `deploy.sh bluegreen` is the default and must enforce: one image build per release SHA, `--no-build` slot starts, candidate health before the nginx lock, DB-fenced execution ownership, same-digest standby synchronization, rollback on routed-health failure, and five-minute P0/P1 monitoring before completion is reported.

Never overwrite unrelated dirty files, restart the active API directly, deploy the full compose stack for an app change, or use `git commit --no-verify`.
