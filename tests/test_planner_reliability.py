import json
import pytest
from agent_core.llm import BackendError, LLMResponse
from agent_core.models import Job, JobStatus
from planner_agent.decision import InvalidDecision, parse_decision
from planner_agent.planner import PlannerAgent

def valid_step():
    return {"decision":"create_step","job_status":"running","reasoning_summary":"next safe item",
            "step":{"title":"Add endpoint","objective":"Add one tested endpoint","rationale":"roadmap evidence",
                    "acceptance_criteria":["Endpoint behavior is tested"],"constraints":["Preserve behavior"],
                    "suggested_files":["api.py"],"dependencies":[],"assigned_agent":"coder-agent"},
            "evidence":[],"human_question":None,"blocker":None}

class ResponseBackend:
    def __init__(self, text=None, fail=False, name='ollama'):
        self.text,self.fail,self.name,self.model=text,fail,name,f'{name}-model'
    def complete(self,messages):
        if self.fail:raise BackendError(f'{self.name} unavailable')
        return LLMResponse(self.text,self.model,self.name,.01,{})
class SequenceRouter:
    def __init__(self,backends):self.backends=backends;self.attempts=[]
    def choose(self,attempt,needs_strong_model=False):self.attempts.append(attempt);return self.backends[min(len(self.attempts)-1,len(self.backends)-1)]
class Store:
    def __init__(self):self.job=Job(1,'goal','/repo','worker/x',JobStatus.PENDING);self.deferred=None;self.applied=None
    def claim_job(self,*a):self.job.status=JobStatus.PLANNING;return self.job
    def context(self,*a):return self.job,[],[]
    def heartbeat(self,*a):return True
    def defer(self,job,worker,kind,detail,retry_seconds=20):self.deferred=kind;self.job.status=JobStatus.RUNNING
    def apply_decision(self,job,worker,decision):self.applied=decision;self.job.status=JobStatus.RUNNING
class Inspector:
    def inspect(self,*a):return {'branch_matches':True,'actual_branch':'worker/x','documents':{},'repository_files':[],'git_status':[],'recent_history':[],'head':'abc'}

def test_schema_repair_then_fourth_attempt_escalation_succeeds():
    invalid=ResponseBackend('{}'); cloud=ResponseBackend(json.dumps(valid_step()),name='openrouter')
    store=Store();router=SequenceRouter([invalid,invalid,invalid,cloud])
    decision=PlannerAgent(store,router,Inspector(),'planner',decision_retries=3,escalation_attempt=4).plan_once()
    assert decision.decision=='create_step' and router.attempts==[1,2,3,4] and store.deferred is None

def test_exhausted_invalid_output_remains_recoverable():
    store=Store();router=SequenceRouter([ResponseBackend('{}')])
    decision=PlannerAgent(store,router,Inspector(),'planner',decision_retries=1).plan_once()
    assert decision.job_status=='running' and store.deferred=='invalid_model_output'

def test_backend_outage_remains_recoverable():
    store=Store();router=SequenceRouter([ResponseBackend(fail=True)])
    PlannerAgent(store,router,Inspector(),'planner',decision_retries=1).plan_once()
    assert store.deferred=='inference_unavailable'

def test_inappropriate_needs_human_is_rejected_but_hard_gate_is_valid():
    with pytest.raises(InvalidDecision):
        parse_decision(json.dumps({'decision':'needs_human','job_status':'needs_human','reasoning_summary':'unsure','human_question':'Which file should I inspect?'}))
    value=parse_decision(json.dumps({'decision':'needs_human','job_status':'needs_human','reasoning_summary':'Production migration is irreversible','human_question':'Approve destructive production migration?'}))
    assert value.decision=='needs_human'
