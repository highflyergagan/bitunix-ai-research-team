import os,html
from fastapi import FastAPI,HTTPException,Header
from fastapi.responses import HTMLResponse
from .core import FAVORITES,DEPARTMENTS,tickers,rank,candles,paper_trade
from .graph import make_graph
app=FastAPI(title='Bitunix Research Desk — PAPER ONLY')
TOKEN=os.getenv('DASHBOARD_TOKEN','')
def auth(authorization):
 if not TOKEN or authorization!='Bearer '+TOKEN:raise HTTPException(401,'Token required')
@app.get('/',response_class=HTMLResponse)
def home():
 return '''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Research Desk</title><style>body{font:16px system-ui;background:#0d1421;color:#eaf0f9;max-width:760px;margin:auto;padding:22px}button,input{padding:12px;border-radius:8px;margin:4px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#182436;padding:16px;border-radius:10px} .muted{color:#9caec8}</style><h2>Bitunix AI Research Desk</h2><p class="muted">PAPER ONLY · No Bitunix credentials · Unverified events block trades</p><input id="token" type="password" placeholder="Dashboard token"><button onclick="go('favorites')">Favorites</button><button onclick="go('scan')">Market scan</button><input id="symbol" value="BTCUSDT" placeholder="Symbol"><button onclick="go('research/'+encodeURIComponent(document.getElementById('symbol').value))">Research</button><pre id="out">Enter token and select a report.</pre><script>async function go(path){let r=await fetch('/api/'+path,{headers:{Authorization:'Bearer '+document.getElementById('token').value}});document.getElementById('out').textContent=JSON.stringify(await r.json(),null,2)}</script>'''
@app.get('/health')
def health():return {'status':'ok','trading':'disabled'}
@app.get('/api/favorites')
def favorites(authorization:str|None=Header(None)):
 auth(authorization);return {'favorites':FAVORITES,'departments':DEPARTMENTS}
@app.get('/api/scan')
def scan(authorization:str|None=Header(None)):
 auth(authorization)
 try:return {'symbols':rank(tickers()),'source':'Bitunix public futures tickers'}
 except Exception as e:raise HTTPException(503,str(e))
@app.get('/api/research/{symbol}')
def research(symbol:str,authorization:str|None=Header(None)):
 auth(authorization);symbol=symbol.upper()
 if not symbol.isalnum() or len(symbol)>24:raise HTTPException(400,'Invalid symbol')
 try:return make_graph().invoke({'symbol':symbol,'candles':candles(symbol),'external':{},'reports':{},'result':{}})['result']
 except Exception as e:raise HTTPException(503,str(e))
@app.get('/api/paper-example')
def example(authorization:str|None=Header(None)):
 auth(authorization);return paper_trade(1000,100,98)
