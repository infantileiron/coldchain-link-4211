import random
from datetime import date
from src.config.database import Base,engine,Session
from src.models.entities import User,Shipment,Sample,Contract
from src.utils.security import hash_password


def _prev_month():
 today=date.today()
 y,m=today.year,today.month-1
 if m==0:y,m=y-1,12
 return f'{y:04d}-{m:02d}'


def seed():
 Base.metadata.create_all(engine);rng=random.Random(3102)
 month=_prev_month()
 with Session() as s:
  for role in ('admin','analyst','viewer'):
   email=role+'@demo.local'
   if not s.query(User).filter_by(email=email).first():s.add(User(email=email,role=role,password=hash_password('Demo123!')))
  s.commit()
  for i in range(60):
   code=f'XD-1115-{i+1:03}'
   ship=s.query(Shipment).filter_by(code=code).first()
   if not ship:
    ship=Shipment(code=code,route=f'鲜达线路 {i%12+1:02}',zone=('冷藏' if i<30 else '冷冻' if i<50 else '常温对照'),driver=f'驾驶员{i%12+1}');s.add(ship);s.flush()
   exposure=round(rng.uniform(3,85),1);loss=round(min(100,1+exposure*.11+rng.uniform(-.9,.9)),2)
   if i>=50:exposure=20
   if not s.query(Sample).filter_by(code=code+'-S').first():
    # 第 9、39 条样本故意保持货值为空：它们单列为缺失并阻止所属线路结账
    value=None if i in (8,38) else rng.randrange(800_00,26000_00)
    s.add(Sample(shipment_id=ship.id,code=code+'-S',exposure=None if i in (5,37) else exposure,
                 excursions=int(exposure/10),loss=loss,cargo_value_cents=value,settle_month=month))
  s.commit()
  # 12 条线路各一份按月生效的合同；线路 03 从本月起调整免赔与比例，演示版本切换
  for r in range(1,13):
   route=f'鲜达线路 {r:02d}'
   if not s.query(Contract).filter_by(route=route,effective_month='2025-01').first():
    s.add(Contract(route=route,effective_month='2025-01',deductible_cents=500_00,
                   payout_bp=8000,cap_cents=80000_00,note='基准合同',created='seed',updated='seed'))
   if r==3 and not s.query(Contract).filter_by(route=route,effective_month=month).first():
    s.add(Contract(route=route,effective_month=month,deductible_cents=300_00,
                   payout_bp=9000,cap_cents=60000_00,note='续签：降低免赔、提高赔付',created='seed',updated='seed'))
  s.commit();print({'shipments':s.query(Shipment).count(),'samples':s.query(Sample).count(),'users':s.query(User).count(),'contracts':s.query(Contract).count()})
if __name__=='__main__':seed()
