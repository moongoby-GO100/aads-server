# AADS Backup Retention Policy

Updated: 2026-06-08 11:32 KST

## P0 Policy

- Root backup path: `/root/aads/backups`
- External volume backup path: `/mnt/volume_sgp1_01/aads-backups`
- Root retention: 3 days
- External volume retention: latest 2 valid gzip backups
- Long-term retention: server5 or remote object storage, 30 days target

## Runtime Guards

- Backups are written to a temporary `.tmp` gzip file first.
- A backup is promoted only after it is non-empty and `gzip -t` succeeds.
- Zero-byte and corrupted `aads_*.sql.gz` files are removed before and after backup.
- External volume cleanup is count-based, not age-only, because one AADS compressed DB backup is currently several GB and a 50GB external volume cannot hold 5 local copies below the 80% safety target.

## Current Runtime Scripts

- Cron backup script: `/root/aads/scripts/backup.sh`
- Cron disk cleanup script: `/root/aads/scripts/disk_cleanup.sh`
- Versioned cleanup mirror: `scripts/disk_cleanup_v2.sh`

## Completion Criteria

- `/mnt/volume_sgp1_01` stays below 80% after daily cleanup.
- No 0-byte backup remains after `backup.sh` or `disk_cleanup.sh`.
- Latest root backup exists and passes `gzip -t`.
- External backup count does not exceed 2 valid gzip files.
