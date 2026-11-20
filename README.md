# backup – Ansible module for full and incremental backups

Creates tar archives of a directory, either **full** or **incremental**
(GNU tar snapshot based, deletions are tracked). Optional compression
auto-selection, checksums, retention and an opt-in status ping.

## Requirements

- Ansible and Python on the managed host

## Install

```
playbook.yml
library/
  backup.py
```

## Usage

```yaml
- name: Weekly full
  backup:
    source: /var/www
    destination: /backup/www
    name: www
    mode: full
    exclude: ["*.log", "cache/*"]
    keep_fulls: 4

- name: Daily incremental
  backup:
    source: /var/www
    destination: /backup/www
    name: www
    mode: incremental
    include: ["html", "uploads"]
```

See `backup-playbook.yml` for a full/incremental weekly schedule.

## Parameters

| Parameter | Default | Description |
|---|---|---|
| `source` | required | Directory to back up |
| `destination` | required | Where archives and the snapshot are stored |
| `name` | `backup` | Backup set name (file names, snapshot) |
| `mode` | `full` | `full` or `incremental` (falls back to full if no snapshot yet) |
| `include` | all | Paths/globs relative to `source` |
| `exclude` | none | Patterns passed to `tar --exclude` |
| `compression` | `auto` | `auto`, `gzip`/`gz`, `bzip2`/`bz2`, `xz`, `none` |
| `auto_strategy` | `ratio` | `ratio` (sample both), `type` (by file extension), `size` |
| `auto_threshold_mb` | `1024` | Above this size `auto` never picks bzip2 |
| `sample_mb` | `32` | Sample size for `ratio` |
| `ratio_gain_pct` | `10` | bzip2 must beat gzip by this much to be chosen |
| `compressed_fraction_pct` | `50` | For `type`: share of already-compressed bytes that triggers gzip |
| `checksum` | `true` | Write `<archive>.sha256` |
| `keep_fulls` | `0` | Keep newest N full chains (full + its incrementals); `0` = no cleanup |

`auto` always uses gzip for incrementals. Check mode is supported.

## Output

Archives are named `<name>-<YYYYMMDDTHHMMSS>-full|incr.tar.<ext>`; the
snapshot is `<name>.snar`. The task returns `archive`, `mode`, `compression`,
`compression_reason`, `size`, `checksum`, `removed` and `report`.

## Restore

Extract the full archive first, then each incremental in order. Use the
incremental flag so recorded deletions are applied:

```
tar -x --listed-incremental=/dev/null -f <archive>
```
