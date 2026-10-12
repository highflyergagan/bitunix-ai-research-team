import os, requests, math, datetime
BASE='https://fapi.bitunix.com'
FAVORITES=['BTCUSDT','ETHUSDT','QNTUSDT','BRUSDT']
DEPARTMENTS={
 'Market News Research':['Research Analyst','Sentiment Analyst','Market Movement Analyst','News Credibility Analyst','Market Impact Analyst'],
 'Crypto Fundamental Research':['Fundamental Analyst','Tokenomics Analyst','Unlock Analyst','Exchange Listing Analyst'],
 'Industry and Geopolitical Research':['Industry Analyst','Macro Analyst','Geopolitical Risk Analyst','Regulatory Analyst'],
 'Market Activity and Ownership Research':['Volume Analyst','Whale Analyst','Open Interest Analyst','Funding Analyst','Liquidation Analyst'],
 'Independent Research Review':['Bull Case Analyst','Bear Case Analyst','Validation Analyst','Risk Manager','Portfolio Manager']}
def get_json(path,params=None):
 r=requests.get(BASE+path,params=params,timeout=12);r.raise_for_status();j=r.json()
 if j.get('code') not in (0,'0'):raise RuntimeError(str(j.get('msg')))
 return j.get('data') or []
def tickers():return get_json('/api/v1/futures/market/tickers')
def candles(symbol,interval='1h'):
 data=get_json('/api/v1/futures/market/kline',{'symbol':symbol,'interval':interval,'limit':100})
 return sorted(data,key=lambda x:int(x['time']))
def analyze(symbol,data,external=None):
 external=external or {}
 closes=[float(x['close']) for x in data]; vols=[float(x.get('quoteVol') or 0) for x in data]
 if len(closes)<30 or min(closes)<=0:raise ValueError('Need 30 valid candles')
 last=closes[-1]; ma20=sum(closes[-20:])/20; ma30=sum(closes[-30:])/30
 momentum=last/closes[-6]-1
 change=[closes[i]-closes[i-1] for i in range(1,len(closes))][-14:]
 gains=sum(max(x,0) for x in change)/14; losses=sum(max(-x,0) for x in change)/14
 rsi=100 if losses==0 else 100-100/(1+gains/losses)
 daily_vol=math.sqrt(sum((closes[i]/closes[i-1]-1)**2 for i in range(-19,0))/19)
 score=(1 if last>ma20 else -1)+(1 if ma20>ma30 else -1)+(1 if momentum>0 else -1)
 bias='LONG' if score>=2 and rsi<75 else 'SHORT' if score<=-2 and rsi>25 else 'NO TRADE'
 # Independent validation veto: missing external data prevents any paper order
 reasons=[]
 for k in ('news','listings','unlocks','whales'):
  if not external.get(k,{}).get('verified',False):reasons.append(k+' feed unverified')
 if reasons:bias='NO TRADE'
 risk=min(.05,max(.01,daily_vol*math.sqrt(24)))
 return {'symbol':symbol,'price':last,'ma20':round(ma20,6),'ma30':round(ma30,6),'rsi':round(rsi,1),'momentum_5h':round(momentum,4),'bias':bias,'scenarios':{'bear':round(last*(1-2*risk),6),'base':round(last,6),'bull':round(last*(1+2*risk),6)},'scenario_note':'Heuristic ranges, not probabilities or predictive confidence','veto_reasons':reasons,'research':{d:[{'role':role,'status':'data-limited' if d!='Independent Research Review' else 'rule-based'} for role in roles] for d,roles in DEPARTMENTS.items()}}
def rank(rows):
 valid=[]
 for t in rows:
  try:
   p=float(t.get('lastPrice') or t.get('markPrice') or 0);v=float(t.get('quoteVol') or 0)
   if p>0 and v>=0:valid.append({'symbol':t['symbol'],'price':p,'quote_volume_24h':v})
  except (TypeError,ValueError,KeyError):pass
 return sorted(valid,key=lambda x:x['quote_volume_24h'],reverse=True)
def paper_trade(balance,entry,stop,risk_fraction=.005):
 if balance<=0 or entry<=0 or stop<=0 or entry==stop or not 0<risk_fraction<=.01:raise ValueError('Invalid trade')
 risk=balance*risk_fraction;units=risk/abs(entry-stop)
 return {'units':round(units,6),'max_planned_loss_before_fees_slippage':round(risk,2),'notional':round(units*entry,2),'mode':'SIMULATION ONLY'}
