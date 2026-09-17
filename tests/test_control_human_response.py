from contextlib import contextmanager
import pytest
from control_center.store import ControlStore

class Connection:
    def __init__(self, status="needs_human"):
        self.job={"id":7,"status":status,"current_step":11};self.queries=[]
    def execute(self,sql,args=()):
        self.queries.append((sql,args))
        if "SELECT * FROM jobs" in sql:return Result(self.job)
        return Result(None)
class Result:
    def __init__(self,row):self.row=row
    def fetchone(self):return self.row
class Workflow:
    def __init__(self,connection):self.connection=connection;self.events=[]
    @contextmanager
    def connect(self):yield self.connection
    def _event(self,_connection,job,step,event,payload,agent=None):self.events.append((job,step,event,payload,agent))

def subject(status="needs_human"):
    value=ControlStore.__new__(ControlStore);value.workflow=Workflow(Connection(status));return value

def test_human_answer_is_durable_and_resumes_same_workflow():
    store=subject();store.answer(7,"Preserve aliases and continue safely.")
    sql=" ".join(q[0] for q in store.workflow.connection.queries)
    assert "human_notes" in sql and "changes_requested" in sql
    assert store.workflow.events==[(7,11,"human_response_received",{"answer":"Preserve aliases and continue safely."},"control-center")]

def test_human_answer_is_rejected_when_job_is_not_waiting():
    with pytest.raises(ValueError):subject("running").answer(7,"Unexpected answer")
