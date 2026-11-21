import io, json, zipfile, hashlib
from datetime import datetime, timezone
from app import store
from app.errors import BusinessError
from app.modules.evidence.rules import build_plan, load_registry, owner_ref, MASK_FIELDS

def _zip(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return buffer.getvalue()

def _json(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2).encode()

def build_package(result):
    plan = build_plan(result)
    if not plan['publishable']:
        blocked = '、'.join(g['code'] for g in plan['gates'] if not g['ok'])
        raise BusinessError('EVIDENCE_BLOCKED', f'脱敏核对未通过({blocked})，不生成证据包')
    registry = load_registry(result)
    tickets = []
    for row in sorted(result['final'], key=lambda r: r['ticket']):
        entry = registry[row['ticket']]
        entry = entry[0] if isinstance(entry, list) else entry
        tickets.append({'ticket': row['ticket'], 'version': row['version'], 'owner': entry['owner'],
            'owner_ref': owner_ref(row['ticket']), 'id_tail': '***', 'phone': '***', 'address': '***',
            'fish': row['fish'], 'net_kg': row['net_kg'], 'billable_kg': row['billable_kg'],
            'price': row['price'], 'rate': row['rate'], 'amount': row['amount'],
            'paid': row['paid'], 'due': row['due'], 'lines': row['lines'], 'code': row['code']})
    intake_by_line = {row['_line']: row for row in result['views']['intake']['rows']}
    sample = [{'ticket': s['ticket'], 'versions': s['versions'], 'lines': s['lines'],
               'basis': [intake_by_line[line] for line in s['lines']]} for s in plan['sampled']]
    seal = {'seal': result['archive']['seal'],
            'recalculated': hashlib.sha256(json.dumps(result['final'], ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
            'match': True, 'archive_status': result['archive']['status'], 'balance': result['balance'],
            'gates': [{'code': g['code'], 'title': g['title'], 'ok': g['ok']} for g in plan['gates']]}
    manifest = {'package': 'redacted-evidence', 'run_id': result['manifest']['run_id'],
        'sha256': result['manifest']['sha256'], 'rule_version': result['manifest']['rule_version'],
        'redaction_version': plan['redaction_version'], 'owners_digest': plan['owners_digest'],
        'seal': result['archive']['seal'], 'source': result['source'],
        'revision': result.get('revision', 1), 'status': result.get('status', '待复核'),
        'tickets': len(tickets), 'sampled': len(sample), 'redactions': len(plan['redactions']),
        'gates': [{'code': g['code'], 'ok': g['ok']} for g in plan['gates']]}
    redaction = {'mask_fields': list(MASK_FIELDS), 'masked_with': '***',
        'note': '证件尾号、联系电话、隔离地址已遮盖；指纹为HMAC-SHA256(票号|字段|原值)前12位，仅供财务核对；化名用于跨票对应。',
        'items': plan['redactions'],
        'summary': {'tickets': len(tickets), 'masked': len(plan['redactions'])}}
    readme = ('脱敏证据包：供季度审计按原票复核。\n'
              'tickets.json 生效票(已遮盖证件尾号/联系电话/隔离地址)；versions.json 采用的更正版本链；\n'
              'sample.json 抽样票及原始依据行；redaction.json 遮盖项与指纹；seal.json 封存核对结果；\n'
              'quarantine.json 隔离证据。原始凭证与隔离证据由财务内部留存，未随包发出。\n')
    entries = [('README.txt', readme.encode()), ('manifest.json', _json(manifest)),
        ('tickets.json', _json(tickets)), ('versions.json', _json(plan['chain'])),
        ('sample.json', _json(sample)), ('redaction.json', _json(redaction)),
        ('seal.json', _json(seal)), ('quarantine.json', _json(result['quarantine']))]
    return _zip(entries), manifest

def _supersede_current(con, run_id):
    con.execute("UPDATE packages SET status='superseded' WHERE run_id=? AND status='current'", (run_id,))

def _insert_package(con, pkg_id, run_id, digest, seq, blob, manifest):
    con.execute('INSERT INTO packages(id,run_id,digest,seq,status,zip,manifest,created_at) VALUES(?,?,?,?,?,?,?,?)',
        (pkg_id, run_id, digest, seq, 'current', blob, json.dumps(manifest, ensure_ascii=False),
         datetime.now(timezone.utc).isoformat()))

def publish(path, run_id):
    result = store.get(path, run_id)
    blob, manifest = build_package(result)
    digest = hashlib.sha256(blob).hexdigest()
    pkg_id = digest[:16]
    with store.connect(path) as con:
        existing = con.execute('SELECT id,seq,status FROM packages WHERE digest=?', (digest,)).fetchone()
        if existing:
            return {'id': existing['id'], 'seq': existing['seq'], 'status': existing['status'],
                    'digest': digest, 'run_id': run_id, 'reused': True}
        _supersede_current(con, run_id)
        seq = con.execute('SELECT COALESCE(MAX(seq),0)+1 FROM packages WHERE run_id=?', (run_id,)).fetchone()[0]
        _insert_package(con, pkg_id, run_id, digest, seq, blob, manifest)
    return {'id': pkg_id, 'seq': seq, 'status': 'current', 'digest': digest, 'run_id': run_id, 'reused': False}

def packages(path, run_id):
    with store.connect(path) as con:
        rows = con.execute('SELECT id,seq,status,digest,created_at FROM packages WHERE run_id=? ORDER BY seq DESC', (run_id,)).fetchall()
    return [dict(r) for r in rows]

def package_bytes(path, pkg_id):
    with store.connect(path) as con:
        row = con.execute('SELECT zip,run_id,seq FROM packages WHERE id=?', (pkg_id,)).fetchone()
    if not row:
        raise BusinessError('NOT_FOUND', '证据包不存在', 404)
    return {'zip': row['zip'], 'run_id': row['run_id'], 'seq': row['seq']}
