"""月度线路索赔结算。

口径：
1. 每条登记样本按 货值(分) × 货损率(%) 得到损失金额（四舍五入到分）。
2. 月度损失合计先抵当月合同免赔额（不超过损失合计），再乘赔付比例，最后按封顶截断。
3. 月度赔款用最大余数法逐样本分摊到分，各阶段分摊之和与合计严格相等。
4. 已结账月份冻结金额与明细；样本更正后的差额记入下一开放月份，并追溯原结算单。
5. 结账分两阶段：写账（settling）与确认（settled）。写账中断可续跑，重试不重复入账。
"""
from __future__ import annotations

import re
import threading
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from src.models.entities import Contract, Sample, Settlement, SettlementLine, Adjustment, RouteLock, Shipment

MONTH_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')
CLOSED = ('settling', 'settled')

_py_locks = {}
_py_guard = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat()


def valid_month(m):
    return isinstance(m, str) and bool(MONTH_RE.match(m))


def _month_num(m):
    y, mm = m.split('-')
    return int(y) * 12 + int(mm) - 1


def _month_str(n):
    return f'{n // 12:04d}-{n % 12 + 1:02d}'


def next_open_month(month, closed_months):
    n = _month_num(month) + 1
    while _month_str(n) in closed_months:
        n += 1
    return _month_str(n)


def round_half_up(value):
    return int(Decimal(value).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def allocate(total, weights):
    """最大余数法把 total 分到各权重上，返回整数列表且和恰为 total。"""
    n = len(weights)
    if n == 0 or total <= 0:
        return [0] * n
    weight_sum = sum(weights)
    if weight_sum <= 0:
        out = [0] * n
        out[0] = total
        return out
    base = [total * w // weight_sum for w in weights]
    leftover = total - sum(base)
    order = sorted(range(n), key=lambda i: (-(total * weights[i] % weight_sum), i))
    for i in order[:leftover]:
        base[i] += 1
    return base


def gross_loss_cents(cargo_value_cents, loss_rate):
    return round_half_up(Decimal(cargo_value_cents) * Decimal(str(loss_rate)) / Decimal(100))


def compute_month(samples, contract):
    """按合同口径计算整月，逐样本分摊。contract=(deductible_cents,payout_bp,cap_cents)。"""
    deductible, payout_bp, cap = contract
    items = []
    for row in samples:
        if row.cargo_value_cents is None or row.loss is None:
            continue
        items.append({
            'sample_id': row.id,
            'code': row.code,
            'cargo_value_cents': int(row.cargo_value_cents),
            'loss_rate': float(row.loss),
            'gross_cents': gross_loss_cents(row.cargo_value_cents, row.loss),
        })
    items.sort(key=lambda x: (x['code'], x['sample_id']))
    gross = [i['gross_cents'] for i in items]
    gross_total = sum(gross)

    deductible_applied = min(int(deductible), gross_total)
    deductible_parts = allocate(deductible_applied, gross)
    after_deductible = [g - d for g, d in zip(gross, deductible_parts)]
    after_deductible_total = gross_total - deductible_applied

    after_ratio_total = round_half_up(Decimal(after_deductible_total) * int(payout_bp) / Decimal(10000))
    after_ratio_parts = allocate(after_ratio_total, after_deductible)

    cap_applied_total = 0 if cap is None else max(0, after_ratio_total - int(cap))
    cap_parts = allocate(cap_applied_total, after_ratio_parts)
    payable_parts = [r - c for r, c in zip(after_ratio_parts, cap_parts)]
    base_payable = sum(payable_parts)

    for item, d, ad, ar, c, p in zip(items, deductible_parts, after_deductible,
                                     after_ratio_parts, cap_parts, payable_parts):
        item['deductible_share_cents'] = d
        item['after_deductible_cents'] = ad
        item['after_ratio_cents'] = ar
        item['cap_share_cents'] = c
        item['payable_cents'] = p

    return {
        'items': items,
        'sample_count': len(items),
        'gross_loss_cents': gross_total,
        'deductible_applied_cents': deductible_applied,
        'after_deductible_cents': after_deductible_total,
        'after_ratio_cents': after_ratio_total,
        'cap_applied_cents': cap_applied_total,
        'base_payable_cents': base_payable,
    }


# ---------- 合同 ----------

def contract_for(s, route, month):
    if not month:
        return None
    return (s.query(Contract)
            .filter(Contract.route == route, Contract.effective_month <= month)
            .order_by(Contract.effective_month.desc(), Contract.id.desc())
            .first())


def contract_dict(row):
    if not row:
        return None
    return {'id': row.id, 'route': row.route, 'effective_month': row.effective_month,
            'deductible_cents': row.deductible_cents, 'payout_bp': row.payout_bp,
            'cap_cents': row.cap_cents, 'note': row.note}


# ---------- 样本与缺失 ----------

def route_samples(s, route, month):
    return (s.query(Sample).join(Shipment, Shipment.id == Sample.shipment_id)
            .filter(Shipment.route == route, Sample.settle_month == month).all())


def missing_samples(samples):
    return [r for r in samples if r.loss is None or r.cargo_value_cents is None]


def sample_route(s, sample):
    ship = s.get(Shipment, sample.shipment_id)
    return ship.route if ship else None


# ---------- 锁 ----------

def _py_lock(route):
    with _py_guard:
        return _py_locks.setdefault(route, threading.RLock())


class route_locks:
    """按线路名排序获取进程内锁与数据库行锁，结账/更正串行化。

    行锁锚点行只在缺失时补建；已存在时直接 UPDATE 取锁，避免 SAVEPOINT 冲刷
    调用方尚未提交的样本修改。
    """

    def __init__(self, s, routes):
        self.s = s
        self.routes = sorted({r for r in routes if r})
        self.handles = []

    def __enter__(self):
        for r in self.routes:
            h = _py_lock(r)
            h.acquire()
            self.handles.append(h)
        for r in self.routes:
            if self.s.get(RouteLock, r) is None:
                self._ensure_row(r)
            self.s.execute(text('UPDATE route_locks SET route=route WHERE route=:r'), {'r': r})
        return self

    def _ensure_row(self, route):
        try:
            with self.s.begin_nested():
                self.s.add(RouteLock(route=route))
                self.s.flush()
        except IntegrityError:
            pass

    def __exit__(self, *exc):
        for h in reversed(self.handles):
            h.release()
        return False


# ---------- 已结月份的重算与更正差额 ----------

def plan_adjustments(base_payable, adjustments):
    """按 id 顺序决定本月可入账的差额与顺延部分。

    返回 (可入账合计, 顺延合计, [(adjustment, 本月入账额, 顺延额)])。
    负差额最多把本月赔款抵到 0 分，剩余部分下月继续抵，避免出现负赔款。
    """
    capacity = base_payable + sum(int(a.delta_cents) for a in adjustments if a.delta_cents > 0)
    booked = deferred = 0
    plan = []
    for a in adjustments:
        delta = int(a.delta_cents)
        if delta >= 0 or capacity + delta >= 0:
            capacity += delta
            booked += delta
            plan.append((a, delta, 0))
        elif capacity > 0:
            booked += -capacity
            deferred += delta + capacity
            plan.append((a, -capacity, delta + capacity))
            capacity = 0
        else:
            deferred += delta
            plan.append((a, 0, delta))
    return booked, deferred, plan


def closed_months(s, route):
    rows = s.query(Settlement.month).filter(Settlement.route == route,
                                            Settlement.state.in_(CLOSED)).all()
    return {r[0] for r in rows}


def _lines_by_sample(s, settlement_id):
    rows = (s.query(SettlementLine)
            .filter(SettlementLine.settlement_id == settlement_id, SettlementLine.kind == 'sample').all())
    return {r.sample_id: r for r in rows}


def _recompute_settlement(s, st, closed, actor=""):
    """重算已冻结结算单的当前口径金额，把与冻结值的差记到下一开放月份。

    已结月份里被改成缺失货损/货值的样本无法重算，保留原赔款不生成差额，
    由 overview 单列“已结月份数据缺失”提醒补齐，避免静默冲减。
    """
    samples = route_samples(s, st.route, st.month)
    contract = (int(st.deductible_cents or 0), int(st.payout_bp or 0), st.cap_cents)
    fresh = compute_month(samples, contract)
    new_map = {i['sample_id']: i for i in fresh['items']}
    held = {r.id for r in missing_samples(samples)}
    old_map = _lines_by_sample(s, st.id)

    ids = sorted(set(new_map) | {sid for sid in old_map if sid is not None})
    if not ids:
        return

    target = next_open_month(st.month, closed)
    pending = {a.sample_id: a for a in s.query(Adjustment).filter(
        Adjustment.source_settlement_id == st.id, Adjustment.status == 'pending').all()}
    booked = defaultdict(int)
    for a in s.query(Adjustment).filter(
            Adjustment.source_settlement_id == st.id,
            Adjustment.status.in_(('reserved', 'booked'))).all():
        booked[a.sample_id] += a.delta_cents

    for sid in ids:
        new_item = new_map.get(sid)
        old_payable = int(old_map[sid].payable_cents) if sid in old_map else 0
        if new_item is None and sid in old_map and sid in held:
            # 数据变缺失：不生成冲减差额，保留原单口径等待补齐
            new_payable = old_payable
        else:
            new_payable = int(new_item['payable_cents']) if new_item else 0
        cumulative = new_payable - old_payable
        outstanding = cumulative - booked[sid]
        row = pending.pop(sid, None)
        if outstanding == 0:
            if row:
                s.delete(row)
            continue
        code = (new_item or {}).get('code') or (old_map[sid].code if sid in old_map else None)
        reason = ('更正后重算：原赔款 %d 分，现赔款 %d 分，已结转 %d 分' %
                  (old_payable, new_payable, booked[sid]))
        if row is None:
            row = Adjustment(route=st.route, target_month=target, sample_id=sid,
                             sample_code=code, source_settlement_id=st.id,
                             source_month=st.month, created_by=actor, created=now())
            s.add(row)
        row.target_month = target
        row.old_payable_cents = old_payable
        row.new_payable_cents = new_payable
        row.delta_cents = outstanding
        row.old_gross_cents = int(old_map[sid].gross_cents) if sid in old_map else 0
        row.new_gross_cents = int(new_item['gross_cents']) if new_item else 0
        row.reason = reason
        row.status = 'pending'
        row.created_by = actor
    for row in pending.values():
        s.delete(row)


def refresh_corrections(s, routes, actor=''):
    """样本变更后调用（调用方已持有 route_locks）。"""
    for route in sorted({r for r in routes if r}):
        closed = closed_months(s, route)
        for st in s.query(Settlement).filter(Settlement.route == route,
                                             Settlement.state.in_(CLOSED)).all():
            _recompute_settlement(s, st, closed, actor)


def refresh_routes_for_sample_change(s, old_route, new_route, actor='', commit=False):
    """在调用方事务内加锁并重算两条线路的更正差额。

    commit=True 时在锁内提交，保证并发结账看不到“样本已改、差额未记”的中间态。
    """
    with route_locks(s, [old_route, new_route]):
        refresh_corrections(s, [old_route, new_route], actor)
        if commit:
            s.commit()


# ---------- 结账 ----------

def _load(s, settlement_id):
    return s.get(Settlement, settlement_id)


def _finalize(s, st, actor):
    """第二阶段：确认已写账的结算单。幂等。"""
    if st.state == 'settled':
        return st
    s.query(Adjustment).filter(Adjustment.booked_settlement_id == st.id,
                               Adjustment.status == 'reserved').update(
        {'status': 'booked'}, synchronize_session='fetch')
    st.state = 'settled'
    st.settled_at = now()
    st.settled_by = actor
    st.updated = now()
    s.commit()
    return st


def settle(s, route, month, actor, key=None):
    """结账入口。返回 (settlement, replayed)。"""
    if not valid_month(month):
        raise ValueError('结算月格式应为 YYYY-MM')
    key = key or f'{route}|{month}'
    with route_locks(s, [route]):
        st = (s.query(Settlement).filter(Settlement.route == route, Settlement.month == month)
              .with_for_update().one_or_none())
        if st and st.state == 'settled':
            return st, True
        if st and st.state == 'settling':
            return _finalize(s, st, actor), True
        st = _write(s, route, month, actor, key)
        return _finalize(s, st, actor), False


def _write(s, route, month, actor, key):
    """第一阶段：按合同口径写账并预留更正差额；状态为 settling，可续跑。"""
    contract = contract_for(s, route, month)
    if not contract:
        raise LookupError(f'线路 {route} 在 {month} 无生效合同，无法结账')
    samples = route_samples(s, route, month)
    missing = missing_samples(samples)
    if missing:
        codes = '、'.join(r.code for r in missing)
        raise LookupError(f'存在缺失货损或货值的样本，无法结账：{codes}')
    if not samples:
        has_adjustment = s.query(Adjustment).filter(
            Adjustment.route == route, Adjustment.status == 'pending',
            Adjustment.target_month <= month).first()
        if not has_adjustment:
            raise LookupError(f'线路 {route} 在 {month} 没有登记样本')

    calc = compute_month(samples, (contract.deductible_cents, contract.payout_bp, contract.cap_cents))
    adjustments = (s.query(Adjustment)
                   .filter(Adjustment.route == route, Adjustment.status == 'pending',
                           Adjustment.target_month <= month)
                   .order_by(Adjustment.id).all())
    # 负差额不能把本月赔款压成负数：按 id 顺序抵到 0 为止，剩余部分拆行顺延到下月
    carry_month = next_open_month(month, closed_months(s, route) | {month})
    _, _, plan = plan_adjustments(calc['base_payable_cents'], adjustments)
    booked_adjustments = []
    for a, applied, rest_delta in plan:
        if applied == 0:
            # 本月完全抵不动：原行整笔顺延，不拆行
            a.target_month = carry_month
            continue
        if rest_delta:
            s.add(Adjustment(route=a.route, target_month=carry_month, sample_id=a.sample_id,
                             sample_code=a.sample_code, source_settlement_id=a.source_settlement_id,
                             source_month=a.source_month,
                             old_payable_cents=a.old_payable_cents, new_payable_cents=a.new_payable_cents,
                             old_gross_cents=a.old_gross_cents, new_gross_cents=a.new_gross_cents,
                             delta_cents=rest_delta, reason=a.reason, status='pending',
                             created_by=a.created_by, created=now()))
        a.delta_cents = applied
        booked_adjustments.append(a)
    if not samples and not booked_adjustments:
        raise LookupError(f'线路 {route} 在 {month} 没有可结账的样本或差额')
    for a in booked_adjustments:
        a.status = 'reserved'
        a.booked_settlement_id = None
        a.created_by = actor
    s.flush()
    adjustment_total = sum(int(a.delta_cents) for a in booked_adjustments)

    st = Settlement(route=route, month=month, state='settling',
                    contract_id=contract.id, deductible_cents=contract.deductible_cents,
                    payout_bp=contract.payout_bp, cap_cents=contract.cap_cents,
                    gross_loss_cents=calc['gross_loss_cents'],
                    deductible_applied_cents=calc['deductible_applied_cents'],
                    after_deductible_cents=calc['after_deductible_cents'],
                    after_ratio_cents=calc['after_ratio_cents'],
                    cap_applied_cents=calc['cap_applied_cents'],
                    base_payable_cents=calc['base_payable_cents'],
                    adjustment_cents=adjustment_total,
                    payable_cents=calc['base_payable_cents'] + adjustment_total,
                    sample_count=calc['sample_count'],
                    settle_key=key, created=now(), updated=now())
    s.add(st)
    s.flush()

    for item in calc['items']:
        s.add(SettlementLine(settlement_id=st.id, kind='sample', sample_id=item['sample_id'],
                             code=item['code'], cargo_value_cents=item['cargo_value_cents'],
                             loss_rate=item['loss_rate'], gross_cents=item['gross_cents'],
                             deductible_share_cents=item['deductible_share_cents'],
                             after_deductible_cents=item['after_deductible_cents'],
                             after_ratio_cents=item['after_ratio_cents'],
                             cap_share_cents=item['cap_share_cents'],
                             payable_cents=item['payable_cents'], created=now()))
    for a in booked_adjustments:
        s.add(SettlementLine(settlement_id=st.id, kind='adjustment', sample_id=a.sample_id,
                             code=a.sample_code, gross_cents=a.delta_cents,
                             payable_cents=a.delta_cents, source_settlement_id=a.source_settlement_id,
                             source_month=a.source_month, adjustment_id=a.id, created=now()))
        a.booked_settlement_id = st.id
    s.commit()
    return st


def resume_incomplete(s):
    """续跑写账中断的结算单；返回完成的结算单 id 列表。"""
    rows = s.query(Settlement).filter(Settlement.state == 'settling').all()
    done = []
    for st in rows:
        with route_locks(s, [st.route]):
            fresh = s.get(Settlement, st.id)
            if fresh and fresh.state == 'settling':
                _finalize(s, fresh, 'system:resume')
                done.append(st.id)
    return done


# ---------- 展示 ----------

def settlement_dict(s, st):
    lines = (s.query(SettlementLine).filter(SettlementLine.settlement_id == st.id)
             .order_by(SettlementLine.kind.desc(), SettlementLine.id).all())
    return {
        'id': st.id, 'route': st.route, 'month': st.month, 'state': st.state,
        'contract_id': st.contract_id, 'deductible_cents': st.deductible_cents,
        'payout_bp': st.payout_bp, 'cap_cents': st.cap_cents,
        'gross_loss_cents': st.gross_loss_cents,
        'deductible_applied_cents': st.deductible_applied_cents,
        'after_deductible_cents': st.after_deductible_cents,
        'after_ratio_cents': st.after_ratio_cents,
        'cap_applied_cents': st.cap_applied_cents,
        'base_payable_cents': st.base_payable_cents,
        'adjustment_cents': st.adjustment_cents,
        'payable_cents': st.payable_cents,
        'sample_count': st.sample_count,
        'settle_key': st.settle_key, 'settled_at': st.settled_at, 'settled_by': st.settled_by,
        'lines': [{'kind': l.kind, 'sample_id': l.sample_id, 'code': l.code,
                   'cargo_value_cents': l.cargo_value_cents, 'loss_rate': l.loss_rate,
                   'gross_cents': l.gross_cents, 'deductible_share_cents': l.deductible_share_cents,
                   'after_deductible_cents': l.after_deductible_cents,
                   'after_ratio_cents': l.after_ratio_cents,
                   'cap_share_cents': l.cap_share_cents, 'payable_cents': l.payable_cents,
                   'source_settlement_id': l.source_settlement_id,
                   'source_month': l.source_month} for l in lines],
    }


def overview(s, month=None):
    months = {r[0] for r in s.query(Sample.settle_month).filter(Sample.settle_month.isnot(None)).distinct()}
    months |= {r[0] for r in s.query(Settlement.month).distinct()}
    months = sorted(months)
    if month and not valid_month(month):
        raise ValueError('结算月格式应为 YYYY-MM')
    month = month or (months[-1] if months else None)

    routes = [r[0] for r in s.query(Shipment.route).distinct()]
    routes += [r[0] for r in s.query(Settlement.route).distinct()]
    routes = sorted(set(routes))

    rows = []
    for route in routes:
        samples = route_samples(s, route, month) if month else []
        contract = contract_for(s, route, month) if month else None
        st = (s.query(Settlement).filter_by(route=route, month=month).one_or_none()
              if month else None)
        missing = missing_samples(samples)
        pending_adj = (s.query(Adjustment).filter(Adjustment.route == route,
                                                  Adjustment.status == 'pending',
                                                  Adjustment.target_month <= month).all()
                       if month else [])
        frozen_missing = []
        if st and st.state in CLOSED:
            frozen_missing = [r.code for r in missing]
        computed = None
        blockers = []
        closed_now = bool(st and st.state in CLOSED)
        if not contract and not closed_now:
            blockers.append('无生效合同')
        if missing and not closed_now:
            blockers.append('缺失货损或货值：' + '、'.join(r.code for r in missing))
        if month and not samples and not pending_adj:
            blockers.append('该月无登记样本')
        if contract and samples and not missing:
            calc = compute_month(samples, (contract.deductible_cents, contract.payout_bp, contract.cap_cents))
            computed = {k: calc[k] for k in ('sample_count', 'gross_loss_cents', 'deductible_applied_cents',
                                             'after_deductible_cents', 'after_ratio_cents',
                                             'cap_applied_cents', 'base_payable_cents')}
        adjustment_pending = sum(int(a.delta_cents) for a in pending_adj)
        base_preview = computed['base_payable_cents'] if computed else 0
        booked_preview, deferred_preview, _ = plan_adjustments(base_preview, pending_adj)
        rows.append({
            'route': route, 'month': month, 'contract': contract_dict(contract),
            'sample_count': len(samples), 'missing': [r.code for r in missing],
            'blockers': blockers, 'computed': computed,
            'frozen_missing': frozen_missing,
            'adjustments_pending_cents': adjustment_pending,
            'adjustments_deferred_cents': deferred_preview,
            'payable_preview_cents': base_preview + booked_preview,
            'settlement': ({'id': st.id, 'state': st.state, 'payable_cents': st.payable_cents,
                            'base_payable_cents': st.base_payable_cents,
                            'gross_loss_cents': st.gross_loss_cents,
                            'deductible_applied_cents': st.deductible_applied_cents,
                            'after_deductible_cents': st.after_deductible_cents,
                            'after_ratio_cents': st.after_ratio_cents,
                            'cap_applied_cents': st.cap_applied_cents,
                            'sample_count': st.sample_count,
                            'adjustment_cents': st.adjustment_cents,
                            'settled_at': st.settled_at} if st else None),
        })

    unassigned = (s.query(Sample).filter(Sample.settle_month.is_(None),
                                         (Sample.loss.isnot(None)) | (Sample.cargo_value_cents.isnot(None)))
                  .order_by(Sample.code).all())
    return {'month': month, 'months': months, 'routes': rows,
            'unassigned': [{'id': r.id, 'code': r.code, 'route': sample_route(s, r),
                            'loss': r.loss, 'cargo_value_cents': r.cargo_value_cents}
                           for r in unassigned]}
