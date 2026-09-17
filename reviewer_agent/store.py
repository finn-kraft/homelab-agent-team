from __future__ import annotations
import json
from contextlib import contextmanager


class ReviewerStore:
    def __init__(self, database_url): self.database_url=database_url
    @contextmanager
    def connect(self):
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(self.database_url,row_factory=dict_row) as c: yield c

    def claim(self, worker_id, lease_seconds, step_id=None):
        with self.connect() as c:
            row=c.execute("""WITH candidate AS (SELECT s.id FROM steps s JOIN jobs j ON j.id=s.job_id
            WHERE s.status='review' AND j.status NOT IN ('paused','cancelled') AND
            (%s::BIGINT IS NULL OR s.id=%s) AND
            (s.review_lease_expires_at IS NULL OR s.review_lease_expires_at<now())
            ORDER BY s.id FOR UPDATE OF s SKIP LOCKED LIMIT 1), claimed AS (
            UPDATE steps s SET reviewer_worker_id=%s,review_lease_expires_at=now()+(%s*interval '1 second')
            FROM candidate WHERE s.id=candidate.id RETURNING s.*)
            SELECT claimed.*,j.goal,j.status AS job_status FROM claimed JOIN jobs j ON j.id=claimed.job_id""",
            (step_id,step_id,worker_id,lease_seconds)).fetchone()
            if not row:return None
            attempt=c.execute("SELECT COALESCE(MAX(review_attempt),0)+1 n FROM reviews WHERE step_id=%s",(row['id'],)).fetchone()['n']
            review=c.execute("""INSERT INTO reviews(job_id,step_id,review_attempt,reviewer_worker_id,lease_expires_at)
            VALUES(%s,%s,%s,%s,now()+(%s*interval '1 second')) RETURNING id""",
            (row['job_id'],row['id'],attempt,worker_id,lease_seconds)).fetchone()
            row=dict(row);row['review_id']=review['id'];row['review_attempt']=attempt
            self._event(c,row['job_id'],row['id'],'review_started',{'review_id':review['id'],'attempt':attempt})
            return row

    def heartbeat(self, review_id, worker_id, lease_seconds):
        with self.connect() as c:
            review = c.execute("""UPDATE reviews SET lease_expires_at=now()+(%s*interval '1 second')
            WHERE id=%s AND reviewer_worker_id=%s AND completed_at IS NULL
            RETURNING step_id""", (lease_seconds, review_id, worker_id)).fetchone()
            if not review:
                return False
            # The orchestrator's recovery query watches the step lease, while
            # the reviewer owns a separate row in reviews. Renew both in the
            # same transaction so a long evidence/model call cannot appear
            # abandoned to either side of the workflow.
            return c.execute("""UPDATE steps SET review_lease_expires_at=now()+(%s*interval '1 second')
            WHERE id=%s AND reviewer_worker_id=%s AND status='review'""",
                             (lease_seconds, review['step_id'], worker_id)).rowcount == 1

    def abandon(self, step_id, worker_id, detail, retry_seconds=20):
        """Release a failed review safely and make it retryable soon.

        A review can fail before ``complete`` persists a verdict (for example
        an evidence timeout or unavailable model). Marking the in-flight review
        row finished and clearing the step owner prevents the lease from
        stranding the job until its full expiry window.
        """
        with self.connect() as c:
            review = c.execute("""SELECT id,job_id FROM reviews
            WHERE step_id=%s AND reviewer_worker_id=%s AND completed_at IS NULL
            ORDER BY id DESC LIMIT 1 FOR UPDATE""", (step_id, worker_id)).fetchone()
            if not review:
                return False
            summary = f"Reviewer failed before producing a verdict: {str(detail)[:2000]}"
            c.execute("""UPDATE reviews SET verdict='blocked',summary=%s,
            lease_expires_at=NULL,completed_at=now() WHERE id=%s""",
                      (summary, review['id']))
            c.execute("""UPDATE steps SET reviewer_worker_id=NULL,
            review_lease_expires_at=now()+(%s*interval '1 second'),updated_at=now()
            WHERE id=%s AND reviewer_worker_id=%s""",
                      (retry_seconds, step_id, worker_id))
            c.execute("""UPDATE jobs SET status='running',current_phase='review',updated_at=now()
            WHERE id=%s AND status='reviewing'""", (review['job_id'],))
            self._event(c, review['job_id'], step_id, 'review_failed', {
                'review_id': review['id'], 'detail': str(detail)[:30_000],
                'retry_seconds': retry_seconds,
            })
            return True

    def commands(self,step_id,attempt=None):
        with self.connect() as c:
            if attempt is None:
                return list(c.execute("""SELECT * FROM command_runs
                WHERE step_id=%s AND source IN ('engineering-agent','coder-agent') ORDER BY id""",(step_id,)).fetchall())
            return list(c.execute("""SELECT * FROM command_runs
            WHERE step_id=%s AND source IN ('engineering-agent','coder-agent') AND attempt=%s ORDER BY id""",
            (step_id,attempt)).fetchall())

    def prior_issues(self,step_id):
        with self.connect() as c:return list(c.execute("SELECT * FROM review_issues WHERE step_id=%s ORDER BY id",(step_id,)).fetchall())

    def complete(self,item,worker_id,decision,meta,evidence):
        with self.connect() as c:
            review=c.execute("SELECT * FROM reviews WHERE id=%s FOR UPDATE",(item['review_id'],)).fetchone()
            if not review or review['reviewer_worker_id']!=worker_id or review['completed_at']:raise RuntimeError('review lease lost')
            verdict=decision.verdict
            step_status={'approved':'verification','changes_requested':'changes_requested','needs_human':'needs_human','blocked':'blocked'}[verdict]
            c.execute("""UPDATE reviews SET model=%s,provider=%s,verdict=%s,risk=%s,confidence=%s,summary=%s,
            acceptance_matrix=%s,deterministic_checks=%s,diff_sha256=%s,usage=%s,latency_seconds=%s,
            lease_expires_at=NULL,completed_at=now() WHERE id=%s""",
            (meta['model'],meta['provider'],verdict,decision.risk,decision.confidence,decision.summary,
             json.dumps(decision.acceptance_criteria),json.dumps(evidence['checks']),evidence['diff_sha256'],
             json.dumps(meta.get('usage')),meta.get('latency'),item['review_id']))
            prior={x['stable_issue_key']:x for x in self.prior_issues(item['id'])}
            current=set()
            for issue in decision.blocking_issues:
                key=issue['id'];current.add(key)
                c.execute("""INSERT INTO review_issues(review_id,step_id,stable_issue_key,severity,category,file,line,
                problem,evidence,requested_change,status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open')""",
                (item['review_id'],item['id'],key,issue['severity'],issue['category'],issue.get('file'),issue.get('line'),
                 issue['problem'],issue['evidence'],issue['requested_change']))
            for key in set(prior)-current:c.execute("UPDATE review_issues SET status='resolved' WHERE step_id=%s AND stable_issue_key=%s AND status='open'",(item['id'],key))
            c.execute("""UPDATE steps SET status=%s,reviewer_feedback=%s,reviewer_worker_id=NULL,
            review_lease_expires_at=NULL,updated_at=now() WHERE id=%s""",
            (step_status,json.dumps({'verdict':verdict,'issues':decision.blocking_issues}),item['id']))
            if verdict=='needs_human':
                c.execute("UPDATE jobs SET status='needs_human',updated_at=now() WHERE id=%s",(item['job_id'],))
                c.execute("""INSERT INTO human_queue(mission_id,job_id,step_id,kind,question,context)
                    SELECT p.mission_id,%s,%s,'review',%s,%s
                    FROM work_packages p WHERE p.step_id=%s
                    ON CONFLICT (job_id,step_id,kind) WHERE status='open' DO UPDATE SET
                      question=EXCLUDED.question,context=EXCLUDED.context,updated_at=now()""",
                    (item['job_id'], item['id'], getattr(decision, 'human_question', None) or decision.summary,
                     json.dumps({'summary': decision.summary, 'verdict': verdict}), item['id']))
            self._event(c,item['job_id'],item['id'],'review_approved' if verdict=='approved' else verdict,
                        {'review_id':item['review_id'],'verdict':verdict,'next':decision.recommended_next_state})

    def inspect_review(self,id):
        with self.connect() as c:return c.execute("SELECT * FROM reviews WHERE id=%s",(id,)).fetchone()
    def inspect_step(self,id):
        with self.connect() as c:return c.execute("SELECT * FROM steps WHERE id=%s",(id,)).fetchone()
    def issues(self,id):
        with self.connect() as c:return list(c.execute("SELECT * FROM review_issues WHERE step_id=%s ORDER BY id",(id,)).fetchall())
    @staticmethod
    def _event(c,job,step,event,payload):c.execute("INSERT INTO events(job_id,step_id,agent,event_type,structured_payload) VALUES(%s,%s,'reviewer-agent',%s,%s)",(job,step,event,json.dumps(payload)))
