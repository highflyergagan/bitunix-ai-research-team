from .core import DEPARTMENTS,analyze
# Actual LangGraph orchestration. Nodes are deterministic; no simulated LLM claims.
def make_graph():
 from langgraph.graph import StateGraph,START,END
 from typing import TypedDict
 class State(TypedDict):
  symbol:str
  candles:list
  external:dict
  reports:dict
  result:dict
 def department(name):
  def node(state):
   reports=dict(state.get('reports',{}));reports[name]={'roles':DEPARTMENTS[name],'status':'data-limited','verified_feeds':list(k for k,v in state.get('external',{}).items() if v.get('verified'))}
   return {'reports':reports}
  return node
 def chief(state):return {'result':analyze(state['symbol'],state['candles'],state.get('external',{}))}
 g=StateGraph(State)
 previous=START
 for idx,name in enumerate(DEPARTMENTS):
  node=f'department_{idx}';g.add_node(node,department(name));g.add_edge(previous,node);previous=node
 g.add_node('chief',chief);g.add_edge(previous,'chief');g.add_edge('chief',END)
 return g.compile()
