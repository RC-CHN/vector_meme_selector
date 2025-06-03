# celery_worker/tasks/image_tasks.py
import uuid # For generating custom task IDs later in Beat task
from celery_worker.celery_setup import celery_app
from app.core.db import SessionLocal
from app.models.image_metadata import ImageMetadata
from app.models.task_log import TaskLog, TaskStatusEnum, TaskTypeEnum # Import TaskTypeEnum for Beat task
from app.utils.llm_utils import get_two_stage_tags_for_image_bytes
from app.core.minio_handler import MinioHandler
from app.core.config import settings
import json
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import or_ # <--- 添加导入 or_
from datetime import datetime # For timestamps
import time # Import the time module
from celery_worker.tasks.vectorization_tasks import vectorize_image_and_store_task # Import for dispatching

# --- 配置常量用于 Beat 重试任务 ---
# 这些值也可以从 settings 中读取，如果希望通过 .env 配置的话
MAX_LLM_TAGGING_BEAT_RETRIES = settings.MAX_LLM_TAGGING_BEAT_RETRIES if hasattr(settings, 'MAX_LLM_TAGGING_BEAT_RETRIES') else 2
# 由于 Beat 任务每半小时运行一次，这个批次大小可以大一些，但仍受限于后续任务的 2 RPM
TAGGING_RETRY_BATCH_SIZE_PER_BEAT_RUN = settings.TAGGING_RETRY_BATCH_SIZE_PER_BEAT_RUN if hasattr(settings, 'TAGGING_RETRY_BATCH_SIZE_PER_BEAT_RUN') else 50

@celery_app.task(
    bind=True,
    name="tasks.image.generate_llm_tags",
    max_retries=settings.CELERY_LLM_TAGGING_TASK_MAX_RETRIES,
    default_retry_delay=settings.CELERY_LLM_TAGGING_TASK_DEFAULT_RETRY_DELAY,
    rate_limit=settings.CELERY_LLM_TAGGING_TASK_RATE_LIMIT if settings.CELERY_LLM_TAGGING_TASK_RATE_LIMIT else None
)
def generate_llm_tags_task(self, metadata_id: int):
    """
    Celery asynchronous task: Generates two-stage LLM tags for the specified image metadata record
    and updates the TaskLog SQL table with its progress.
    """
    celery_task_id = self.request.id
    task_id_log = f"[Celery Task {celery_task_id}]"
    print(f"{task_id_log} Received task for metadata_id: {metadata_id}")

    db = None # Initialize db session variable

    # Renamed celery_task_id to current_celery_task_id for clarity within this helper
    def update_task_log_status(session, current_celery_task_id: str, status: TaskStatusEnum, result_info_dict: dict = None, error_info: str = None):
        try:
            task_log_entry = session.query(TaskLog).filter(TaskLog.task_id == current_celery_task_id).first()
            
            if not task_log_entry:
                # This is a critical failure if the log entry isn't found, as it should have been created with PENDING status.
                err_msg = f"TaskLog entry not found for task_id {current_celery_task_id} when trying to update to {status}. This indicates a scheduling or DB integrity issue."
                print(f"[Celery Task {current_celery_task_id}] CRITICAL: {err_msg}")
                raise ValueError(err_msg) # Propagate error to the main task handler

            task_log_entry.status = status
            task_log_entry.updated_at = datetime.utcnow() # Use UTC for consistency
            if status == TaskStatusEnum.STARTED:
                if task_log_entry.status != TaskStatusEnum.PENDING and task_log_entry.started_at is not None: # Check if already started
                    print(f"[Celery Task {current_celery_task_id}] WARNING: TaskLog status was {task_log_entry.status} (not PENDING) when trying to set to STARTED. Started_at already set to {task_log_entry.started_at}.")
                else: # Standard case or first time setting started_at
                    task_log_entry.started_at = datetime.utcnow()
            elif status in [TaskStatusEnum.SUCCESS, TaskStatusEnum.FAILURE]:
                task_log_entry.finished_at = datetime.utcnow()
            
            if result_info_dict:
                task_log_entry.result_info = json.dumps(result_info_dict)
            elif error_info:
                task_log_entry.result_info = json.dumps({"error": error_info, "type": type(error_info).__name__ if not isinstance(error_info, str) else "Exception"})

            session.commit()
            print(f"[Celery Task {current_celery_task_id}] Updated TaskLog status to {status}")

        except SQLAlchemyError as sqla_exc:
            session.rollback()
            print(f"[Celery Task {current_celery_task_id}] SQLAlchemyError while updating TaskLog to {status}: {sqla_exc}")
            raise # Re-raise to be caught by the main task handler
        except Exception as exc: 
            session.rollback()
            print(f"[Celery Task {current_celery_task_id}] Exception while updating TaskLog to {status}: {exc}")
            raise # Re-raise

    # --- Main task logic for generate_llm_tags_task ---
    try:
        db = SessionLocal()
        
        # Startup check: Ensure PENDING TaskLog exists for this task_id (self.request.id)
        task_log_on_startup = db.query(TaskLog).filter(TaskLog.task_id == self.request.id).first()
        if not task_log_on_startup:
            err_msg = f"CRITICAL: TaskLog entry not found for task_id {self.request.id} at startup. Aborting task as PENDING log from dispatcher is missing."
            print(f"{task_id_log} {err_msg}")
            # This task should not run if its PENDING log was not created by the dispatcher.
            raise ValueError(err_msg) # This will mark the Celery task as FAILED.
        
        if task_log_on_startup.status != TaskStatusEnum.PENDING:
            warn_msg = f"WARNING: Task {self.request.id} found with status {task_log_on_startup.status}, not PENDING, at startup. Proceeding, but this may indicate an issue (e.g., task re-execution)."
            print(f"{task_id_log} {warn_msg}")
            # Depending on strictness, could raise error here too. For now, proceed with warning if not PENDING.
            # If it's already SUCCESS/FAILURE, perhaps it shouldn't run again.
            if task_log_on_startup.status in [TaskStatusEnum.SUCCESS, TaskStatusEnum.FAILURE]:
                info_msg = f"Task {self.request.id} already in terminal state {task_log_on_startup.status}. Skipping execution."
                print(f"{task_id_log} {info_msg}")
                return {"status": "skipped", "reason": info_msg, "current_log_status": task_log_on_startup.status.value }


        # Update TaskLog to STARTED using self.request.id
        update_task_log_status(db, self.request.id, TaskStatusEnum.STARTED)

        metadata = db.query(ImageMetadata).filter(ImageMetadata.id == metadata_id).first()
        if not metadata:
            err_msg = f"Metadata record with id {metadata_id} not found."
            print(f"{task_id_log} {err_msg}")
            update_task_log_status(db, self.request.id, TaskStatusEnum.FAILURE, error_info=err_msg)
            return # Task finishes

        print(f"{task_id_log} Processing image: {metadata.filename} (MinIO: {metadata.minio_object_name})")

        # 1. Fetch image bytes from MinIO
        image_bytes = None
        s3_object = None
        try:
            minio_client = MinioHandler.get_client()
            if not minio_client:
                raise ConnectionError("MinIO client not available in Celery task. Check MinIO configuration and service.")
            
            s3_object = minio_client.get_object(
                settings.MINIO_BUCKET_NAME,
                metadata.minio_object_name
            )
            image_bytes = s3_object.read()
            print(f"{task_id_log} Successfully fetched {len(image_bytes)} bytes from MinIO for {metadata.minio_object_name}")
        except Exception as e:
            print(f"{task_id_log} Error fetching image from MinIO for {metadata.minio_object_name}: {e}")
            raise self.retry(exc=e, countdown=int(self.default_retry_delay * (self.request.retries + 1)))
        finally:
            if s3_object and hasattr(s3_object, 'close'):
                s3_object.close()
            if s3_object and hasattr(s3_object, 'release_conn'):
                s3_object.release_conn()

        if not image_bytes:
            print(f"{task_id_log} Failed to get image bytes for {metadata.minio_object_name} after MinIO attempt.")
            raise ValueError(f"Image bytes could not be retrieved from MinIO for {metadata.minio_object_name}")

        # 2. Call LLM for two-stage tagging
        print(f"{task_id_log} Calling LLM for two-stage tagging for {metadata.filename}...")
        tagging_results = get_two_stage_tags_for_image_bytes(image_bytes, metadata.filename) # This is a dict

        # 3. Update ImageMetadata record in the database
        metadata.emotion_tags_text = tagging_results.get("emotion_tags_json_str")
        metadata.semantic_tags_text = tagging_results.get("semantic_tags_json_str")
        metadata.is_emotion_tagged = tagging_results.get("emotion_tags_json_str") is not None
        metadata.is_semantic_tagged = tagging_results.get("semantic_tags_json_str") is not None
        
        llm_errors = tagging_results.get("errors")
        if llm_errors:
            error_log_message = f"{task_id_log} LLM tagging for {metadata.filename} (ID: {metadata_id}) encountered errors: {'; '.join(llm_errors)}"
            print(error_log_message)
            # Optionally store these LLM-specific errors in ImageMetadata or TaskLog's result_info if partial success
            # For now, if there are errors, we might consider the task partially failed or fully failed depending on policy.
            # If any tag type failed, let's consider it a failure for simplicity in TaskLog,
            # but ImageMetadata will store whatever tags were successfully generated.
            if not metadata.is_emotion_tagged or not metadata.is_semantic_tagged:
                # This case is handled below by checking these flags for final TaskLog status.
                # No need to update TaskLog to FAILURE here prematurely if ImageMetadata is to be committed.
                pass
            # else: (both are True or one/both are None but llm_errors is populated)
            #   This is also handled by the final status check.

        db.commit() # Commit changes to ImageMetadata (stores whatever tags were fetched, even if partial)
        success_log_message = f"{task_id_log} Successfully processed and updated ImageMetadata for ID: {metadata_id}. Emotion tagged: {metadata.is_emotion_tagged}, Semantic tagged: {metadata.is_semantic_tagged}."
        print(success_log_message)

        # Dispatch vectorization task if tagging was successful for both stages
        if metadata.is_emotion_tagged and metadata.is_semantic_tagged:
            print(f"{task_id_log} Both emotion and semantic tagging successful for metadata_id: {metadata.id}. Attempting to dispatch initial vectorization task.")
            
            vectorization_custom_task_id = uuid.uuid4().hex
            vectorization_task_log_created = False
            vectorization_celery_task_submitted = False
            dispatch_error_message = None

            try:
                # 1. Create PENDING TaskLog for the initial vectorization task
                new_vectorization_task_log = TaskLog(
                    task_id=vectorization_custom_task_id,
                    task_name=vectorize_image_and_store_task.name,
                    task_type=TaskTypeEnum.INITIAL_VECTORIZATION,
                    status=TaskStatusEnum.PENDING,
                    image_metadata_id=metadata.id,
                    created_at=datetime.utcnow()
                )
                db.add(new_vectorization_task_log)
                db.commit()
                vectorization_task_log_created = True
                print(f"{task_id_log} Successfully created PENDING TaskLog {vectorization_custom_task_id} for initial vectorization of metadata_id: {metadata.id}.")

                # 2. Dispatch Celery task for vectorization
                target_queue_vectorization = 'initial_vectorization_queue' # Placeholder, to be configured
                vectorize_image_and_store_task.apply_async(
                    args=[metadata.id],
                    task_id=vectorization_custom_task_id,
                    queue=target_queue_vectorization
                )
                vectorization_celery_task_submitted = True
                print(f"{task_id_log} Successfully dispatched initial vectorization task {vectorization_custom_task_id} to queue '{target_queue_vectorization}' for metadata_id: {metadata.id}.")

            except SQLAlchemyError as e_sql_vec_log:
                db.rollback()
                dispatch_error_message = f"SQLAlchemyError creating TaskLog for initial vectorization (task_id: {vectorization_custom_task_id}): {e_sql_vec_log}"
                print(f"{task_id_log} {dispatch_error_message}")
            except Exception as e_dispatch_vec:
                # This catches errors from apply_async or other unexpected issues.
                # If task_log_created is True, it means TaskLog commit was successful, but Celery dispatch failed.
                dispatch_error_message = f"Exception dispatching initial vectorization task (task_id: {vectorization_custom_task_id}): {e_dispatch_vec}"
                print(f"{task_id_log} {dispatch_error_message}")
                if vectorization_task_log_created:
                    print(f"{task_id_log} CRITICAL: Orphan PENDING TaskLog {vectorization_custom_task_id} may exist for initial vectorization of metadata_id {metadata.id}.")
            
            if dispatch_error_message:
                # If dispatching vectorization task failed, add this to llm_errors for the current tagging task's log.
                if llm_errors is None: llm_errors = []
                llm_errors.append(f"INITIAL_VECTORIZATION_DISPATCH_ERROR: {dispatch_error_message}")
        else:
            print(f"{task_id_log} One or both tagging stages incomplete for metadata_id: {metadata.id}. Skipping vectorization task dispatch.")
        
        # Prepare task result for TaskLog and Celery for the current tagging task
        # Prepare task result for TaskLog and Celery
        emotion_tags_list_val = tagging_results.get("emotion_tags_list")
        semantic_tags_list_val = tagging_results.get("semantic_tags_list")

        task_return_value = {
            "status": "success", 
            "metadata_id": metadata_id, 
            "emotion_tags_count": len(emotion_tags_list_val) if emotion_tags_list_val is not None else 0, 
            "semantic_tags_count": len(semantic_tags_list_val) if semantic_tags_list_val is not None else 0,
            "llm_errors": llm_errors if llm_errors else None # llm_errors might now include dispatch error
        }
        
        # Determine final TaskLog status based on "没完全成功就是完全没成功"
        if metadata.is_emotion_tagged and metadata.is_semantic_tagged:
            final_task_log_status = TaskStatusEnum.SUCCESS
            # task_return_value already prepared with counts and any non-critical llm_errors (like dispatch error)
            update_task_log_status(db, self.request.id, final_task_log_status, result_info_dict=task_return_value)
        else:
            final_task_log_status = TaskStatusEnum.FAILURE
            # Ensure llm_errors in task_return_value reflects the tagging failure if not already there
            error_summary_for_log = "LLM tagging incomplete: "
            if not metadata.is_emotion_tagged: error_summary_for_log += "Emotion tags missing. "
            if not metadata.is_semantic_tagged: error_summary_for_log += "Semantic tags missing. "
            if llm_errors:
                error_summary_for_log += f"Details: {'; '.join(llm_errors)}"
            else: # If llm_errors was empty but flags are false
                llm_errors = [error_summary_for_log.strip()] # Populate llm_errors
                task_return_value["llm_errors"] = llm_errors
            
            update_task_log_status(db, self.request.id, final_task_log_status, error_info=error_summary_for_log.strip())
            
        return task_return_value

    except ValueError as ve: # Catch ValueError from startup PENDING log check or update_task_log_status
        # This error (e.g., PENDING log not found) should mark the task as failed without retry by Celery for this specific error.
        # The error is already logged by the raising function or the startup check.
        if db: db.rollback() # Rollback any DB changes from this task attempt
        # Do not try to update TaskLog here as the problem might be with TaskLog itself or its absence.
        raise # Re-raise to let Celery handle it as a task failure based on this exception.
    except Exception as e:
        # This catches other exceptions (MinIO, LLM, DB commit for ImageMetadata, etc.)
        if db: 
            db.rollback() # Rollback ImageMetadata changes if any uncommitted
            try:
                # Try to update TaskLog to FAILURE, but be wary if the PENDING log was the issue (though ValueError should catch that first)
                update_task_log_status(db, self.request.id, TaskStatusEnum.FAILURE, error_info=f"Task failed due to unhandled exception: {type(e).__name__} - {str(e)}")
            except ValueError: # Raised by update_task_log_status if log was missing (should have been caught by startup check)
                 print(f"{task_id_log} CRITICAL: Could not update TaskLog to FAILURE for task {self.request.id} as PENDING log was likely missing.")
            except Exception as log_e:
                 print(f"{task_id_log} Further error trying to update TaskLog to FAILURE for task {self.request.id}: {log_e}")
        
        error_message = f"{task_id_log} Unhandled exception processing metadata_id {metadata_id} for task {self.request.id}: {type(e).__name__} - {str(e)}"
        print(error_message)
        # Re-raise exception for Celery to handle retries (if configured for this task type) or mark as failure.
        # The ValueError from startup check should lead to non-retryable failure.
        # Other exceptions here might be retryable by Celery's default mechanisms.
        raise
    finally:
        if db:
            # Introduce a configurable delay before closing the DB and finishing the task
            delay_seconds = settings.CELERY_TASK_POST_DELAY_SECONDS
            if delay_seconds > 0:
                print(f"{task_id_log} Task processing logic finished. Adding a {delay_seconds}-second delay before closing resources (configurable via CELERY_TASK_POST_DELAY_SECONDS in .env).")
                time.sleep(delay_seconds)
                print(f"{task_id_log} Delay finished.")
            else:
                print(f"{task_id_log} Task processing logic finished. No post-task delay configured.")
            
            db.close()
            print(f"{task_id_log} Resources closed. Task complete.")


@celery_app.task(name="tasks.image.retry_failed_tagging_beat")
def retry_failed_tagging_beat_task():
    """
    Celery Beat task to find image tagging tasks that previously failed (or incomplete)
    and re-dispatch them, up to a certain number of beat retries.
    Scheduled to run periodically (e.g., every 30 minutes).
    """
    db = None
    processed_count = 0
    task_name_log = f"[BeatTask RetryTagging - {datetime.utcnow().isoformat()}]"
    print(f"{task_name_log} Starting scan for failed/incomplete tagging tasks to retry.")

    try:
        db = SessionLocal()
        
        # 查询打标未完成 (任一标签为False) 且 Beat 重试次数未达上限的记录
        records_to_retry = db.query(ImageMetadata).filter(
            or_(ImageMetadata.is_emotion_tagged == False, ImageMetadata.is_semantic_tagged == False),
            ImageMetadata.llm_tagging_beat_retry_count < MAX_LLM_TAGGING_BEAT_RETRIES
        ).order_by(ImageMetadata.id).limit(TAGGING_RETRY_BATCH_SIZE_PER_BEAT_RUN).all()

        if not records_to_retry:
            print(f"{task_name_log} No image tagging tasks found needing beat retry.")
            return {"status": "success", "message": "No tasks to retry.", "processed_count": 0}

        print(f"{task_name_log} Found {len(records_to_retry)} image(s) for tagging retry (limit {TAGGING_RETRY_BATCH_SIZE_PER_BEAT_RUN}).")
        
        for metadata_item in records_to_retry:
            # Check for active (PENDING or STARTED) tagging tasks for this metadata_id
            active_tagging_task = db.query(TaskLog).filter(
                TaskLog.image_metadata_id == metadata_item.id,
                TaskLog.task_name == generate_llm_tags_task.name, # Check for the specific tagging task
                TaskLog.status.in_([TaskStatusEnum.PENDING, TaskStatusEnum.STARTED])
            ).first()

            if active_tagging_task:
                print(f"{task_name_log} Skipping retry for metadata_id {metadata_item.id}: Active tagging task {active_tagging_task.task_id} (Status: {active_tagging_task.status.value}) found.")
                continue

            # Check for at least one historical FAILED tagging task for this metadata_id
            failed_tagging_task_history = db.query(TaskLog).filter(
                TaskLog.image_metadata_id == metadata_item.id,
                TaskLog.task_name == generate_llm_tags_task.name, # Check for the specific tagging task
                TaskLog.status == TaskStatusEnum.FAILURE
            ).first()

            if not failed_tagging_task_history:
                print(f"{task_name_log} Skipping retry for metadata_id {metadata_item.id}: No prior FAILED tagging task log found.")
                continue
            
            # If passed both checks, proceed with retry
            custom_task_id = uuid.uuid4().hex
            task_log_created = False
            celery_task_submitted = False
            
            current_beat_attempt_count = metadata_item.llm_tagging_beat_retry_count + 1 # Tentative new count

            try:
                # 1. Create PENDING TaskLog entry with custom_task_id
                new_task_log_entry = TaskLog(
                    task_id=custom_task_id,
                    task_name=generate_llm_tags_task.name,
                    task_type=TaskTypeEnum.RETRY_TAGGING, # Set task_type
                    status=TaskStatusEnum.PENDING,
                    image_metadata_id=metadata_item.id,
                    created_at=datetime.utcnow() # Explicitly set for clarity, though server_default exists
                )
                db.add(new_task_log_entry)
                
                # Increment beat_retry_count on the metadata object
                metadata_item.llm_tagging_beat_retry_count = current_beat_attempt_count
                db.add(metadata_item) # Ensure metadata_item is also part of the session changes
                
                db.commit() # Commit both TaskLog and metadata update together
                task_log_created = True
                print(f"{task_name_log} Successfully created PENDING TaskLog {custom_task_id} and updated beat_retry_count to {current_beat_attempt_count} for metadata_id: {metadata_item.id}.")

                # 2. If TaskLog creation was successful, dispatch Celery task with the same custom_task_id
                # TODO: Replace 'default_queue_for_retry_tagging' with the actual target queue name from settings or config
                # For now, using a placeholder. This will be part of Celery queue configuration (Step D).
                target_queue = 'retry_tagging_queue' # Placeholder, will be configured later
                generate_llm_tags_task.apply_async(
                    args=[metadata_item.id],
                    task_id=custom_task_id,
                    queue=target_queue 
                )
                celery_task_submitted = True
                print(f"{task_name_log} Successfully dispatched Celery task {custom_task_id} to queue '{target_queue}' for metadata_id: {metadata_item.id}.")
                processed_count += 1

            except SQLAlchemyError as e_sql:
                db.rollback()
                # If task_log_created is True here, it means commit failed after add, or during commit itself.
                # If it's False, it means add itself failed or error before commit.
                # The metadata_item.llm_tagging_beat_retry_count in memory is not persisted.
                print(f"{task_name_log} SQLAlchemyError for metadata_id {metadata_item.id} (custom_task_id: {custom_task_id}, beat attempt: {current_beat_attempt_count}): {e_sql}. TaskLog created: {task_log_created}, Celery task submitted: {celery_task_submitted}.")
                # If task_log_created but celery_task_submitted is False, we have an orphan TaskLog.
                # This needs monitoring/cleanup or specific handling if critical.
                # For now, the Beat task will simply retry this metadata_item in the next run if count < max.

            except Exception as e_celery_dispatch:
                # This block catches errors from apply_async (e.g., broker down)
                # or any other unexpected error after TaskLog commit but before/during Celery dispatch.
                # db.rollback() is not strictly needed here if TaskLog was already committed,
                # but doesn't hurt if there were other uncommitted session changes.
                # The llm_tagging_beat_retry_count and TaskLog are already committed if task_log_created is True.
                print(f"{task_name_log} Exception dispatching Celery task for metadata_id {metadata_item.id} (custom_task_id: {custom_task_id}, beat attempt: {current_beat_attempt_count}): {e_celery_dispatch}. TaskLog created: {task_log_created}.")
                if task_log_created:
                    print(f"{task_name_log} CRITICAL: Orphan PENDING TaskLog {custom_task_id} may exist for metadata_id {metadata_item.id} as Celery dispatch failed after log creation.")
                    # An auxiliary cleanup task for orphan TaskLogs would be beneficial.
                    # The current metadata_item.llm_tagging_beat_retry_count is committed.
                    # This metadata_item might be picked up again if it still meets criteria, potentially creating another TaskLog.
                    # This highlights the need for robust orphan handling or ensuring task_id uniqueness if re-attempted for same metadata.
                    # For now, we proceed, and the sub-task startup check for PENDING log is the main guard.
        
        return {"status": "success", "processed_count": processed_count, "batch_size_limit": TAGGING_RETRY_BATCH_SIZE_PER_BEAT_RUN}

    except Exception as e_outer:
        print(f"{task_name_log} General error in beat task: {e_outer}")
        if db: 
            db.rollback()
        return {"status": "error", "reason": str(e_outer), "processed_count": processed_count}
    finally:
        if db:
            db.close()
        print(f"{task_name_log} Finished scan. Processed {processed_count} tasks for retry dispatch.")
