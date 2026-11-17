from sqlalchemy import Column,Integer,String,Float,JSON,Text,UniqueConstraint,Index
from src.config.database import Base
class User(Base):
 __tablename__='users'
 id=Column(Integer,primary_key=True);email=Column(String(100),unique=True);password=Column(String(200));role=Column(String(20))
class Shipment(Base):
 __tablename__='shipments'
 id=Column(Integer,primary_key=True);code=Column(String(60),unique=True);route=Column(String(60));zone=Column(String(20));driver=Column(String(60))
class Sample(Base):
 __tablename__='samples'
 id=Column(Integer,primary_key=True);shipment_id=Column(Integer,index=True);code=Column(String(60),unique=True);exposure=Column(Float,nullable=True);excursions=Column(Integer,nullable=True);loss=Column(Float,nullable=True)
 # 索赔口径：货值以分存储，避免浮点尾差；结算月为 YYYY-MM，缺失表示未归属月份
 cargo_value_cents=Column(Integer,nullable=True);settle_month=Column(String(7),nullable=True,index=True)
class Study(Base):
 __tablename__='studies'
 id=Column(Integer,primary_key=True);name=Column(String(100));zone=Column(String(20));feature=Column(String(30));state=Column(String(20),default='queued');result=Column(JSON,nullable=True);error=Column(Text,default='')
class Recommendation(Base):
 __tablename__='recommendations'
 id=Column(Integer,primary_key=True);study_id=Column(Integer);note=Column(Text);owner=Column(String(60));status=Column(String(20),default='draft')
class Audit(Base):
 __tablename__='audits'
 id=Column(Integer,primary_key=True);actor=Column(String(100));action=Column(String(200));created=Column(String(40))
class Contract(Base):
 """线路合同：同一线路同一生效月唯一；结算取生效月 <= 结算月的最新版本。"""
 __tablename__='contracts'
 id=Column(Integer,primary_key=True);route=Column(String(60),nullable=False,index=True);effective_month=Column(String(7),nullable=False)
 deductible_cents=Column(Integer,nullable=False,default=0);payout_bp=Column(Integer,nullable=False,default=0);cap_cents=Column(Integer,nullable=False,default=0)
 note=Column(String(300),default='');created=Column(String(40),default='');updated=Column(String(40),default='')
 __table_args__=(UniqueConstraint('route','effective_month',name='uq_contract_route_month'),)
class Settlement(Base):
 """线路月度结算单。pending=待核算，settled=已结（金额与明细冻结）。"""
 __tablename__='settlements'
 id=Column(Integer,primary_key=True);route=Column(String(60),nullable=False,index=True);month=Column(String(7),nullable=False)
 state=Column(String(20),default='pending',nullable=False)
 contract_id=Column(Integer,nullable=True);deductible_cents=Column(Integer,nullable=True);payout_bp=Column(Integer,nullable=True);cap_cents=Column(Integer,nullable=True)
 gross_loss_cents=Column(Integer,default=0);deductible_applied_cents=Column(Integer,default=0);after_deductible_cents=Column(Integer,default=0)
 after_ratio_cents=Column(Integer,default=0);cap_applied_cents=Column(Integer,default=0);base_payable_cents=Column(Integer,default=0)
 adjustment_cents=Column(Integer,default=0);payable_cents=Column(Integer,default=0);sample_count=Column(Integer,default=0)
 # 已结单的“影子重算基线”：仅用于计算后续更正差额，不改变原单金额
 settle_key=Column(String(100),nullable=True,index=True);settled_at=Column(String(40),nullable=True);settled_by=Column(String(100),nullable=True)
 created=Column(String(40),default='');updated=Column(String(40),default='')
 __table_args__=(UniqueConstraint('route','month',name='uq_settlement_route_month'),)
class SettlementLine(Base):
 """已结单的样本/调整分摊快照，金额到分。"""
 __tablename__='settlement_lines'
 id=Column(Integer,primary_key=True);settlement_id=Column(Integer,nullable=False,index=True);kind=Column(String(20),default='sample')
 sample_id=Column(Integer,nullable=True);code=Column(String(60),nullable=True);cargo_value_cents=Column(Integer,default=0);loss_rate=Column(Float,nullable=True)
 gross_cents=Column(Integer,default=0);deductible_share_cents=Column(Integer,default=0);after_deductible_cents=Column(Integer,default=0)
 after_ratio_cents=Column(Integer,default=0);cap_share_cents=Column(Integer,default=0);payable_cents=Column(Integer,default=0)
 # 调整行：追溯来源结算单与来源月份
 source_settlement_id=Column(Integer,nullable=True);source_month=Column(String(7),nullable=True);adjustment_id=Column(Integer,nullable=True)
 created=Column(String(40),default='')
 __table_args__=(Index('ix_settlement_lines_settle_sample','settlement_id','sample_id'),)
class Adjustment(Base):
 """已结月份的事后更正差额，转入下一开放月份，并追溯来源原单。"""
 __tablename__='adjustments'
 id=Column(Integer,primary_key=True);route=Column(String(60),nullable=False,index=True);target_month=Column(String(7),nullable=False,index=True)
 sample_id=Column(Integer,nullable=True);sample_code=Column(String(60),nullable=True)
 source_settlement_id=Column(Integer,nullable=True,index=True);source_month=Column(String(7),nullable=True)
 old_payable_cents=Column(Integer,default=0);new_payable_cents=Column(Integer,default=0);delta_cents=Column(Integer,default=0)
 old_gross_cents=Column(Integer,default=0);new_gross_cents=Column(Integer,default=0)
 reason=Column(String(300),default='');status=Column(String(20),default='pending');booked_settlement_id=Column(Integer,nullable=True,index=True)
 created_by=Column(String(100),default='');created=Column(String(40),default='')
 __table_args__=(Index('ix_adjustments_target','route','target_month','status'),)
class RouteLock(Base):
 """MySQL 行锁锚点：同一线路的结账与更正串行化，避免差额丢失。"""
 __tablename__='route_locks'
 route=Column(String(60),primary_key=True)
