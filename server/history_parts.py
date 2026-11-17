"""Lossless GitHub report packaging for histories larger than one safe blob.

The VPS keeps its ordinary complete history.json. Only the published snapshot
is split, with hashes and a loader; no observations or PC evidence are removed.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

FORMAT = 'sv-iptv-history-parts-v1'
INLINE_BYTES = 40 * 1024 * 1024
PART_BYTES = 4 * 1024 * 1024


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True) + '\n'


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def package_history(history, inline_bytes=INLINE_BYTES, part_bytes=PART_BYTES):
    complete = encode(history)
    if len(complete.encode('utf-8')) <= inline_bytes:
        return {'history.json': complete}
    index = deepcopy(history)
    original_storage = index.get('history_storage')
    files, specs = {}, []
    fields = [('streams',)]
    fields += [('expansion_campaign', k) for k in
               ('requests', 'cohort_keys', 'previously_working_keys', 'publication_protected_keys')]
    for field in fields:
        parent = index
        for name in field[:-1]:
            parent = parent.get(name, {})
        value = parent.get(field[-1])
        if not isinstance(value, (dict, list)) or not value:
            continue
        kind = 'dict' if isinstance(value, dict) else 'list'
        rows = sorted(value.items()) if kind == 'dict' else list(value)
        chunk, size, number = [], 2, 0

        def flush():
            nonlocal chunk, size, number
            if not chunk:
                return
            body = encode(dict(chunk) if kind == 'dict' else chunk)
            path = f'history-parts/{"-".join(field)}-{number:04d}.json'
            files[path] = body
            specs.append({'path': path, 'sha256': digest(body), 'bytes': len(body.encode('utf-8')),
                          'field': list(field), 'kind': kind, 'entries': len(chunk)})
            chunk, size, number = [], 2, number + 1

        for row in rows:
            row_size = len(encode(row).encode('utf-8')) + 1
            if row_size > part_bytes:
                raise ValueError('single history entry exceeds report part budget')
            if chunk and size + row_size > part_bytes:
                flush()
            chunk.append(row)
            size += row_size
        flush()
        parent[field[-1]] = {} if kind == 'dict' else []
    index['history_storage'] = {'format': FORMAT, 'complete': True,
        'logical_sha256': digest(complete), 'logical_bytes': len(complete.encode('utf-8')),
        'original_history_storage': original_storage, 'had_history_storage': 'history_storage' in history,
        'parts': specs, 'instructions': 'Load all parts from the same commit; use server/history_parts.py.'}
    files['history.json'] = encode(index)
    if len(files['history.json'].encode('utf-8')) > max(inline_bytes, part_bytes):
        raise ValueError('history index exceeds report budget')
    return files


def load_snapshot(root):
    """Read an immutable ru-checks checkout, rejecting missing/mixed parts."""
    root = Path(root).resolve()
    history = json.loads((root / 'history.json').read_text(encoding='utf-8'))
    storage = history.get('history_storage', {})
    if storage.get('format') != FORMAT:
        return history
    if storage.get('complete') is not True:
        raise ValueError('incomplete history snapshot')
    seen_paths = set()
    allowed = {('streams',)} | {('expansion_campaign', k) for k in
        ('requests', 'cohort_keys', 'previously_working_keys', 'publication_protected_keys')}
    for part in storage['parts']:
        name, field = part['path'], tuple(part['field'])
        path = (root / name).resolve()
        if (not re.fullmatch(r'history-parts/[a-z_0-9-]+\.json', name)
                or not path.is_relative_to(root) or name in seen_paths or field not in allowed):
            raise ValueError('invalid history part path or field')
        seen_paths.add(name)
        data = path.read_bytes()
        if len(data) != part['bytes'] or hashlib.sha256(data).hexdigest() != part['sha256']:
            raise ValueError('history part checksum mismatch')
        value = json.loads(data)
        kind = 'dict' if isinstance(value, dict) else 'list' if isinstance(value, list) else None
        if kind != part['kind'] or len(value) != part['entries']:
            raise ValueError('history part type or count mismatch')
        parent = history
        for name in field[:-1]:
            parent = parent[name]
        target = parent[field[-1]]
        if kind == 'dict':
            if set(target) & value.keys():
                raise ValueError('duplicate history request in parts')
            target.update(value)
        else:
            target.extend(value)
    if storage['had_history_storage']:
        history['history_storage'] = storage['original_history_storage']
    else:
        history.pop('history_storage', None)
    logical = encode(history)
    if digest(logical) != storage['logical_sha256'] or len(logical.encode('utf-8')) != storage['logical_bytes']:
        raise ValueError('history snapshot is incomplete or inconsistent')
    return history


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Reassemble a verified ru-checks history without losing observations')
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.output.resolve() == (args.snapshot / 'history.json').resolve():
        parser.error('choose a separate output file; do not overwrite the snapshot index')
    args.output.write_text(encode(load_snapshot(args.snapshot)), encoding='utf-8')
