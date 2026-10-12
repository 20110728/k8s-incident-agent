"""Durable due dates; an interaction lane is occupied only after activation."""
from uuid import uuid4
from psycopg.types.json import Jsonb

from backend.app.persistence.runs import request_digest

WORKFLOW = "delayed-recheck-v1"
ACTIVE = ("queued", "running", "waiting_user", "waiting_approval", "retry_scheduled", "reconciling")


def latest_diagnosis(conn, incident_id):
    row = conn.execute("""SELECT run_id FROM incident_agent_app.runs WHERE incident_id=%s AND run_kind='diagnosis'
        ORDER BY created_at DESC,run_id DESC LIMIT 1""", (incident_id,)).fetchone()
    return row["run_id"] if row else None


def schedule(conn, key, incident_id, lease, window):
    if window.get("status") != "passed" or not window.get("target") or not window.get("finished_at"):
        return
    incident = conn.execute("SELECT * FROM incident_agent_app.incidents WHERE incident_id=%s FOR UPDATE", (incident_id,)).fetchone()
    result = window["last_result"] or {}
    snapshot = {"incident_id": incident_id, "phase": "verification_succeeded",
                "request": {k: incident[k] for k in ("namespace", "service_name", "description")},
                "service_profile": result.get("service_profile") or result.get("post_repair_profile") or {}}
    initial = {"status": "passed", "finished_at": window["finished_at"],
               "consecutive": window["consecutive"], "policy": window["policy"]}
    conn.execute("""INSERT INTO incident_agent_app.delayed_rechecks
        (delayed_id,observation_key,policy_version,incident_id,source_run_id,basis_run_id,event_revision,target,snapshot,initial_result,
         due_at,expires_at,next_attempt_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp()+interval '30 seconds',
                clock_timestamp()+interval '330 seconds',clock_timestamp()+interval '30 seconds')
        ON CONFLICT(observation_key,policy_version) DO NOTHING""",
        (str(uuid4()), key, window["policy"]["version"], incident_id, lease["run_id"] if lease else None,
         latest_diagnosis(conn, incident_id), incident["event_revision"], Jsonb(window["target"]), Jsonb(snapshot), Jsonb(initial)))


def stale(conn, row, incident):
    if incident["event_revision"] != row["event_revision"] or latest_diagnosis(conn, row["incident_id"]) != row["basis_run_id"]:
        return True
    if row["source_run_id"]:
        source = conn.execute("SELECT status,invalidated_at FROM incident_agent_app.runs WHERE run_id=%s", (row["source_run_id"],)).fetchone()
        if source["invalidated_at"] or source["status"] == "cancelled":
            return True
    return False


def terminal(conn, row, status, reason, result=None):
    conn.execute("""UPDATE incident_agent_app.delayed_rechecks SET status=%s,reason=%s,result=%s,
        finished_at=clock_timestamp() WHERE delayed_id=%s""", (status, reason, Jsonb(result) if result else None, row["delayed_id"]))


def activate_due(repo):
    # Read candidate IDs without locking; each incident is arbitrated separately
    # in the same order as commands, approvals and write dispatch.
    candidates = repo._read("""SELECT delayed_id,incident_id FROM incident_agent_app.delayed_rechecks
        WHERE status IN ('pending','running') AND next_attempt_at<=clock_timestamp()
        ORDER BY next_attempt_at,sequence LIMIT 20""", ())
    for candidate in candidates:
        with repo._connect() as conn:
            incident = conn.execute("SELECT * FROM incident_agent_app.incidents WHERE incident_id=%s FOR UPDATE", (candidate["incident_id"],)).fetchone()
            row = conn.execute("""SELECT *,expires_at<=clock_timestamp() AS expired FROM incident_agent_app.delayed_rechecks
                WHERE delayed_id=%s FOR UPDATE""", (candidate["delayed_id"],)).fetchone()
            if row["status"] not in {"pending", "running"}:
                continue
            invalid = stale(conn, row, incident)
            if invalid or row["expired"]:
                terminal(conn, row, "invalidated" if invalid else "expired", "BASIS_CHANGED" if invalid else "DELAYED_RECHECK_EXPIRED")
                if row["run_id"]:
                    conn.execute("""UPDATE incident_agent_app.runs SET status='cancelled',finished_at=clock_timestamp(),
                        lease_owner=NULL,lease_expires_at=NULL WHERE run_id=%s AND status=ANY(%s)""", (row["run_id"], list(ACTIVE)))
                continue
            if row["status"] == "running":
                run = conn.execute("SELECT status FROM incident_agent_app.runs WHERE run_id=%s", (row["run_id"],)).fetchone()
                if run["status"] in {"failed", "cancelled", "succeeded"}:
                    terminal(conn, row, "unknown", "DELAYED_TASK_ENDED_WITHOUT_RESULT")
                else:
                    conn.execute("UPDATE incident_agent_app.delayed_rechecks SET next_attempt_at=clock_timestamp()+interval '5 seconds' WHERE delayed_id=%s", (row["delayed_id"],))
                continue
            busy = conn.execute("SELECT 1 FROM incident_agent_app.runs WHERE incident_id=%s AND status=ANY(%s) LIMIT 1",
                                (row["incident_id"], list(ACTIVE))).fetchone()
            unsafe = conn.execute("""SELECT 1 FROM incident_agent_app.operations WHERE incident_id=%s
                AND state IN ('prepared','dispatching','outcome_unknown','manual_required') LIMIT 1""", (row["incident_id"],)).fetchone()
            if busy or unsafe or incident["phase"] in {"created", "awaiting_approval", "executing_remediation"}:
                conn.execute("""UPDATE incident_agent_app.delayed_rechecks SET reason='EVENT_BUSY',
                    next_attempt_at=clock_timestamp()+interval '5 seconds' WHERE delayed_id=%s""", (row["delayed_id"],))
                continue
            run_id = str(uuid4())
            payload = {"intent": "delayed_recheck", "delayed_id": row["delayed_id"]}
            context = {"message_id": None, "snapshot": row["snapshot"]}
            conn.execute("""INSERT INTO incident_agent_app.runs
                (run_id,incident_id,thread_id,run_kind,input_payload,input_sha256,request_sha256,workflow_version,context_snapshot,context_sha256,event_revision)
                VALUES (%s,%s,%s,'interaction',%s,%s,%s,%s,%s,%s,%s)""",
                (run_id, row["incident_id"], str(uuid4()), Jsonb(payload), request_digest(payload), request_digest(payload),
                 WORKFLOW, Jsonb(context), request_digest(context), incident["event_revision"]))
            conn.execute("""UPDATE incident_agent_app.delayed_rechecks SET status='running',run_id=%s,reason=NULL,
                next_attempt_at=clock_timestamp()+interval '5 seconds' WHERE delayed_id=%s""", (run_id, row["delayed_id"]))


def history(repo, incident_id, limit=20, before=None):
    from fastapi.encoders import jsonable_encoder
    from backend.app.persistence.rounds import RoundNotFound
    if not repo._read("SELECT 1 FROM incident_agent_app.incidents WHERE incident_id=%s", (incident_id,)):
        raise RoundNotFound()
    rows = repo._read("""SELECT sequence,delayed_id,observation_key,policy_version,status,due_at,expires_at,
        created_at,finished_at,reason,initial_result,result FROM incident_agent_app.delayed_rechecks
        WHERE incident_id=%s AND (%s::bigint IS NULL OR sequence<%s) ORDER BY sequence DESC LIMIT %s""",
        (incident_id, before, before, limit + 1))
    return jsonable_encoder({"items": rows[:limit], "next_before_sequence": rows[limit - 1]["sequence"] if len(rows) > limit else None})
