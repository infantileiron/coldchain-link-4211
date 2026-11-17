"""月度索赔结算的回归测试：口径、分摊尾差、更正差额、并发与续跑。

运行：pytest -q backend/tests
使用临时 SQLite，不依赖 Docker。
"""
import os
import sys
import threading
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TMP = Path('/tmp') / f'claims-test-{uuid.uuid4().hex}'
TMP.mkdir(parents=True)
os.environ['DATABASE_URL'] = 'sqlite:///' + str(TMP / 'test.db')
os.environ.setdefault('JWT_SECRET', 'test-secret')
sys.path.insert(0, str(ROOT))

from src.config.database import Base, engine, Session  # noqa: E402
from src.models.entities import Contract, Sample, Shipment, Settlement, SettlementLine, Adjustment  # noqa: E402
from src.services import claims  # noqa: E402
from src.services.claims import round_half_up  # noqa: E402


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as s:
        yield s


def mkroute(s, route='鲜达线路 01'):
    ship = Shipment(code=f'XD-{uuid.uuid4().hex[:6]}', route=route, zone='冷藏', driver='驾驶员1')
    s.add(ship)
    s.flush()
    return ship


def mksample(s, ship, code, value_cents, loss, month='2026-10'):
    row = Sample(shipment_id=ship.id, code=code, exposure=30, excursions=3,
                 loss=loss, cargo_value_cents=value_cents, settle_month=month)
    s.add(row)
    s.flush()
    return row


def contract(s, route='鲜达线路 01', month='2026-01', ded=0, bp=10000, cap=10**9):
    row = Contract(route=route, effective_month=month, deductible_cents=ded,
                   payout_bp=bp, cap_cents=cap)
    s.add(row)
    s.commit()
    return row


def test_allocate_is_exact():
    for total in (0, 1, 7, 100, 999983):
        for weights in ([1, 1, 1], [1, 2, 3], [0, 5], [3, 3, 3, 3, 3], [10 ** 6, 1]):
            parts = claims.allocate(total, weights)
            assert sum(parts) == total
            assert all(p >= 0 for p in parts)


def test_contract_picks_latest_effective_month(db):
    s = db
    contract(s, month='2026-01', ded=100)
    contract(s, month='2026-10', ded=500)
    mkroute(s)
    s.commit()
    assert claims.contract_for(s, '鲜达线路 01', '2026-09').deductible_cents == 100
    assert claims.contract_for(s, '鲜达线路 01', '2026-10').deductible_cents == 500
    assert claims.contract_for(s, '鲜达线路 01', '2025-12') is None


def test_month_formula_and_allocation_exact(db):
    s = db
    ship = mkroute(s)
    mksample(s, ship, 'S0', 12345, 4.2)
    mksample(s, ship, 'S1', 777, 3.1)
    mksample(s, ship, 'S2', 99999, 2.5)
    contract(s, ded=200, bp=8000, cap=5000)
    st, replayed = claims.settle(s, '鲜达线路 01', '2026-10', 'admin@demo.local')
    assert not replayed
    d = claims.settlement_dict(s, st)
    # 518 + 24 + 2500 = 3042；先抵 200 → 2842；×80% = 2273.6 → 2274
    assert d['gross_loss_cents'] == 3042
    assert d['deductible_applied_cents'] == 200
    assert d['after_deductible_cents'] == 2842
    assert d['after_ratio_cents'] == 2274
    assert d['cap_applied_cents'] == 0
    assert d['payable_cents'] == 2274
    sample_lines = [l for l in d['lines'] if l['kind'] == 'sample']
    assert sum(l['gross_cents'] for l in sample_lines) == d['gross_loss_cents']
    assert sum(l['deductible_share_cents'] for l in sample_lines) == 200
    assert sum(l['after_ratio_cents'] for l in sample_lines) == 2274
    assert sum(l['payable_cents'] for l in sample_lines) == d['payable_cents']
    assert d['state'] == 'settled'


def test_cap_applied_and_allocated(db):
    s = db
    ship = mkroute(s)
    for i in range(7):
        mksample(s, ship, f'C{i}', 10000 + i * 137, 3.0 + i * 0.11)
    contract(s, ded=0, bp=10000, cap=1234)
    st, _ = claims.settle(s, '鲜达线路 01', '2026-10', 'admin')
    d = claims.settlement_dict(s, st)
    lines = [l for l in d['lines'] if l['kind'] == 'sample']
    assert d['after_ratio_cents'] > 1234
    assert d['cap_applied_cents'] == d['after_ratio_cents'] - 1234
    assert d['payable_cents'] == 1234
    assert sum(l['cap_share_cents'] for l in lines) == d['cap_applied_cents']
    assert sum(l['payable_cents'] for l in lines) == 1234


def test_missing_loss_blocks_settlement(db):
    s = db
    ship = mkroute(s)
    mksample(s, ship, 'S0', 10000, 3.0)
    mksample(s, ship, 'S1', 10000, None)
    contract(s)
    board = claims.overview(s, '2026-10')
    row = next(r for r in board['routes'] if r['route'] == '鲜达线路 01')
    assert 'S1' in row['missing']
    assert any('S1' in b for b in row['blockers'])
    with pytest.raises(LookupError) as exc:
        claims.settle(s, '鲜达线路 01', '2026-10', 'admin')
    assert 'S1' in str(exc.value)
    assert s.query(Settlement).count() == 0


def test_missing_cargo_value_blocks(db):
    s = db
    ship = mkroute(s)
    mksample(s, ship, 'S0', None, 3.0)
    contract(s)
    with pytest.raises(LookupError):
        claims.settle(s, '鲜达线路 01', '2026-10', 'admin')


def test_settlement_without_contract_blocked(db):
    s = db
    ship = mkroute(s)
    mksample(s, ship, 'S0', 10000, 3.0)
    with pytest.raises(LookupError):
        claims.settle(s, '鲜达线路 01', '2026-10', 'admin')


def test_retry_is_idempotent(db):
    s = db
    ship = mkroute(s)
    mksample(s, ship, 'S0', 10000, 3.0)
    contract(s)
    a, rep1 = claims.settle(s, '鲜达线路 01', '2026-10', 'admin', key='req-1')
    b, rep2 = claims.settle(s, '鲜达线路 01', '2026-10', 'admin', key='req-1')
    c, rep3 = claims.settle(s, '鲜达线路 01', '2026-10', 'admin')
    assert rep1 is False and rep2 is True and rep3 is True
    assert a.id == b.id == c.id
    assert s.query(Settlement).count() == 1
    assert s.query(SettlementLine).count() == 1


def test_closed_month_frozen_and_correction_rolls_forward(db):
    s = db
    ship = mkroute(s)
    mksample(s, ship, 'S0', 12345, 4.2)
    s2 = mksample(s, ship, 'S2', 99999, 2.5)
    mksample(s, ship, 'S3', 5000, 1.0, month='2026-11')
    contract(s, ded=200, bp=8000, cap=5000)
    oct_st, _ = claims.settle(s, '鲜达线路 01', '2026-10', 'admin')
    oct_payable = oct_st.payable_cents

    s2.loss = 3.0
    s.commit()
    claims.refresh_routes_for_sample_change(s, '鲜达线路 01', '鲜达线路 01')
    adj = s.query(Adjustment).filter_by(source_settlement_id=oct_st.id).all()
    assert adj, '更正应生成差额记录'
    assert all(a.target_month == '2026-11' for a in adj)
    assert all(a.source_month == '2026-10' for a in adj)

    # 已结单金额不变
    assert s.get(Settlement, oct_st.id).payable_cents == oct_payable
    assert s.get(Settlement, oct_st.id).state == 'settled'

    delta_total = sum(a.delta_cents for a in adj)
    nov_st, _ = claims.settle(s, '鲜达线路 01', '2026-11', 'admin')
    dnov = claims.settlement_dict(s, nov_st)
    assert dnov['adjustment_cents'] == delta_total
    assert dnov['payable_cents'] == dnov['base_payable_cents'] + delta_total
    adj_lines = [l for l in dnov['lines'] if l['kind'] == 'adjustment']
    assert sum(l['payable_cents'] for l in adj_lines) == delta_total
    assert all(l['source_month'] == '2026-10' for l in adj_lines)
    assert all(l['source_settlement_id'] == oct_st.id for l in adj_lines)
    assert all(a.status == 'booked' for a in s.query(Adjustment).all())


def test_correction_twice_accumulates_without_double_count(db):
    s = db
    ship = mkroute(s)
    row = mksample(s, ship, 'S0', 10000, 10.0)
    mksample(s, ship, 'S1', 5000, 2.0, month='2026-11')
    contract(s, ded=0, bp=10000, cap=10**9)
    oct_st, _ = claims.settle(s, '鲜达线路 01', '2026-10', 'admin')

    row.loss = 12.0
    s.commit()
    claims.refresh_routes_for_sample_change(s, '鲜达线路 01', '鲜达线路 01')
    first = sum(a.delta_cents for a in s.query(Adjustment).all())
    # 再改一次：差额应相对“原单赔款”累计，而不是叠加两次
    row.loss = 15.0
    s.commit()
    claims.refresh_routes_for_sample_change(s, '鲜达线路 01', '鲜达线路 01')
    pend = s.query(Adjustment).filter_by(status='pending').all()
    assert sum(a.delta_cents for a in pend) == 1500 - 1000
    assert sum(a.delta_cents for a in pend) > first or first == 200

    nov_st, _ = claims.settle(s, '鲜达线路 01', '2026-11', 'admin')
    d = claims.settlement_dict(s, nov_st)
    assert d['adjustment_cents'] == 500


def test_correction_after_rollforward_creates_new_delta(db):
    s = db
    ship = mkroute(s)
    row = mksample(s, ship, 'S0', 10000, 10.0)
    mksample(s, ship, 'S1', 5000, 2.0, month='2026-11')
    mksample(s, ship, 'S2', 5000, 2.0, month='2026-12')
    contract(s, ded=0, bp=10000, cap=10**9)
    claims.settle(s, '鲜达线路 01', '2026-10', 'admin')
    row.loss = 12.0
    s.commit()
    claims.refresh_routes_for_sample_change(s, '鲜达线路 01', '鲜达线路 01')
    nov_st, _ = claims.settle(s, '鲜达线路 01', '2026-11', 'admin')
    assert nov_st.adjustment_cents == 200

    # 11 月结账后再改 10 月样本：新差额应进入 12 月，不再改 11 月
    row.loss = 15.0
    s.commit()
    claims.refresh_routes_for_sample_change(s, '鲜达线路 01', '鲜达线路 01')
    pend = s.query(Adjustment).filter_by(status='pending').all()
    assert len(pend) == 1 and pend[0].delta_cents == 300 and pend[0].target_month == '2026-12'
    assert s.get(Settlement, nov_st.id).adjustment_cents == 200

    dec_st, _ = claims.settle(s, '鲜达线路 01', '2026-12', 'admin')
    assert dec_st.adjustment_cents == 300


def test_correction_reverses_to_zero(db):
    s = db
    ship = mkroute(s)
    row = mksample(s, ship, 'S0', 10000, 10.0)
    mksample(s, ship, 'S1', 5000, 2.0, month='2026-11')
    contract(s)
    claims.settle(s, '鲜达线路 01', '2026-10', 'admin')
    row.loss = 12.0
    s.commit()
    claims.refresh_routes_for_sample_change(s, '鲜达线路 01', '鲜达线路 01')
    row.loss = 10.0
    s.commit()
    claims.refresh_routes_for_sample_change(s, '鲜达线路 01', '鲜达线路 01')
    assert s.query(Adjustment).filter_by(status='pending').count() == 0


def test_interrupted_write_resumes_without_double_booking(db):
    s = db
    ship = mkroute(s)
    mksample(s, ship, 'S0', 10000, 10.0)
    mksample(s, ship, 'S1', 5000, 2.0)
    contract(s)
    # 手工模拟“写账完成、确认前中断”
    with claims.route_locks(s, ['鲜达线路 01']):
        st = Settlement(route='鲜达线路 01', month='2026-10', state='settling',
                        contract_id=1, deductible_cents=0, payout_bp=10000, cap_cents=10**9,
                        gross_loss_cents=1100, payable_cents=1100, sample_count=2,
                        settle_key='k1')
        s.add(st)
        s.flush()
        s.add(SettlementLine(settlement_id=st.id, kind='sample', code='S0', payable_cents=1000))
        s.add(SettlementLine(settlement_id=st.id, kind='sample', code='S1', payable_cents=100))
        s.commit()
        sid = st.id

    resumed = claims.resume_incomplete(s)
    assert resumed == [sid]
    st2, replayed = claims.settle(s, '鲜达线路 01', '2026-10', 'admin')
    assert replayed is True
    assert st2.state == 'settled'
    assert s.query(Settlement).count() == 1
    assert s.query(SettlementLine).count() == 2
    assert st2.payable_cents == 1100


def test_concurrent_settle_single_result(db):
    s = db
    ship = mkroute(s)
    for i in range(5):
        mksample(s, ship, f'S{i}', 10000 + i, 2.0 + i)
    contract(s, ded=100, bp=8000, cap=10**9)
    s.commit()

    results = []
    errors = []

    def worker():
        with Session() as ss:
            try:
                st, rep = claims.settle(ss, '鲜达线路 01', '2026-10', 'admin@demo.local')
                results.append((st.id, st.payable_cents, rep))
            except Exception as e:  # pragma: no cover - 调试用
                errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    assert len({r[0] for r in results}) == 1
    assert len({r[1] for r in results}) == 1
    with Session() as check:
        assert check.query(Settlement).count() == 1
        assert check.query(SettlementLine).count() == 5


def test_concurrent_correction_and_settle_no_lost_delta(db):
    s = db
    ship = mkroute(s)
    row = mksample(s, ship, 'S0', 10000, 10.0)
    mksample(s, ship, 'S1', 5000, 2.0, month='2026-11')
    contract(s)
    st, _ = claims.settle(s, '鲜达线路 01', '2026-10', 'admin')
    row_id = row.id
    s.commit()

    errors = []

    def correct():
        with Session() as ss:
            try:
                r = ss.get(Sample, row_id)
                r.loss = 15.0
                ss.commit()
                claims.refresh_routes_for_sample_change(ss, '鲜达线路 01', '鲜达线路 01')
                ss.commit()
            except Exception as e:  # pragma: no cover
                errors.append('correct:' + repr(e))

    def settle_nov():
        with Session() as ss:
            try:
                claims.settle(ss, '鲜达线路 01', '2026-11', 'admin')
            except Exception as e:  # pragma: no cover
                errors.append('settle:' + repr(e))

    threads = [threading.Thread(target=correct), threading.Thread(target=settle_nov)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors

    with Session() as check:
        nov = check.query(Settlement).filter_by(month='2026-11').one()
        booked = sum(a.delta_cents for a in check.query(Adjustment)
                     .filter_by(source_settlement_id=st.id, status='booked').all())
        # 差额只在 11 月结账单或仍待结转中记一次；更正若晚于 11 月结账则顺延到 12 月
        pending = sum(a.delta_cents for a in check.query(Adjustment)
                      .filter_by(source_settlement_id=st.id, status='pending').all())
        assert booked + pending == 500
        assert nov.adjustment_cents == booked


def test_overview_preview_and_unassigned(db):
    s = db
    ship = mkroute(s)
    mksample(s, ship, 'S0', 10000, 10.0)
    loose = Sample(shipment_id=ship.id, code='L0', exposure=1, excursions=1, loss=2.0,
                   cargo_value_cents=100, settle_month=None)
    s.add(loose)
    contract(s)
    board = claims.overview(s, '2026-10')
    row = next(r for r in board['routes'] if r['route'] == '鲜达线路 01')
    assert row['computed']['base_payable_cents'] == 1000
    assert row['payable_preview_cents'] == 1000
    assert any(u['code'] == 'L0' for u in board['unassigned'])


def test_cap_and_deductible_never_overshoot(db):
    """封顶小于免赔后余额、免赔大于损失等极端组合仍保持分账守恒。"""
    s = db
    cases = [
        dict(ded=10 ** 7, bp=10000, cap=10 ** 7),
        dict(ded=0, bp=10000, cap=0),
        dict(ded=0, bp=0, cap=1000),
        dict(ded=333, bp=3333, cap=1),
    ]
    for n, c in enumerate(cases):
        route = f'线路极端 {n}'
        sh = mkroute(s, route=route)
        for i in range(4):
            mksample(s, sh, f'E{n}-{i}', 10000 * (i + 1) + 7, 3.33 + i)
        contract(s, route=route, **c)
    s.commit()
    for n, c in enumerate(cases):
        route = f'线路极端 {n}'
        st, _ = claims.settle(s, route, '2026-10', 'admin')
        d = claims.settlement_dict(s, st)
        lines = [l for l in d['lines'] if l['kind'] == 'sample']
        assert sum(l['payable_cents'] for l in lines) == d['payable_cents']
        assert d['payable_cents'] >= 0
        assert d['payable_cents'] <= max(0, d['after_ratio_cents'])
        assert sum(l['deductible_share_cents'] for l in lines) == d['deductible_applied_cents']
        assert sum(l['cap_share_cents'] for l in lines) == d['cap_applied_cents']


def test_moving_sample_between_routes_adjusts_both(db):
    s = db
    a = mkroute(s, route='线路甲')
    b = mkroute(s, route='线路乙')
    row = mksample(s, a, 'M0', 10000, 10.0)
    mksample(s, b, 'M3', 10000, 2.0)
    mksample(s, b, 'M1', 10000, 5.0, month='2026-11')
    mksample(s, a, 'M2', 10000, 5.0, month='2026-11')
    contract(s, route='线路甲')
    contract(s, route='线路乙')
    st_a, _ = claims.settle(s, '线路甲', '2026-10', 'admin')
    st_b, _ = claims.settle(s, '线路乙', '2026-10', 'admin')
    assert st_a.payable_cents == 1000 and st_b.payable_cents == 200

    row.shipment_id = b.id
    s.commit()
    claims.refresh_routes_for_sample_change(s, '线路甲', '线路乙')
    adj_a = s.query(Adjustment).filter_by(source_settlement_id=st_a.id).all()
    adj_b = s.query(Adjustment).filter_by(source_settlement_id=st_b.id).all()
    assert sum(a.delta_cents for a in adj_a) == -1000
    assert sum(a.delta_cents for a in adj_b) == 1000
    # 原单均冻结
    assert s.get(Settlement, st_a.id).payable_cents == 1000
    assert s.get(Settlement, st_b.id).payable_cents == 200


def test_correction_targets_next_open_month_skipping_closed(db):
    s = db
    ship = mkroute(s)
    row = mksample(s, ship, 'K0', 10000, 10.0)
    mksample(s, ship, 'K1', 10000, 1.0, month='2026-11')
    mksample(s, ship, 'K2', 10000, 1.0, month='2026-12')
    contract(s)
    claims.settle(s, '鲜达线路 01', '2026-10', 'admin')
    claims.settle(s, '鲜达线路 01', '2026-11', 'admin')
    row.loss = 20.0
    s.commit()
    claims.refresh_routes_for_sample_change(s, '鲜达线路 01', '鲜达线路 01')
    pend = s.query(Adjustment).filter_by(status='pending').all()
    assert len(pend) == 1 and pend[0].target_month == '2026-12'


def test_backfill_into_closed_month_creates_delta(db):
    """跨月补录：结账后补登同月样本，差额进入下一开放月并追溯原单。"""
    s = db
    ship = mkroute(s)
    mksample(s, ship, 'B0', 10000, 10.0)
    mksample(s, ship, 'B1', 10000, 1.0, month='2026-11')
    contract(s)
    oct_st, _ = claims.settle(s, '鲜达线路 01', '2026-10', 'admin')
    late = Sample(shipment_id=ship.id, code='B9', exposure=10, excursions=1,
                  loss=4.0, cargo_value_cents=25000, settle_month='2026-10')
    s.add(late)
    s.flush()
    claims.refresh_routes_for_sample_change(s, None, '鲜达线路 01', 'analyst@demo.local')
    s.commit()
    adj = s.query(Adjustment).filter_by(source_settlement_id=oct_st.id).all()
    assert len(adj) == 1
    assert adj[0].sample_code == 'B9'
    assert adj[0].delta_cents == 1000  # 25000 × 4% = 1000，全额赔付
    assert adj[0].target_month == '2026-11'
    assert adj[0].created_by == 'analyst@demo.local'
    assert s.get(Settlement, oct_st.id).payable_cents == 1000  # 原单不变

    nov_st, _ = claims.settle(s, '鲜达线路 01', '2026-11', 'admin')
    assert nov_st.adjustment_cents == 1000
    assert nov_st.payable_cents == 100 + 1000


def test_allocation_residuals_exact_on_ugly_numbers(db):
    """分摊尾差：3 条样本、免赔与比例都无法整除时，各阶段合计仍精确。"""
    s = db
    ship = mkroute(s)
    for i in range(3):
        mksample(s, ship, f'U{i}', 3333 + i, 3.33)
    contract(s, ded=7, bp=3333, cap=100000)
    st, _ = claims.settle(s, '鲜达线路 01', '2026-10', 'admin')
    d = claims.settlement_dict(s, st)
    lines = [l for l in d['lines'] if l['kind'] == 'sample']
    assert sum(l['gross_cents'] for l in lines) == d['gross_loss_cents']
    assert sum(l['deductible_share_cents'] for l in lines) == 7
    assert sum(l['after_ratio_cents'] for l in lines) == d['after_ratio_cents']
    assert sum(l['payable_cents'] for l in lines) == d['payable_cents']
    # 修正后的比例赔付应等于剩余损失 × 33.33%，四舍五入到分
    expect = round_half_up((d['gross_loss_cents'] - 7) * 3333 / 10000)
    assert d['after_ratio_cents'] == expect
    assert d['payable_cents'] == expect


def test_shipment_route_change_moves_all_corrections(db):
    s = db
    a = mkroute(s, route='线路甲')
    b = mkroute(s, route='线路乙')
    mksample(s, a, 'R0', 10000, 10.0)
    mksample(s, b, 'R1', 10000, 2.0)
    contract(s, route='线路甲')
    contract(s, route='线路乙')
    st_a, _ = claims.settle(s, '线路甲', '2026-10', 'admin')
    st_b, _ = claims.settle(s, '线路乙', '2026-10', 'admin')
    a.route = '线路乙'
    s.commit()
    claims.refresh_routes_for_sample_change(s, '线路甲', '线路乙')
    # 甲线路已无当月样本：已结单仍冻结，差额冲回
    adj_a = s.query(Adjustment).filter_by(source_settlement_id=st_a.id).all()
    assert sum(x.delta_cents for x in adj_a) == -1000
    # 乙线路新增样本：差额补入
    adj_b = s.query(Adjustment).filter_by(source_settlement_id=st_b.id).all()
    assert sum(x.delta_cents for x in adj_b) == 1000
    assert s.get(Settlement, st_a.id).payable_cents == 1000
    assert s.get(Settlement, st_b.id).payable_cents == 200


def test_write_failure_rolls_back_and_leaves_no_partial(db, monkeypatch):
    """写账阶段抛错时不能留下结算单或预留的差额。"""
    s = db
    ship = mkroute(s)
    mksample(s, ship, 'S0', 10000, 10.0)
    mksample(s, ship, 'S1', 5000, 2.0, month='2026-11')
    mksample(s, ship, 'S2', 5000, 2.0, month='2026-11')
    contract(s)
    claims.settle(s, '鲜达线路 01', '2026-10', 'admin')
    row = s.query(Sample).filter_by(code='S0').one()
    row.loss = 15.0
    s.commit()
    claims.refresh_routes_for_sample_change(s, '鲜达线路 01', '鲜达线路 01')
    s.commit()  # 与 API 一致：样本更正与差额重算同一事务提交
    assert s.query(Adjustment).filter_by(status='pending').count() == 1

    real_commit = s.commit
    calls = {'n': 0}

    def flaky_commit():
        calls['n'] += 1
        if calls['n'] == 1:  # 首次提交（写账）失败，模拟断电/异常
            raise RuntimeError('boom')
        return real_commit()

    monkeypatch.setattr(s, 'commit', flaky_commit)
    with pytest.raises(RuntimeError):
        claims.settle(s, '鲜达线路 01', '2026-11', 'admin')
    monkeypatch.setattr(s, 'commit', real_commit)
    s.rollback()

    assert s.query(Settlement).filter_by(month='2026-11').count() == 0
    assert s.query(Adjustment).filter_by(status='pending').count() == 1
    assert s.query(Adjustment).filter_by(status='reserved').count() == 0

    # 恢复后重试可正常结账，差额只入账一次
    st, replayed = claims.settle(s, '鲜达线路 01', '2026-11', 'admin')
    assert st.adjustment_cents == 500
    assert s.query(Adjustment).filter_by(status='booked').count() == 1


def test_resume_after_interruption_books_adjustments_once(db):
    """写账中断后续跑：差额由 reserved 变为 booked，只入账一次。"""
    s = db
    ship = mkroute(s)
    mksample(s, ship, 'S0', 10000, 10.0)
    mksample(s, ship, 'S1', 5000, 2.0, month='2026-11')
    contract(s)
    claims.settle(s, '鲜达线路 01', '2026-10', 'admin')
    row = s.query(Sample).filter_by(code='S0').one()
    row.loss = 13.0
    s.commit()
    claims.refresh_routes_for_sample_change(s, '鲜达线路 01', '鲜达线路 01')
    s.commit()

    # 模拟：差额已预留并写账，但确认前进程中断
    with claims.route_locks(s, ['鲜达线路 01']):
        st = claims._write(s, '鲜达线路 01', '2026-11', 'admin', 'k')
        sid = st.id
        assert st.state == 'settling'
        assert s.query(Adjustment).filter_by(status='reserved').count() == 1

    resumed = claims.resume_incomplete(s)
    assert resumed == [sid]
    st2, replayed = claims.settle(s, '鲜达线路 01', '2026-11', 'admin')
    assert replayed and st2.state == 'settled'
    assert st2.adjustment_cents == 300
    assert s.query(Adjustment).filter_by(status='booked').count() == 1
    assert s.query(SettlementLine).filter_by(settlement_id=sid).count() == 2  # 1 样本 + 1 差额


def test_closed_month_sample_becoming_missing_holds_original(db):
    """已结月份样本被改成缺失：不生成冲减差额，保留原单，界面提示补齐。"""
    s = db
    ship = mkroute(s)
    row = mksample(s, ship, 'H0', 10000, 10.0)
    mksample(s, ship, 'H1', 5000, 2.0, month='2026-11')
    contract(s)
    st, _ = claims.settle(s, '鲜达线路 01', '2026-10', 'admin')
    row.cargo_value_cents = None
    s.commit()
    claims.refresh_routes_for_sample_change(s, '鲜达线路 01', '鲜达线路 01')
    s.commit()
    assert s.query(Adjustment).count() == 0
    assert s.get(Settlement, st.id).payable_cents == 1000
    board = claims.overview(s, '2026-10')
    r = next(x for x in board['routes'] if x['route'] == '鲜达线路 01')
    assert r['frozen_missing'] == ['H0']
    assert not any('缺失' in b for b in r['blockers'])  # 已结月份不再显示为结账阻断

    # 补齐后按原单口径重新生成差额
    row.cargo_value_cents = 20000
    s.commit()
    claims.refresh_routes_for_sample_change(s, '鲜达线路 01', '鲜达线路 01')
    s.commit()
    adj = s.query(Adjustment).filter_by(source_settlement_id=st.id).all()
    assert sum(a.delta_cents for a in adj) == 1000
    assert claims.overview(s, '2026-10')['routes'][0]['frozen_missing'] == []


def test_negative_adjustment_never_makes_payable_negative(db):
    """更正冲减超过当月应付时，只抵到 0，其余顺延到下月，赔款不为负。"""
    s = db
    ship = mkroute(s)
    row = mksample(s, ship, 'N0', 100000, 10.0)     # 10 月赔款 10000
    mksample(s, ship, 'N1', 1000, 1.0, month='2026-11')   # 11 月仅 10 分
    mksample(s, ship, 'N2', 100000, 5.0, month='2026-12')  # 12 月 5000
    contract(s)
    claims.settle(s, '鲜达线路 01', '2026-10', 'admin')
    row.loss = 0.01  # 重算赔款 10 分，冲减 9990 分
    row.cargo_value_cents = 100000
    s.commit()
    claims.refresh_routes_for_sample_change(s, '鲜达线路 01', '鲜达线路 01')
    s.commit()
    adj = sum(a.delta_cents for a in s.query(Adjustment).filter_by(status='pending'))
    assert adj == -9990

    nov, _ = claims.settle(s, '鲜达线路 01', '2026-11', 'admin')
    assert nov.payable_cents == 0            # 10 − 9990 抵到 0
    assert nov.adjustment_cents == -10
    carried = s.query(Adjustment).filter_by(status='pending').all()
    assert len(carried) == 1
    assert carried[0].target_month == '2026-12'
    assert carried[0].delta_cents == -9980

    dec, _ = claims.settle(s, '鲜达线路 01', '2026-12', 'admin')
    assert dec.payable_cents == 0  # 5000 也只抵到 0
    rest = s.query(Adjustment).filter_by(status='pending').all()
    assert len(rest) == 1 and rest[0].target_month == '2027-01'
    assert rest[0].delta_cents == -4980

    # 预览与结账一致
    mksample(s, ship, 'N3', 1000000, 10.0, month='2027-01')
    board = claims.overview(s, '2027-01')
    r = next(x for x in board['routes'] if x['route'] == '鲜达线路 01')
    assert r['payable_preview_cents'] == r['computed']['base_payable_cents'] - 4980
    jan, _ = claims.settle(s, '鲜达线路 01', '2027-01', 'admin')
    assert jan.payable_cents == r['payable_preview_cents']
    assert s.query(Adjustment).filter_by(status='pending').count() == 0
