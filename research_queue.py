"""Load the complete, immutable research archive without approving identities."""
from __future__ import annotations

import gzip
import hashlib
import json


def archive_items(root, specs, existing_keys, key, validate):
    """Validate every archived URL+headers pair; never filter country or name.

    Reuse the already published compressed files rather than exporting a second
    copy of their URLs. Newly admitted keys remain subject to identity review,
    even if another catalog later assigns one of them to a familiar channel.
    """
    items = {}
    for spec in specs:
        name = spec.get('path', '')
        path = (root / name).resolve()
        if not name or not path.is_relative_to(root.resolve()):
            raise ValueError('invalid frozen research archive path')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != spec.get('sha256'):
            raise ValueError('frozen research archive hash mismatch')
        seen = set()
        with gzip.open(path, 'rt', encoding='utf-8') as source:
            for line in source:
                row = json.loads(line)
                validate(row['url'])
                headers = row.get('headers', {})
                if (not isinstance(headers, dict) or
                        any(k not in ('User-Agent', 'Referer') or not isinstance(v, str)
                            or '\r' in v or '\n' in v for k, v in headers.items())):
                    raise ValueError('invalid research request headers')
                item = {'url': row['url'], 'headers': headers, 'attrs': {},
                        'name': (row.get('names') or ['Unidentified candidate'])[0],
                        'unsupported': False, 'research_only': True}
                request_key = key(item)
                if request_key != row.get('key') or request_key in seen:
                    raise ValueError('frozen research request key mismatch or duplicate')
                seen.add(request_key)
                if request_key not in existing_keys:
                    item['identity_review_required'] = True
                    items[request_key] = item
        if len(seen) != spec.get('request_count'):
            raise ValueError('frozen research archive request count mismatch')
    return list(items.values())
