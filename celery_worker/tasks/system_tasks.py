import json
from datetime import datetime, timedelta

from celery_worker.celery_setup import celery_app
from app.core.db import SessionLocal
from app.models.task_log import TaskLog, TaskStatusEnum
from app.core.config import settings
from sqlalchemy.exc import SQLAlchemyError

@celery_app.task(name="tasks.system.monitor_task_timeouts_beat")
def monitor_task_timeouts_beat():
    """
    Celery Beat task to monitor and handle timed-out tasks in TaskLog.
    Marks tasks stuck in PENDING or STARTED state for too long as FAILURE.
    """
    task_name_log = f"[BeatTask MonitorTimeouts - {datetime.utcnow().isoformat()}]"
    print(f"{task_name_log} Starting scan for timed-out tasks.")
    
    db = None
    processed_pending_count = 0
    processed_started_count = 0

    try:
        db = SessionLocal()
        current_time = datetime.utcnow()

        # --- Handle PENDING timeouts ---
        pending_timeout_threshold = timedelta(seconds=settings.CELERY_TASK_PENDING_TIMEOUT_SECONDS)
        pending_deadline = current_time - pending_timeout_threshold
        
        timed_out_pending_tasks = db.query(TaskLog).filter(
            TaskLog.status == TaskStatusEnum.PENDING,
            TaskLog.created_at < pending_deadline
        ).all()

        if timed_out_pending_tasks:
            print(f"{task_name_log} Found {len(timed_out_pending_tasks)} PENDING tasks that timed out (threshold: {settings.CELERY_TASK_PENDING_TIMEOUT_SECONDS}s).")
            for task_log_entry in timed_out_pending_tasks:
                original_task_id_to_revoke = task_log_entry.task_id # Save before potential modification
                try:
                    print(f"{task_name_log} Processing PENDING timeout for TaskLog ID: {task_log_entry.id}, Celery Task ID: {original_task_id_to_revoke}, Created: {task_log_entry.created_at}")
                    task_log_entry.status = TaskStatusEnum.FAILURE
                    task_log_entry.finished_at = current_time
                    task_log_entry.result_info = json.dumps({
                        "error": "Task exceeded PENDING state timeout.",
                        "timeout_seconds": settings.CELERY_TASK_PENDING_TIMEOUT_SECONDS,
                        "detected_at": current_time.isoformat()
                    })
                    task_log_entry.updated_at = current_time
                    
                    # Attempt to revoke from Celery (best effort)
                    celery_app.control.revoke(original_task_id_to_revoke)
                    print(f"{task_name_log} Attempted to revoke PENDING Celery task: {original_task_id_to_revoke}")
                    
                    db.commit()
                    processed_pending_count += 1
                except SQLAlchemyError as e_sql:
                    db.rollback()
                    print(f"{task_name_log} SQLAlchemyError processing PENDING timeout for TaskLog ID {task_log_entry.id} (Celery Task ID: {original_task_id_to_revoke}): {e_sql}")
                except Exception as e_proc:
                    db.rollback()
                    print(f"{task_name_log} Exception processing PENDING timeout for TaskLog ID {task_log_entry.id} (Celery Task ID: {original_task_id_to_revoke}): {e_proc}")
        else:
            print(f"{task_name_log} No PENDING tasks found to be timed out.")

        # --- Handle STARTED timeouts ---
        started_timeout_threshold = timedelta(seconds=settings.CELERY_TASK_STARTED_TIMEOUT_SECONDS)
        started_deadline = current_time - started_timeout_threshold

        timed_out_started_tasks = db.query(TaskLog).filter(
            TaskLog.status == TaskStatusEnum.STARTED,
            TaskLog.started_at < started_deadline
        ).all()

        if timed_out_started_tasks:
            print(f"{task_name_log} Found {len(timed_out_started_tasks)} STARTED tasks that timed out (threshold: {settings.CELERY_TASK_STARTED_TIMEOUT_SECONDS}s).")
            for task_log_entry in timed_out_started_tasks:
                original_task_id_to_revoke = task_log_entry.task_id
                try:
                    print(f"{task_name_log} Processing STARTED timeout for TaskLog ID: {task_log_entry.id}, Celery Task ID: {original_task_id_to_revoke}, Started: {task_log_entry.started_at}")
                    task_log_entry.status = TaskStatusEnum.FAILURE
                    task_log_entry.finished_at = current_time
                    task_log_entry.result_info = json.dumps({
                        "error": "Task exceeded STARTED state timeout.",
                        "timeout_seconds": settings.CELERY_TASK_STARTED_TIMEOUT_SECONDS,
                        "detected_at": current_time.isoformat()
                    })
                    task_log_entry.updated_at = current_time

                    # Attempt to terminate the Celery task (best effort)
                    celery_app.control.revoke(original_task_id_to_revoke, terminate=True, signal='SIGKILL')
                    print(f"{task_name_log} Attempted to terminate STARTED Celery task: {original_task_id_to_revoke}")

                    db.commit()
                    processed_started_count += 1
                except SQLAlchemyError as e_sql:
                    db.rollback()
                    print(f"{task_name_log} SQLAlchemyError processing STARTED timeout for TaskLog ID {task_log_entry.id} (Celery Task ID: {original_task_id_to_revoke}): {e_sql}")
                except Exception as e_proc:
                    db.rollback()
                    print(f"{task_name_log} Exception processing STARTED timeout for TaskLog ID {task_log_entry.id} (Celery Task ID: {original_task_id_to_revoke}): {e_proc}")
        else:
            print(f"{task_name_log} No STARTED tasks found to be timed out.")
            
        return {
            "status": "success",
            "processed_pending_timeouts": processed_pending_count,
            "processed_started_timeouts": processed_started_count
        }

    except Exception as e_outer:
        print(f"{task_name_log} General error in monitor_task_timeouts_beat: {e_outer}")
        if db:
            db.rollback() # Rollback any potential uncommitted changes from a failed loop
        # Optionally re-raise or handle to notify admins
        # For a beat task, it's often better to log and let it retry on next schedule
        return {"status": "error", "reason": str(e_outer)}
    finally:
        if db:
            db.close()
        print(f"{task_name_log} Finished scan. Processed PENDING: {processed_pending_count}, Processed STARTED: {processed_started_count}.")
