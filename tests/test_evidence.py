import io, json, zipfile, hashlib
import pytest
from fastapi.testclient import TestClient
from app import main, store, publisher
from app.engine import evaluate
from app.config import ROOT
from app.errors import BusinessError
from app.modules.evidence.rules import build_plan

def owners():
    return json.loads((ROOT / 'sample_data/owners.json').read_text())

def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    return TestClient(main.app)

def saved(tmp_path, state):
    db = str(tmp_path / 't.sqlite')
    store.save(db, state)
    return db, state['manifest']['run_id']

def gate(plan, code):
    return [g for g in plan['gates'] if g['code'] == code][0]

# --- 核对闸门 ---

def test_default_batch_publishable(sample):
    assert sample['evidence']['publishable'] is True
    assert all(g['ok'] for g in sample['evidence']['gates'])

def test_sampling_deterministic_and_covers_corrected():
    a, b = evaluate(), evaluate()
    sa = [s['ticket'] for s in a['evidence']['sampled']]
    assert sa == [s['ticket'] for s in b['evidence']['sampled']]
    assert '00001' in sa and 3 <= len(sa) <= 10

def test_redaction_covers_every_ticket(sample):
    redactions = sample['evidence']['redactions']
    assert len(redactions) == len(sample['final']) * 3
    assert {r['field'] for r in redactions} == {'id_tail', 'phone', 'address'}
    assert len({r['fingerprint'] for r in redactions}) == len(redactions)

def test_identity_missing_blocks():
    registry = owners(); del registry['00007']
    plan = evaluate({'overrides': {'owners': registry}})['evidence']
    assert not plan['publishable']
    assert any(i['ticket'] == '00007' for i in gate(plan, 'IDENTITY_STABLE')['items'])

def test_identity_conflict_blocks():
    registry = owners()
    registry['00003'] = [registry['00003'], dict(registry['00003'], phone='13900000000')]
    plan = evaluate({'overrides': {'owners': registry}})['evidence']
    assert not gate(plan, 'IDENTITY_STABLE')['ok'] and not plan['publishable']

def test_identity_bad_phone_blocks():
    registry = owners(); registry['00005']['phone'] = '123'
    plan = evaluate({'overrides': {'owners': registry}})['evidence']
    assert not gate(plan, 'IDENTITY_STABLE')['ok']

def test_sampled_basis_missing_blocks():
    state = evaluate()
    lines = {ln for v in state['version_audit'] if v['ticket'] == '00001' for ln in v['lines']}
    state['views']['intake']['rows'] = [r for r in state['views']['intake']['rows'] if r['_line'] not in lines]
    plan = build_plan(state)
    assert not gate(plan, 'SAMPLED_BASIS')['ok'] and not plan['publishable']
    assert any(i['ticket'] == '00001' for i in gate(plan, 'SAMPLED_BASIS')['items'])

def test_seal_version_mismatch_blocks():
    state = evaluate()
    state['final'][0]['amount'] = '0.01'
    plan = build_plan(state)
    assert not gate(plan, 'SEAL_CONSISTENT')['ok'] and not plan['publishable']

# --- 证据包 ---

def unzip(blob):
    return zipfile.ZipFile(io.BytesIO(blob))

def test_package_deterministic():
    state = evaluate()
    first, _ = publisher.build_package(state)
    second, _ = publisher.build_package(state)
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()

def test_package_contents_and_seal(tmp_path):
    db, rid = saved(tmp_path, evaluate())
    info = publisher.publish(db, rid)
    z = unzip(publisher.package_bytes(db, info['id'])['zip'])
    assert {'README.txt', 'manifest.json', 'tickets.json', 'versions.json', 'sample.json',
            'redaction.json', 'seal.json', 'quarantine.json'} <= set(z.namelist())
    manifest = json.loads(z.read('manifest.json'))
    assert manifest['run_id'] == rid and all(g['ok'] for g in manifest['gates'])
    seal = json.loads(z.read('seal.json'))
    assert seal['match'] and seal['seal'] == seal['recalculated']
    assert len(json.loads(z.read('quarantine.json'))) == 8
    sample = json.loads(z.read('sample.json'))
    assert all(entry['basis'] for entry in sample)
    versions = json.loads(z.read('versions.json'))
    adopted = [v for v in versions if v['adopted']]
    assert len(adopted) == 20 and {v['ticket'] for v in adopted if v['version'] == 2} == {'00001'}

def test_package_has_no_pii(tmp_path):
    db, rid = saved(tmp_path, evaluate())
    info = publisher.publish(db, rid)
    z = unzip(publisher.package_bytes(db, info['id'])['zip'])
    text = ''.join(z.read(name).decode() for name in z.namelist())
    for entry in owners().values():
        assert entry['phone'] not in text
        assert entry['address'] not in text
    tickets = json.loads(z.read('tickets.json'))
    assert all(row['id_tail'] == '***' and row['phone'] == '***' and row['address'] == '***' for row in tickets)
    assert all(set(item) == {'ticket', 'field', 'fingerprint'} for item in json.loads(z.read('redaction.json'))['items'])
    assert tickets[0]['owner'] and tickets[0]['owner_ref'].startswith('HU-')

# --- 幂等与发布恢复 ---

def test_publish_idempotent_same_batch(tmp_path):
    db, rid = saved(tmp_path, evaluate())
    first = publisher.publish(db, rid)
    store.save(db, evaluate())  # 同输入重跑，批号不变
    second = publisher.publish(db, rid)
    assert first['id'] == second['id'] and second['reused']
    assert len(publisher.packages(db, rid)) == 1

def test_blocked_publish_creates_nothing(tmp_path):
    registry = owners(); del registry['00007']
    db, rid = saved(tmp_path, evaluate({'overrides': {'owners': registry}}))
    with pytest.raises(BusinessError) as error:
        publisher.publish(db, rid)
    assert error.value.code == 'EVIDENCE_BLOCKED'
    assert publisher.packages(db, rid) == []

def test_interrupted_publish_restores_previous(tmp_path, monkeypatch):
    db, rid = saved(tmp_path, evaluate())
    first = publisher.publish(db, rid)
    store.review(db, rid, 1)
    second = publisher.publish(db, rid)
    assert second['seq'] == 2 and second['id'] != first['id']
    store.review(db, rid, 2)
    def boom(*args, **kwargs): raise RuntimeError('发布中断')
    monkeypatch.setattr(publisher, '_insert_package', boom)
    with pytest.raises(RuntimeError):
        publisher.publish(db, rid)
    packages = publisher.packages(db, rid)
    assert len(packages) == 2
    assert packages[0]['seq'] == 2 and packages[0]['status'] == 'current'
    assert packages[1]['seq'] == 1 and packages[1]['status'] == 'superseded'
    assert publisher.package_bytes(db, first['id'])['zip']  # 旧版证据包仍在
    run = store.get(db, rid)  # 原始凭证与隔离证据未误删
    assert len(run['quarantine']) == 8 and 'input_snapshot' in run and run['records']

# --- HTTP 端到端 ---

def test_publish_flow_http(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={}).json()
    rid = run['manifest']['run_id']
    page = c.get('/evidence?run_id=' + rid)
    assert page.status_code == 200
    for text in ('抽样票', '遮盖项', '采用的更正版本', '封存核对结果'):
        assert text in page.text
    published = c.post(f'/api/runs/{rid}/publish').json()
    assert published['seq'] == 1 and not published['reused']
    again = c.post(f'/api/runs/{rid}/publish').json()
    assert again['id'] == published['id'] and again['reused']
    assert len(c.get(f'/api/runs/{rid}/packages').json()) == 1
    download = c.get(f"/api/packages/{published['id']}")
    assert download.status_code == 200 and download.headers['content-type'] == 'application/zip'
    assert 'manifest.json' in unzip(download.content).namelist()

def test_rerun_same_batch_no_duplicate_package(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={}).json()['manifest']['run_id']
    first = c.post(f'/api/runs/{rid}/publish').json()
    assert c.post(f'/api/runs/{rid}/rerun').json()['manifest']['run_id'] == rid
    second = c.post(f'/api/runs/{rid}/publish').json()
    assert first['id'] == second['id']
    assert len(c.get(f'/api/runs/{rid}/packages').json()) == 1

def test_blocked_publish_http(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    registry = owners(); del registry['00007']
    rid = c.post('/api/runs', json={'overrides': {'owners': registry}}).json()['manifest']['run_id']
    response = c.post(f'/api/runs/{rid}/publish')
    assert response.status_code == 422 and response.json()['code'] == 'EVIDENCE_BLOCKED'
    assert c.get(f'/api/runs/{rid}/packages').json() == []

def test_superseded_package_still_downloadable(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    rid = c.post('/api/runs', json={}).json()['manifest']['run_id']
    first = c.post(f'/api/runs/{rid}/publish').json()
    assert c.post(f'/api/runs/{rid}/review', json={'revision': 1}).status_code == 200
    second = c.post(f'/api/runs/{rid}/publish').json()
    assert second['seq'] == 2
    packages = {p['seq']: p for p in c.get(f'/api/runs/{rid}/packages').json()}
    assert packages[1]['status'] == 'superseded' and packages[2]['status'] == 'current'
    assert c.get(f"/api/packages/{first['id']}").status_code == 200
