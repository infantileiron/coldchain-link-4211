"""HTTP 层回归：鉴权、缺失阻断、幂等重试、更正追溯、合同不可回改。

运行：pytest -q backend/tests
"""
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix='claims-api-', dir='/tmp'))
os.environ['DATABASE_URL'] = 'sqlite:///' + str(TMP / f'{uuid.uuid4().hex}.db')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6399/0')
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from src.config.database import Base, engine, Session  # noqa: E402
from src.main import app  # noqa: E402
from src.models.entities import Sample, Shipment, User  # noqa: E402
from src.utils.security import hash_password  # noqa: E402

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as s:
        for role in ('admin', 'analyst', 'viewer'):
            s.add(User(email=f'{role}@demo.local', role=role, password=hash_password('Demo123!')))
        ship = Shipment(code='XD-1', route='鲜达线路 01', zone='冷藏', driver='驾驶员1')
        s.add(ship)
        s.flush()
        for i, (value, loss) in enumerate([(12345, 4.2), (777, 3.1), (99999, 2.5)]):
            s.add(Sample(shipment_id=ship.id, code=f'S{i}', exposure=30, excursions=3,
                         loss=loss, cargo_value_cents=value, settle_month='2026-10'))
        s.commit()
    yield


def auth(role):
    r = client.post('/auth/login', json={'email': f'{role}@demo.local', 'password': 'Demo123!'})
    assert r.status_code == 200
    return {'Authorization': 'Bearer ' + r.json()['token']}


def add_contract(role='admin', **over):
    payload = {'route': '鲜达线路 01', 'effective_month': '2026-01',
               'deductible_cents': 200, 'payout_bp': 8000, 'cap_cents': 5000}
    payload.update(over)
    return client.post('/contracts', json=payload, headers=auth(role))


def test_permissions():
    assert add_contract('analyst').status_code == 403
    assert add_contract('viewer').status_code == 403
    assert add_contract('admin').status_code == 200
    assert client.post('/claims/settle', json={'route': '鲜达线路 01', 'month': '2026-10'},
                       headers=auth('analyst')).status_code == 403
    assert client.post('/claims/settle', json={'route': '鲜达线路 01', 'month': '2026-10'},
                       headers=auth('viewer')).status_code == 403
    assert client.get('/claims', headers=auth('viewer')).status_code == 200


def test_board_and_settle_idempotent():
    add_contract()
    board = client.get('/claims?month=2026-10', headers=auth('analyst')).json()
    row = board['routes'][0]
    assert row['blockers'] == []
    assert row['computed']['base_payable_cents'] == 2274
    assert row['payable_preview_cents'] == 2274

    first = client.post('/claims/settle', json={'route': '鲜达线路 01', 'month': '2026-10', 'settle_key': 'req-9'},
                        headers=auth('admin')).json()
    second = client.post('/claims/settle', json={'route': '鲜达线路 01', 'month': '2026-10', 'settle_key': 'req-9'},
                         headers=auth('admin')).json()
    assert first['replayed'] is False and second['replayed'] is True
    assert first['settlement']['id'] == second['settlement']['id']
    assert second['settlement']['payable_cents'] == 2274
    detail = client.get(f"/settlements/{first['settlement']['id']}", headers=auth('viewer')).json()
    assert len(detail['lines']) == 3
    assert sum(l['payable_cents'] for l in detail['lines']) == 2274
    assert client.get('/settlements', headers=auth('viewer')).json()[0]['state'] == 'settled'


def test_missing_blocks_with_409():
    add_contract()
    with Session() as s:
        s.add(Sample(shipment_id=1, code='MISS', exposure=1, excursions=0,
                     loss=None, cargo_value_cents=100, settle_month='2026-10'))
        s.commit()
    r = client.post('/claims/settle', json={'route': '鲜达线路 01', 'month': '2026-10'}, headers=auth('admin'))
    assert r.status_code == 409 and 'MISS' in r.json()['detail']
    board = client.get('/claims?month=2026-10', headers=auth('admin')).json()
    assert 'MISS' in board['routes'][0]['missing']
    assert any('MISS' in b for b in board['routes'][0]['blockers'])


def test_no_contract_409_and_bad_month_422():
    r = client.post('/claims/settle', json={'route': '鲜达线路 01', 'month': '2026-10'}, headers=auth('admin'))
    assert r.status_code == 409
    assert client.get('/claims?month=2026-13', headers=auth('admin')).status_code == 422
    assert client.post('/claims/settle', json={'route': '鲜达线路 01', 'month': '2026-13'},
                       headers=auth('admin')).status_code == 422


def test_contract_locked_after_settlement():
    c = add_contract().json()
    client.post('/claims/settle', json={'route': '鲜达线路 01', 'month': '2026-10'}, headers=auth('admin'))
    r = client.put(f"/contracts/{c['id']}",
                   json={'route': '鲜达线路 01', 'effective_month': '2026-01',
                         'deductible_cents': 1, 'payout_bp': 1, 'cap_cents': 1},
                   headers=auth('admin'))
    assert r.status_code == 409
    # 新增下一版本则允许
    assert add_contract(effective_month='2026-11', deductible_cents=300).status_code == 200


def test_backfill_after_close_creates_traced_adjustment():
    add_contract()
    st = client.post('/claims/settle', json={'route': '鲜达线路 01', 'month': '2026-10'},
                     headers=auth('admin')).json()['settlement']
    # 跨月补录同月样本
    r = client.post('/samples', json={'shipment_id': 1, 'code': 'LATE', 'loss': 4.0,
                                      'cargo_value_cents': 25000, 'settle_month': '2026-10'},
                    headers=auth('analyst'))
    assert r.status_code == 200
    adj = client.get('/adjustments', headers=auth('viewer')).json()
    assert adj, '补录后应生成更正差额'
    assert any(a['sample_code'] == 'LATE' for a in adj)
    assert all(a['target_month'] == '2026-11' for a in adj)
    assert all(a['source_settlement_id'] == st['id'] for a in adj)
    assert all(a['source_month'] == '2026-10' for a in adj)
    # 月度免赔与封顶是整月口径：新增样本会改变整月重算，逐样本差额之和等于整月增量
    total_delta = sum(a['delta_cents'] for a in adj)
    assert total_delta == 800  # (3042+1000-200)×80% − 2274
    # 原单保持原值
    assert client.get(f"/settlements/{st['id']}", headers=auth('viewer')).json()['payable_cents'] == 2274
    # 11 月结账需要样本或差额；这里只有差额也应允许
    with Session() as s:
        s.add(Sample(shipment_id=1, code='NOV', loss=2.0, cargo_value_cents=5000, settle_month='2026-11'))
        s.commit()
    nov = client.post('/claims/settle', json={'route': '鲜达线路 01', 'month': '2026-11'},
                      headers=auth('admin')).json()['settlement']
    assert nov['adjustment_cents'] == total_delta
    assert nov['payable_cents'] == nov['base_payable_cents'] + total_delta
    line = next(l for l in nov['lines'] if l['kind'] == 'adjustment')
    assert line['source_month'] == '2026-10' and line['source_settlement_id'] == st['id']


def test_resume_endpoint():
    add_contract()
    assert client.post('/claims/resume', headers=auth('admin')).json() == {'resumed': []}
    assert client.post('/claims/resume', headers=auth('analyst')).status_code == 403
