import json,subprocess
from pathlib import Path
import pytest
from agent_core.llm import LLMResponse,Router
from reviewer_agent.decision import InvalidReview,parse_review,stable_issue_key
from reviewer_agent.evidence import EvidenceCollector
from reviewer_agent.reviewer import ReviewerAgent

CRITERIA=['Behavior is correct','Relevant tests pass']
def approval():return {'verdict':'approved','summary':'Evidence satisfies the step','blocking_issues':[],
'non_blocking_suggestions':[],'acceptance_criteria':[{'criterion':c,'status':'satisfied','evidence':'passing test and diff'} for c in CRITERIA],
'risk':'low','confidence':'high','recommended_next_state':'verification'}
def rejection():return {'verdict':'changes_requested','summary':'Validation missing','blocking_issues':[{'severity':'high','category':'correctness','file':'api.py','line':4,'problem':'zero accepted','evidence':'code permits zero','requested_change':'Reject zero'}],
'non_blocking_suggestions':[],'acceptance_criteria':[{'criterion':CRITERIA[0],'status':'not_satisfied','evidence':'zero accepted'},{'criterion':CRITERIA[1],'status':'satisfied','evidence':'tests pass'}],
'risk':'medium','confidence':'high','recommended_next_state':'coder_revision'}

def test_01_valid_candidate_approved():assert parse_review(json.dumps(approval()),CRITERIA).verdict=='approved'
def test_02_failed_criterion_changes_requested():assert parse_review(json.dumps(rejection()),CRITERIA).verdict=='changes_requested'
def test_03_approval_cannot_contain_failed_criterion():
 p=approval();p['acceptance_criteria'][0]['status']='not_satisfied'
 with pytest.raises(InvalidReview):parse_review(json.dumps(p),CRITERIA)
def test_04_invalid_json_never_approves():
 with pytest.raises(InvalidReview):parse_review('approved',CRITERIA)
def test_05_all_criteria_required():
 p=approval();p['acceptance_criteria'].pop()
 with pytest.raises(InvalidReview):parse_review(json.dumps(p),CRITERIA)
def test_06_each_criterion_needs_evidence():
 p=approval();p['acceptance_criteria'][0]['evidence']=''
 with pytest.raises(InvalidReview):parse_review(json.dumps(p),CRITERIA)
def test_07_rejection_needs_issue():
 p=rejection();p['blocking_issues']=[]
 with pytest.raises(InvalidReview):parse_review(json.dumps(p),CRITERIA)
def test_08_issue_is_actionable():
 p=rejection();del p['blocking_issues'][0]['requested_change']
 with pytest.raises(InvalidReview):parse_review(json.dumps(p),CRITERIA)
def test_09_stable_ids_survive_attempts():assert stable_issue_key(rejection()['blocking_issues'][0])==stable_issue_key(rejection()['blocking_issues'][0])
def test_10_approval_routes_verification():assert parse_review(json.dumps(approval()),CRITERIA).recommended_next_state=='verification'
def test_11_approval_cannot_route_merge():
 p=approval();p['recommended_next_state']='merge'
 with pytest.raises(InvalidReview):parse_review(json.dumps(p),CRITERIA)
def test_12_rejection_routes_same_step_revision():assert parse_review(json.dumps(rejection()),CRITERIA).recommended_next_state=='coder_revision'
def test_13_needs_human_requires_question():
 p=approval();p.update(verdict='needs_human',recommended_next_state='blocked')
 with pytest.raises(InvalidReview):parse_review(json.dumps(p),CRITERIA)
def test_14_needs_human_is_not_a_reviewer_verdict():
    p = approval()
    p.update(
        verdict="needs_human",
        recommended_next_state="blocked",
        human_question="Approve destructive migration?",
    )
    with pytest.raises(InvalidReview):
        parse_review(json.dumps(p), CRITERIA)


def test_15_invalid_severity_rejected():
 p=rejection();p['blocking_issues'][0]['severity']='huge'
 with pytest.raises(InvalidReview):parse_review(json.dumps(p),CRITERIA)

def repo(tmp_path):
 r=tmp_path/'repo';r.mkdir();subprocess.run(['git','init','-q','-b','worker/x'],cwd=r,check=True)
 (r/'app.py').write_text('value = 1\n');subprocess.run(['git','add','app.py'],cwd=r,check=True)
 subprocess.run(['git','-c','user.name=T','-c','user.email=t@e','commit','-qm','start'],cwd=r,check=True)
 start=subprocess.run(['git','rev-parse','HEAD'],cwd=r,text=True,capture_output=True).stdout.strip();return r,start
def test_16_collector_is_read_only(tmp_path):
 r,s=repo(tmp_path);(r/'app.py').write_text('value = 2\n');c=EvidenceCollector([str(tmp_path)]);c.collect(str(r),s,['app.py'],[])
 assert (r/'app.py').read_text()=='value = 2\n'
def test_17_no_commit_push_merge_interface(tmp_path):
 c=EvidenceCollector([str(tmp_path)]);assert not any(hasattr(c,x) for x in ('commit','push','merge','write'))
def test_18_secret_detected(tmp_path):
 r,s=repo(tmp_path);token="ghp_"+"abcdefghijklmnopqrstuvwxyz";(r/'app.py').write_text(f"token='{token}'\n")
 assert EvidenceCollector([str(tmp_path)]).collect(str(r),s,['app.py'],[])['secret_hits']
def test_19_dependency_change_detected(tmp_path):
 r,s=repo(tmp_path);(r/'pyproject.toml').write_text('[project]\n')
 assert EvidenceCollector([str(tmp_path)]).collect(str(r),s,['pyproject.toml'],[])['risk_flags']['dependency_change']
def test_20_financial_change_heightened(tmp_path):
 r,s=repo(tmp_path);(r/'app.py').write_text('from decimal import Decimal\n')
 assert EvidenceCollector([str(tmp_path)]).collect(str(r),s,['app.py'],[])['risk_flags']['financial_change']
def test_21_migration_detected(tmp_path):
 r,s=repo(tmp_path);(r/'migration_1.sql').write_text('ALTER TABLE x;\n')
 assert EvidenceCollector([str(tmp_path)]).collect(str(r),s,['migration_1.sql'],[])['risk_flags']['migration_change']
def test_22_test_weakening_flagged(tmp_path):
 r,s=repo(tmp_path);(r/'app.py').write_text('value = 1\nassert value\n');subprocess.run(['git','add','app.py'],cwd=r,check=True);subprocess.run(['git','-c','user.name=T','-c','user.email=t@e','commit','-qm','test'],cwd=r,check=True);s=subprocess.run(['git','rev-parse','HEAD'],cwd=r,text=True,capture_output=True).stdout.strip();(r/'app.py').write_text('value = 1\n')
 assert EvidenceCollector([str(tmp_path)]).collect(str(r),s,['app.py'],[])['risk_flags']['tests_deleted']
def test_23_preexisting_unlisted_change_ignored(tmp_path):
 r,s=repo(tmp_path);(r/'human.txt').write_text('mine');(r/'app.py').write_text('value = 2\n');e=EvidenceCollector([str(tmp_path)]).collect(str(r),s,['app.py'],[])
 assert 'human.txt' not in e['diff']
def test_24_diff_check_is_recorded(tmp_path):
 r,s=repo(tmp_path);(r/'app.py').write_text('value = 2\n');e=EvidenceCollector([str(tmp_path)]).collect(str(r),s,['app.py'],[])
 assert e['checks']['diff_check']['exit_code']==0
def test_25_arbitrary_verification_is_not_run(tmp_path):
 r,s=repo(tmp_path);e=EvidenceCollector([str(tmp_path)]).collect(str(r),s,['app.py'],[['sh','-c','false']])
 assert e['checks']['verification']==[]
def test_26_large_diff_escalation_flag(tmp_path):
 r,s=repo(tmp_path);(r/'app.py').write_text('x'*200);e=EvidenceCollector([str(tmp_path)],20).collect(str(r),s,['app.py'],[])
 assert e['risk_flags']['large_diff']
def test_27_diff_hash_auditable(tmp_path):
 r,s=repo(tmp_path);(r/'app.py').write_text('value=3\n');assert len(EvidenceCollector([str(tmp_path)]).collect(str(r),s,['app.py'],[])['diff_sha256'])==64
def test_28_router_escalates_repeated_review():
 local,cloud=object(),object();assert Router(local,cloud,3).choose(3) is cloud
def test_29_low_risk_uses_local():
 local,cloud=object(),object();assert Router(local,cloud,3).choose(1) is local
def test_30_new_genuine_issue_gets_new_id():
 a=rejection()['blocking_issues'][0];b={**a,'problem':'another defect'};assert stable_issue_key(a)!=stable_issue_key(b)
