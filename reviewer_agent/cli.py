from __future__ import annotations
import argparse,json,os,sys
from dataclasses import asdict
from agent_core.llm import OllamaBackend,OpenRouterBackend,Router
from planner_agent.store import PlannerStore
from .evidence import EvidenceCollector
from .reviewer import ReviewerAgent
from .store import ReviewerStore
from .worker import ReviewerWorker

def build():
    store=ReviewerStore(os.environ['DATABASE_URL']);roots=os.environ['REVIEWER_ALLOWED_REPOSITORIES'].split(os.pathsep)
    local=OllamaBackend(os.getenv('OLLAMA_URL','http://localhost:11434'),os.getenv('REVIEWER_MODEL','qwen2.5-coder:14b'))
    cloud=OpenRouterBackend('https://openrouter.ai/api/v1',os.getenv('OPENROUTER_REVIEWER_MODEL','anthropic/claude-sonnet-4'),os.environ['OPENROUTER_API_KEY']) if os.getenv('OPENROUTER_API_KEY') else None
    return ReviewerAgent(store,Router(local,cloud,3),EvidenceCollector(roots,int(os.getenv('REVIEWER_MAX_DIFF_BYTES','300000'))),
                         os.getenv('REVIEWER_WORKER_ID','reviewer-1'),int(os.getenv('REVIEWER_LEASE_SECONDS','300')),int(os.getenv('REVIEWER_MAX_ATTEMPTS','5')))
def main(argv=None):
    p=argparse.ArgumentParser(prog='reviewer-agent');s=p.add_subparsers(dest='command',required=True)
    for x in ('init-db','run','once'):s.add_parser(x)
    for x in ('inspect','inspect-step','issues'):
        q=s.add_parser(x);q.add_argument('id',type=int)
    a=p.parse_args(argv);reviewer=build()
    if a.command=='init-db':PlannerStore(os.environ['DATABASE_URL']).migrate()
    elif a.command=='run':ReviewerWorker(reviewer).run_forever()
    elif a.command=='once':
        d=reviewer.review_once();print(json.dumps(asdict(d) if d else {'verdict':'idle'},default=str))
    else:
        fn={'inspect':reviewer.store.inspect_review,'inspect-step':reviewer.store.inspect_step,'issues':reviewer.store.issues}[a.command]
        print(json.dumps(fn(a.id),default=str,indent=2))
    return 0
if __name__=='__main__':sys.exit(main())
