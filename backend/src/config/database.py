import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
url=os.environ['DATABASE_URL']
kwargs={'pool_pre_ping':True}
if url.startswith('sqlite'):
 # 本地/测试使用 SQLite 时允许跨线程连接，并放宽同库写锁等待
 kwargs['connect_args']={'check_same_thread':False,'timeout':30}
else:
 # READ COMMITTED：拿到线路行锁后能读到其他事务刚提交的样本与结算状态，
 # 避免结账与更正并发时因快照过期漏掉差额
 kwargs['isolation_level']='READ COMMITTED'
engine=create_engine(url,**kwargs)
Session=sessionmaker(bind=engine,expire_on_commit=False)
Base=declarative_base()
def db():
 with Session() as s: yield s
