from pydantic import BaseModel,Field
from typing import Optional,Literal
class Login(BaseModel):
 email:str;password:str
class ShipmentInput(BaseModel):
 code:str=Field(min_length=1,max_length=60);route:str=Field(min_length=1,max_length=60);zone:Literal['冷藏','冷冻','常温对照'];driver:str=Field(min_length=1,max_length=60)
class SampleInput(BaseModel):
 shipment_id:int;code:str=Field(min_length=1,max_length=60);exposure:Optional[float]=Field(default=None,ge=0,le=1440);excursions:Optional[int]=Field(default=None,ge=0,le=100);loss:Optional[float]=Field(default=None,ge=0,le=100)
 cargo_value_cents:Optional[int]=Field(default=None,ge=0,le=10**12);settle_month:Optional[str]=Field(default=None,pattern=r'^\d{4}-(0[1-9]|1[0-2])$')
class StudyInput(BaseModel):
 name:str=Field(min_length=1,max_length=100);zone:Literal['冷藏','冷冻','常温对照'];feature:Literal['exposure','excursions']='exposure'
class RecommendationInput(BaseModel):
 study_id:int;note:str=Field(min_length=10,max_length=2000);owner:str=Field(min_length=1,max_length=60)

class ContractInput(BaseModel):
 route:str=Field(min_length=1,max_length=60)
 effective_month:str=Field(pattern=r'^\d{4}-(0[1-9]|1[0-2])$')
 deductible_cents:int=Field(ge=0,le=10**12)
 payout_bp:int=Field(ge=0,le=10000)
 cap_cents:int=Field(ge=0,le=10**12)
 note:str=Field(default='',max_length=300)

class SettleInput(BaseModel):
 route:str=Field(min_length=1,max_length=60);month:str=Field(pattern=r'^\d{4}-(0[1-9]|1[0-2])$')
 settle_key:Optional[str]=Field(default=None,min_length=1,max_length=100)
