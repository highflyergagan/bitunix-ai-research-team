import pytest
from app.core import analyze,paper_trade,rank,DEPARTMENTS

def sample():return [{'time':i,'close':str(100+i*.1),'quoteVol':'100'} for i in range(40)]
def test_five_departments():assert len(DEPARTMENTS)==5 and sum(map(len,DEPARTMENTS.values()))==23
def test_no_trade_without_verified_feeds():
 x=analyze('BTCUSDT',sample());assert x['bias']=='NO TRADE' and len(x['veto_reasons'])==4
def test_forecasts_ordered():
 x=analyze('BTCUSDT',sample());assert x['scenarios']['bear']<x['scenarios']['base']<x['scenarios']['bull']
def test_risk():assert paper_trade(1000,100,98)['max_planned_loss_before_fees_slippage']==5
def test_invalid_risk():
 with pytest.raises(ValueError):paper_trade(1000,100,100)
def test_rank():assert rank([{'symbol':'A','lastPrice':'2','quoteVol':'3'},{'symbol':'B','lastPrice':'1','quoteVol':'5'}])[0]['symbol']=='B'
def test_graph():
 pytest.importorskip('langgraph')
 from app.graph import make_graph
 x=make_graph().invoke({'symbol':'BTCUSDT','candles':sample(),'external':{},'reports':{},'result':{}})
 assert len(x['reports'])==5 and x['result']['bias']=='NO TRADE'
