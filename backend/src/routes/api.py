import os,json
import redis
from fastapi import APIRouter,Depends,HTTPException,Response
from sqlalchemy.exc import IntegrityError
from src.config.database import db
from src.models.entities import User,Shipment,Sample,Study,Recommendation,Audit,Contract,Settlement,Adjustment
from src.schemas.business import Login,ShipmentInput,SampleInput,StudyInput,RecommendationInput,ContractInput,SettleInput
from src.middlewares.auth import current,editor,admin
from src.utils.security import verify,token
from src.services.business import audit,dump
from src.services import claims
from src.controllers.business import ensure_shipment,ensure_study,study_rows
router=APIRouter()
@router.post('/auth/login')
def login(data:Login,s=Depends(db)):
 u=s.query(User).filter_by(email=data.email).first()
 if not u or not verify(data.password,u.password):raise HTTPException(401,'账号或密码错误')
 return {'token':token(u),'user':dump(u)}
@router.get('/me')
def me(u=Depends(current)):return dump(u)
@router.get('/shipments')
def ships(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Shipment).all()]
@router.post('/shipments')
def ship(data:ShipmentInput,u=Depends(editor),s=Depends(db)):
 r=Shipment(**data.model_dump());s.add(r)
 try:s.commit()
 except IntegrityError:s.rollback();raise HTTPException(409,'班次编号重复')
 audit(s,u,'创建班次 '+r.code);return dump(r)
@router.get('/samples')
def samples(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Sample).all()]
@router.post('/samples')
def sample(data:SampleInput,u=Depends(editor),s=Depends(db)):
 ensure_shipment(s,data.shipment_id);r=Sample(**data.model_dump());s.add(r)
 try:s.flush()
 except IntegrityError:s.rollback();raise HTTPException(409,'样本编号重复')
 claims.refresh_routes_for_sample_change(s,None,s.get(Shipment,data.shipment_id).route,u.email,commit=True)
 try:s.commit()
 except IntegrityError:s.rollback();raise HTTPException(409,'样本编号重复')
 audit(s,u,'录入样本 '+r.code);return dump(r)
@router.get('/studies')
def studies(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Study).order_by(Study.id.desc()).all()]
@router.post('/studies')
def study(data:StudyInput,u=Depends(editor),s=Depends(db)):
 rows=study_rows(s,data.zone);r=Study(**data.model_dump(),result={'snapshot':rows});s.add(r);s.commit()
 try:redis.Redis.from_url(os.environ['REDIS_URL']).rpush('studies',r.id)
 except redis.RedisError:
  r.state='failed';r.error='队列暂不可用，请重新创建研究';s.commit();raise HTTPException(503,r.error)
 audit(s,u,'提交研究 '+str(r.id));return dump(r)
@router.get('/studies/{i}')
def detail(i:int,u=Depends(current),s=Depends(db)):return dump(ensure_study(s,i))
@router.get('/studies/{i}/export')
def export(i:int,u=Depends(current),s=Depends(db)):
 r=ensure_study(s,i)
 if r.state!='done':raise HTTPException(409,'研究未成功完成')
 return Response(json.dumps(dump(r),ensure_ascii=False,indent=2),media_type='application/json',headers={'Content-Disposition':f'attachment; filename="coldchain-link-{i}.json"'})
@router.get('/recommendations')
def recs(u=Depends(current),s=Depends(db)):return [dump(r) for r in s.query(Recommendation).order_by(Recommendation.id.desc()).all()]
@router.post('/recommendations')
def rec(data:RecommendationInput,u=Depends(editor),s=Depends(db)):
 if ensure_study(s,data.study_id).state!='done':raise HTTPException(409,'仅已完成研究可以起草建议')
 r=Recommendation(**data.model_dump());s.add(r);s.commit();audit(s,u,'起草建议 '+str(r.id));return dump(r)
@router.post('/recommendations/{i}/approve')
def approve(i:int,u=Depends(admin),s=Depends(db)):
 r=s.get(Recommendation,i)
 if not r:raise HTTPException(404,'建议不存在')
 r.status='approved';s.commit();audit(s,u,'签核建议 '+str(i));return dump(r)
@router.get('/audits')
def audits(u=Depends(admin),s=Depends(db)):return [dump(r) for r in s.query(Audit).order_by(Audit.id.desc()).limit(100)]

@router.put('/shipments/{i}')
def edit_ship(i:int,data:ShipmentInput,u=Depends(editor),s=Depends(db)):
 row=s.get(Shipment,i)
 if not row:raise HTTPException(404,'班次不存在')
 old_route=row.route
 for k,v in data.model_dump().items():setattr(row,k,v)
 try:s.flush()
 except IntegrityError:s.rollback();raise HTTPException(409,'班次编号重复')
 # 线路调拨会把该班次样本整体移到另一条线路，两条线路的已结更正都要重算
 if old_route!=row.route:
  claims.refresh_routes_for_sample_change(s,old_route,row.route,u.email,commit=True)
 try:s.commit()
 except IntegrityError:s.rollback();raise HTTPException(409,'班次编号重复')
 audit(s,u,'修改班次 '+str(i));return dump(row)
@router.put('/samples/{i}')
def edit_sample(i:int,data:SampleInput,u=Depends(editor),s=Depends(db)):
 row=s.get(Sample,i)
 if not row:raise HTTPException(404,'样本不存在')
 old_route=s.get(Shipment,row.shipment_id)
 old_route=old_route.route if old_route else None
 ensure_shipment(s,data.shipment_id)
 for k,v in data.model_dump().items():setattr(row,k,v)
 try:s.flush()
 except IntegrityError:s.rollback();raise HTTPException(409,'样本编号重复')
 new_route=s.get(Shipment,data.shipment_id).route
 claims.refresh_routes_for_sample_change(s,old_route,new_route,u.email,commit=True)
 try:s.commit()
 except IntegrityError:s.rollback();raise HTTPException(409,'样本编号重复')
 audit(s,u,'修改样本 '+str(i));return dump(row)

# ---------- 月度索赔：合同 / 对账 / 结账 / 更正 ----------

@router.get('/contracts')
def contracts(u=Depends(current),s=Depends(db)):
 return [claims.contract_dict(r) for r in s.query(Contract).order_by(Contract.route,Contract.effective_month.desc()).all()]

def _validate_contract(data):
 if not data.route.strip():raise HTTPException(422,'线路不能为空')
 if data.payout_bp>10000:raise HTTPException(422,'赔付比例不能超过 100%')

@router.post('/contracts')
def create_contract(data:ContractInput,u=Depends(admin),s=Depends(db)):
 _validate_contract(data)
 r=Contract(**data.model_dump(),created=claims.now(),updated=claims.now());s.add(r)
 try:s.commit()
 except IntegrityError:s.rollback();raise HTTPException(409,'该线路在此生效月已有合同')
 audit(s,u,f'新增合同 {r.route} {r.effective_month}');return claims.contract_dict(r)

@router.put('/contracts/{i}')
def update_contract(i:int,data:ContractInput,u=Depends(admin),s=Depends(db)):
 _validate_contract(data)
 r=s.get(Contract,i)
 if not r:raise HTTPException(404,'合同不存在')
 settled=(s.query(Settlement).filter(Settlement.contract_id==i,Settlement.state.in_(claims.CLOSED)).count())
 if settled:raise HTTPException(409,'该合同已用于已结账月份，请新增生效合同版本而不是修改历史合同')
 for k,v in data.model_dump().items():setattr(r,k,v)
 r.updated=claims.now()
 try:s.commit()
 except IntegrityError:s.rollback();raise HTTPException(409,'该线路在此生效月已有合同')
 audit(s,u,f'修改合同 {r.route} {r.effective_month}');return claims.contract_dict(r)

@router.get('/claims')
def claims_board(month:str|None=None,u=Depends(current),s=Depends(db)):
 try:return claims.overview(s,month)
 except ValueError as e:raise HTTPException(422,str(e))

@router.post('/claims/settle')
def settle_month(data:SettleInput,u=Depends(admin),s=Depends(db)):
 try:
  st,replayed=claims.settle(s,data.route,data.month,u.email,data.settle_key)
 except LookupError as e:raise HTTPException(409,str(e))
 except ValueError as e:raise HTTPException(422,str(e))
 audit(s,u,f'结账 {st.route} {st.month}'+('（幂等重放）' if replayed else ''))
 return {'settlement':claims.settlement_dict(s,st),'replayed':replayed}

@router.post('/claims/resume')
def resume(u=Depends(admin),s=Depends(db)):
 done=claims.resume_incomplete(s)
 if done:audit(s,u,'续跑未完成结账 '+','.join(map(str,done)))
 return {'resumed':done}

@router.get('/settlements')
def settlements(route:str|None=None,month:str|None=None,u=Depends(current),s=Depends(db)):
 q=s.query(Settlement)
 if route:q=q.filter(Settlement.route==route)
 if month:q=q.filter(Settlement.month==month)
 return [{'id':r.id,'route':r.route,'month':r.month,'state':r.state,'payable_cents':r.payable_cents,
          'base_payable_cents':r.base_payable_cents,'adjustment_cents':r.adjustment_cents,
          'sample_count':r.sample_count,'settled_at':r.settled_at} for r in q.order_by(Settlement.month.desc(),Settlement.route).all()]

@router.get('/settlements/{i}')
def settlement_detail(i:int,u=Depends(current),s=Depends(db)):
 st=s.get(Settlement,i)
 if not st:raise HTTPException(404,'结算单不存在')
 return claims.settlement_dict(s,st)

@router.get('/adjustments')
def adjustments(u=Depends(current),s=Depends(db)):
 rows=s.query(Adjustment).order_by(Adjustment.target_month,Adjustment.route,Adjustment.id).all()
 return [{'id':r.id,'route':r.route,'target_month':r.target_month,'sample_id':r.sample_id,
          'sample_code':r.sample_code,'source_settlement_id':r.source_settlement_id,
          'source_month':r.source_month,'old_payable_cents':r.old_payable_cents,
          'new_payable_cents':r.new_payable_cents,'delta_cents':r.delta_cents,
          'old_gross_cents':r.old_gross_cents,'new_gross_cents':r.new_gross_cents,
          'reason':r.reason,'status':r.status,'booked_settlement_id':r.booked_settlement_id,
          'created_by':r.created_by,'created':r.created} for r in rows]
