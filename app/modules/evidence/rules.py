import hashlib, hmac, json, re
from app.config import ROOT, REDACT_SALT

REDACT_VERSION = '2026.11.21-r1'
MASK_FIELDS = ('id_tail', 'phone', 'address')
MIN_SAMPLE, MAX_SAMPLE = 3, 10
_ID_TAIL = re.compile(r'\d{4}')
_PHONE = re.compile(r'1\d{10}')

def load_registry(state):
    registry = state['context'].get('owners')
    if registry is None:
        registry = json.loads((ROOT / 'sample_data/owners.json').read_text())
    return registry

def _entries(registry, ticket):
    entries = registry.get(ticket)
    if entries is None: return []
    return entries if isinstance(entries, list) else [entries]

def owner_ref(ticket):
    return 'HU-' + hmac.new(REDACT_SALT.encode(), f'ref|{ticket}'.encode(), hashlib.sha256).hexdigest()[:6].upper()

def _fingerprint(ticket, field, value):
    return hmac.new(REDACT_SALT.encode(), f'{ticket}|{field}|{value}'.encode(), hashlib.sha256).hexdigest()[:12]

def sample_tickets(final, seed):
    tickets = sorted({row['ticket'] for row in final})
    corrected = {row['ticket'] for row in final if row.get('code') == 'CORRECTED'}
    picked = [t for t in tickets if t in corrected]
    for ticket in tickets:
        if ticket in corrected: continue
        draw = int(hashlib.sha256(f'{seed}|{ticket}'.encode()).hexdigest()[:8], 16)
        if draw % 4 == 0: picked.append(ticket)
    for ticket in tickets:
        if len(picked) >= MIN_SAMPLE: break
        if ticket not in picked: picked.append(ticket)
    return sorted(picked)[:MAX_SAMPLE]

def gate_identity(final, registry):
    items = []
    for row in final:
        ticket = row['ticket']
        entries = _entries(registry, ticket)
        if not entries:
            items.append({'ticket': ticket, 'reason': '船户档案缺失，证件与联系方式无法对应'}); continue
        if len(entries) != 1:
            items.append({'ticket': ticket, 'reason': f'档案存在{len(entries)}条记录，无法稳定对应'}); continue
        entry = entries[0]
        if not _ID_TAIL.fullmatch(str(entry.get('id_tail', ''))):
            items.append({'ticket': ticket, 'reason': '证件尾号缺失或不是4位数字'}); continue
        if not _PHONE.fullmatch(str(entry.get('phone', ''))):
            items.append({'ticket': ticket, 'reason': '联系电话缺失或格式无效'})
    return {'code': 'IDENTITY_STABLE', 'title': '证件与联系方式稳定对应', 'ok': not items, 'items': items}

def gate_basis(sampled, state):
    intake_lines = {row['_line'] for row in state['views']['intake']['rows']}
    items = []
    for ticket in sampled:
        lines = [line for v in state['version_audit'] if v['ticket'] == ticket for line in v['lines']]
        missing = [line for line in lines if line not in intake_lines]
        if not lines:
            items.append({'ticket': ticket, 'reason': '抽样票没有版本链'})
        elif missing:
            items.append({'ticket': ticket, 'reason': f'原始依据行缺失: {missing}'})
    return {'code': 'SAMPLED_BASIS', 'title': '抽样票原始依据完整', 'ok': not items, 'items': items}

def gate_seal(state):
    items = []
    seal = hashlib.sha256(json.dumps(state['final'], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if seal != state['archive']['seal']:
        items.append({'reason': '封存摘要与生效票重算结果不一致'})
    latest = {}
    for v in state['version_audit']:
        latest[v['ticket']] = max(latest.get(v['ticket'], 0), v['version'])
    for row in state['final']:
        if row['version'] != latest.get(row['ticket']):
            items.append({'ticket': row['ticket'], 'reason': f"生效版本v{row['version']}与封存链顶v{latest.get(row['ticket'])}不一致"})
    if state['archive']['status'] != '可归档':
        items.append({'reason': f"归档状态为{state['archive']['status']}"})
    return {'code': 'SEAL_CONSISTENT', 'title': '封存版本与生效版本一致', 'ok': not items, 'items': items}

def build_plan(state):
    registry = load_registry(state)
    seed = state.get('source_digest') or state['manifest']['sha256']
    sampled = []
    for ticket in sample_tickets(state['final'], seed):
        versions = [v for v in state['version_audit'] if v['ticket'] == ticket]
        lines = sorted({line for v in versions for line in v['lines']})
        sampled.append({'ticket': ticket, 'versions': [v['version'] for v in versions], 'lines': lines})
    gates = [gate_identity(state['final'], registry), gate_basis([s['ticket'] for s in sampled], state), gate_seal(state)]
    redactions = []
    for row in sorted(state['final'], key=lambda r: r['ticket']):
        entries = _entries(registry, row['ticket'])
        if len(entries) != 1: continue
        for field in MASK_FIELDS:
            redactions.append({'ticket': row['ticket'], 'field': field,
                               'fingerprint': _fingerprint(row['ticket'], field, str(entries[0].get(field, '')))})
    latest = {}
    for v in state['version_audit']:
        latest[v['ticket']] = max(latest.get(v['ticket'], 0), v['version'])
    chain = [{**v, 'adopted': v['version'] == latest[v['ticket']]} for v in state['version_audit']]
    return {'sampled': sampled, 'redactions': redactions, 'gates': gates, 'chain': chain,
            'publishable': all(g['ok'] for g in gates), 'redaction_version': REDACT_VERSION,
            'owners_digest': hashlib.sha256(json.dumps(registry, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}

def apply(state):
    plan = build_plan(state)
    state['evidence'] = plan
    state['views']['evidence'] = {'title': '证据发布', 'rows': plan['gates'],
        'metrics': {'抽样票': len(plan['sampled']), '遮盖项': len(plan['redactions']),
                    '状态': '可发布' if plan['publishable'] else '阻断'}}
