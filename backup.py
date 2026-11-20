#!/usr/bin/python

DOCUMENTATION = r'''
---
module: backup
short_description: Create full or incremental tar backups
description:
  - Backs up a directory to a tar archive using GNU tar.
  - Incremental mode uses a GNU tar snapshot file, so only new/changed files
    are stored and deletions are tracked.
options:
  source:
    description: Directory to back up.
    type: path
    required: true
  destination:
    description: Directory where archives and the snapshot file are stored.
    type: path
    required: true
  name:
    description: Backup set name (used in file names and for the snapshot file).
    type: str
    default: backup
  mode:
    description:
      - C(full) always makes a new full backup and resets the incremental chain.
      - C(incremental) stores changes since the last backup in the chain;
        falls back to full if no snapshot exists yet.
    type: str
    choices: [full, incremental]
    default: full
  include:
    description: Paths/globs relative to I(source). If empty, everything is included.
    type: list
    elements: str
    default: []
  exclude:
    description: Glob patterns to exclude (passed to tar C(--exclude)).
    type: list
    elements: str
    default: []
  compression:
    description:
      - Archive compression. C(gzip)/C(gz) is fast, C(bzip2)/C(bz2) is smaller but slower.
      - C(auto) decides per run. Incrementals always use gzip (speed). Fulls are
        decided by I(auto_strategy). bzip2 is never chosen when the source is larger
        than I(auto_threshold_mb).
    type: str
    choices: [auto, gz, gzip, bz2, bzip2, xz, none]
    default: auto
  auto_strategy:
    description:
      - C(size) picks bzip2 for any source under I(auto_threshold_mb).
      - C(type) looks at file extensions. If at least I(compressed_fraction_pct) percent
        of the bytes are already-compressed formats (jpg, mp4, zip, ...), gzip is used
        because bzip2 would only cost CPU. Otherwise bzip2.
      - C(ratio) compresses a sample of the data with both gzip and bzip2 in memory and
        picks bzip2 only if it is at least I(ratio_gain_pct) percent smaller.
    type: str
    choices: [size, type, ratio]
    default: ratio
  auto_threshold_mb:
    description: Source size limit (MB) above which C(auto) never picks bzip2.
    type: int
    default: 1024
  sample_mb:
    description: Amount of data sampled for C(ratio) (first 1 MB of each file, up to this total).
    type: int
    default: 32
  ratio_gain_pct:
    description: Minimum extra size reduction bzip2 must give over gzip, in percent of the gzip size.
    type: int
    default: 10
  compressed_fraction_pct:
    description: For C(type), share of already-compressed bytes at which gzip is chosen.
    type: int
    default: 50
  checksum:
    description: Write a C(.sha256) file next to each archive.
    type: bool
    default: true
  notify_url:
    description:
      - If set, the module sends a single HTTP GET to this URL after a successful run
        (never in check mode). Nothing is sent when unset.
      - A delivery problem only produces a warning, it never fails the backup.
    type: str
  keep_fulls:
    description:
      - Retention. Keep the newest N full backups together with their incrementals;
        older chains are deleted. C(0) disables cleanup.
    type: int
    default: 0
author:
  - Cornel-Mihai Badea
  - cornel@companyx.ro
'''

EXAMPLES = r'''
- name: Weekly full
  backup:
    source: /var/www
    destination: /backup/www
    name: www
    mode: full
    exclude: ["*.log", "cache/*"]

- name: Daily incremental
  backup:
    source: /var/www
    destination: /backup/www
    name: www
    mode: incremental
    include: ["html", "uploads/*.jpg"]
'''

RETURN = r'''
archive:
  description: Path of the archive created.
  type: str
  returned: always
mode:
  description: Mode actually used (incremental may fall back to full).
  type: str
  returned: always
snapshot:
  description: Path to the tar snapshot file.
  type: str
  returned: always
compression:
  description: Compression actually used (relevant for C(auto)).
  type: str
  returned: always
compression_reason:
  description: Why that compression was chosen (includes measured numbers for C(auto)).
  type: str
  returned: always
size:
  description: Archive size in bytes.
  type: int
  returned: when not check_mode
checksum:
  description: SHA-256 of the archive.
  type: str
  returned: when checksum is true and not check_mode
removed:
  description: Archives deleted by retention.
  type: list
  returned: when keep_fulls > 0
'''

import bz2
import fnmatch
import glob
import hashlib
import os
import re
import json
import shutil
import time
import zlib

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.urls import fetch_url

EXT = {'gz': ('tar.gz', '-z'), 'bz2': ('tar.bz2', '-j'),
       'xz': ('tar.xz', '-J'), 'none': ('tar', None)}
ALIASES = {'gzip': 'gz', 'bzip2': 'bz2'}


COMPRESSED_EXT = frozenset((
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.avif',
    '.mp3', '.aac', '.ogg', '.opus', '.flac', '.m4a',
    '.mp4', '.mkv', '.avi', '.mov', '.webm',
    '.zip', '.gz', '.tgz', '.bz2', '.xz', '.zst', '.7z', '.rar', '.lz4',
    '.jar', '.war', '.apk', '.deb', '.rpm', '.whl',
    '.pdf', '.docx', '.xlsx', '.pptx', '.odt', '.ods', '.epub',
))


def iter_files(source, members, excludes):
    """Yield (path, size) for regular files, roughly honoring include/exclude."""
    def skipped(rel):
        base = os.path.basename(rel)
        return any(fnmatch.fnmatch(rel, x) or fnmatch.fnmatch(base, x) for x in excludes)

    for member in members:
        top = os.path.normpath(os.path.join(source, member))
        if os.path.isfile(top):
            walk = [(os.path.dirname(top), [], [os.path.basename(top)])]
        else:
            walk = os.walk(top)
        for root, dirs, files in walk:
            dirs[:] = sorted(d for d in dirs
                             if not skipped(os.path.relpath(os.path.join(root, d), source)))
            for f in sorted(files):
                path = os.path.join(root, f)
                if skipped(os.path.relpath(path, source)) or os.path.islink(path):
                    continue
                try:
                    yield path, os.lstat(path).st_size
                except OSError:
                    pass


def sample_ratios(files, sample_bytes):
    """Return (raw, gzip_size, bzip2_size) for the head of many files."""
    raw = gz = bz = 0
    for path, size in files:
        if raw >= sample_bytes:
            break
        if size == 0:
            continue
        try:
            with open(path, 'rb') as fh:
                chunk = fh.read(1024 * 1024)
        except (IOError, OSError):
            continue
        raw += len(chunk)
        gz += len(zlib.compress(chunk, 6))
        bz += len(bz2.compress(chunk, 9))
    return raw, gz, bz


def choose_compression(p, mode, source, members):
    requested = p['compression']
    if requested != 'auto':
        c = ALIASES.get(requested, requested)
        return c, 'requested explicitly'
    if mode == 'incremental':
        return 'gz', 'auto: incremental backups use gzip for speed'

    files = list(iter_files(source, members, p['exclude']))
    total = sum(size for _path, size in files)
    total_mb = total / (1024.0 * 1024.0)
    if total_mb > p['auto_threshold_mb']:
        return 'gz', 'auto: source %.0f MB exceeds auto_threshold_mb, bzip2 too slow' % total_mb

    strategy = p['auto_strategy']
    if strategy == 'size':
        return 'bz2', 'auto/size: source %.0f MB is within threshold' % total_mb

    if strategy == 'type':
        packed = sum(size for path, size in files
                     if os.path.splitext(path)[1].lower() in COMPRESSED_EXT)
        pct = 100.0 * packed / total if total else 0.0
        if pct >= p['compressed_fraction_pct']:
            return 'gz', 'auto/type: %.0f%% of bytes already compressed, bzip2 would waste CPU' % pct
        return 'bz2', 'auto/type: only %.0f%% of bytes already compressed' % pct
    raw, gz, bz = sample_ratios(files, p['sample_mb'] * 1024 * 1024)
    if raw == 0:
        return 'gz', 'auto/ratio: nothing to sample'
    gain = 100.0 * (gz - bz) / gz if gz else 0.0
    detail = 'sample %.1f MB: gzip %.1f%%, bzip2 %.1f%% of original, bzip2 gain %.1f%%' % (
        raw / 1048576.0, 100.0 * gz / raw, 100.0 * bz / raw, gain)
    if gain >= p['ratio_gain_pct']:
        return 'bz2', 'auto/ratio: ' + detail
    return 'gz', 'auto/ratio: bzip2 gain below %d%%; %s' % (p['ratio_gain_pct'], detail)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def apply_retention(dest, name, keep_fulls, dry_run):
    """Delete whole chains (full + following incrementals) beyond keep_fulls."""
    rx = re.compile(r'^%s-(\d{8}T\d{6})-(full|incr)\.tar(\.gz|\.bz2|\.xz)?$' % re.escape(name))
    files = sorted(f for f in os.listdir(dest) if rx.match(f))
    chains = []
    for f in files:
        if rx.match(f).group(2) == 'full':
            chains.append([f])
        elif chains:
            chains[-1].append(f)
    removed = []
    for chain in chains[:-keep_fulls] if len(chains) > keep_fulls else []:
        for f in chain:
            for victim in (f, f + '.sha256'):
                path = os.path.join(dest, victim)
                if os.path.exists(path):
                    removed.append(path)
                    if not dry_run:
                        os.remove(path)
    return removed


def expand_includes(source, patterns):
    """Resolve include globs relative to source; return relative paths."""
    result = []
    for pat in patterns:
        matches = glob.glob(os.path.join(source, pat))
        for m in matches:
            rel = os.path.relpath(m, source)
            if rel not in result:
                result.append(rel)
    return sorted(result)


def http_get(module, url):
    """Send a GET request to url. Warns on failure, never raises."""
    try:
        _resp, info = fetch_url(module, url, method='GET', timeout=10)
    except Exception as exc:
        module.warn('GET %s failed: %s' % (url, exc))
        return
    status = info.get('status', -1)
    if not 200 <= status < 300:
        module.warn('GET %s failed: %s %s' % (url, status, info.get('msg', '')))


def main():
    module = AnsibleModule(
        argument_spec=dict(
            source=dict(type='path', required=True),
            destination=dict(type='path', required=True),
            name=dict(type='str', default='backup'),
            mode=dict(type='str', default='full', choices=['full', 'incremental']),
            include=dict(type='list', elements='str', default=[]),
            exclude=dict(type='list', elements='str', default=[]),
            compression=dict(type='str', default='auto',
                             choices=['auto', 'gz', 'gzip', 'bz2', 'bzip2', 'xz', 'none']),
            auto_strategy=dict(type='str', default='ratio',
                               choices=['size', 'type', 'ratio']),
            auto_threshold_mb=dict(type='int', default=1024),
            sample_mb=dict(type='int', default=32),
            ratio_gain_pct=dict(type='int', default=10),
            compressed_fraction_pct=dict(type='int', default=50),
            checksum=dict(type='bool', default=True),
            keep_fulls=dict(type='int', default=0),
            notify_url=dict(type='str'),
        ),
        supports_check_mode=True,
    )
    p = module.params
    source, dest, name = p['source'], p['destination'], p['name']
    if not os.path.isdir(source):
        module.fail_json(msg="source '%s' is not a directory" % source)
    tar = module.get_bin_path('tar', required=True)
    snapshot = os.path.join(dest, name + '.snar')
    mode = p['mode']
    if mode == 'incremental' and not os.path.exists(snapshot):
        mode = 'full'

    if p['include']:
        members = expand_includes(source, p['include'])
        if not members:
            module.fail_json(msg="include patterns matched nothing in %s" % source)
    else:
        members = ['.']

    comp, reason = choose_compression(p, mode, source, members)
    ext, zflag = EXT[comp]
    stamp = time.strftime('%Y%m%dT%H%M%S')
    archive = os.path.join(dest, '%s-%s-%s.%s' % (
        name, stamp, 'full' if mode == 'full' else 'incr', ext))

    result = dict(changed=True, archive=archive, mode=mode, snapshot=snapshot,
                  compression=comp, compression_reason=reason)

    if module.check_mode:
        if p['keep_fulls'] > 0 and os.path.isdir(dest):
            result['removed'] = apply_retention(dest, name, p['keep_fulls'], True)
        module.exit_json(**result)

    if not os.path.isdir(dest):
        os.makedirs(dest)
    work_snar = snapshot + '.tmp'
    if mode == 'full' and os.path.exists(work_snar):
        os.remove(work_snar)
    if mode == 'incremental':
        shutil.copy2(snapshot, work_snar)

    cmd = [tar, '--create', '--file', archive,
           '--listed-incremental', work_snar,
           '--directory', source]
    if zflag:
        cmd.append(zflag)
    for pat in p['exclude']:
        cmd.append('--exclude=%s' % pat)
    cmd.extend(members)
    rc, out, err = module.run_command(cmd)
    if rc not in (0, 1):
        for f in (archive, work_snar):
            if os.path.exists(f):
                os.remove(f)
        module.fail_json(msg='tar failed', rc=rc, stderr=err, cmd=' '.join(cmd))
    os.rename(work_snar, snapshot)
    result['size'] = os.path.getsize(archive)
    if p['checksum']:
        digest = sha256_of(archive)
        with open(archive + '.sha256', 'w') as fh:
            fh.write('%s  %s\n' % (digest, os.path.basename(archive)))
        result['checksum'] = digest
    if p['keep_fulls'] > 0:
        result['removed'] = apply_retention(dest, name, p['keep_fulls'], False)
    if rc == 1:
        result['warnings'] = ['tar: some files changed while being archived']
    if p['notify_url']:
        http_get(module, p['notify_url'])
    module.exit_json(**result)


if __name__ == '__main__':
    main()
