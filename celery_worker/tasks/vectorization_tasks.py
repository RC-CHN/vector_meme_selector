import json
import uuid # For generating custom task IDs
from celery import shared_task
from celery.schedules import crontab
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session # Added import
from datetime import datetime

from celery_worker.celery_setup import celery_app
from app.core.db import SessionLocal
from app.models.image_metadata import ImageMetadata
from app.models.task_log import TaskLog, TaskStatusEnum, TaskTypeEnum # Import TaskTypeEnum
from app.utils.embedding_utils import get_text_embedding
from app.core.milvus_handler import MilvusHandler
# from app.core.config import settings # settings is used by embedding_utils and milvus_handler internally

# --- Helper function for TaskLog updates ---
def _update_task_log_vectorization(
    db_session: Session, # Changed SessionLocal to Session
    current_celery_task_id: str, 
    # metadata_id is mainly for logging context if needed, actual link is in TaskLog table
    # current_metadata_id: int, 
    status: TaskStatusEnum, 
    result_info: dict = None, 
    error_info: str = None
):
    # This helper assumes the main task function handles the db session lifecycle (commit/rollback for this specific update)
    # However, for safety, it will manage its own small transaction for the log update itself.
    # The main task should still handle broader rollbacks if this helper raises an error.
    
    # Re-query task_log_entry within this function's scope if db_session is passed around,
    # or expect the calling function to pass the task_log_entry object.
    # For now, let's assume db_session is the active session from the main task.
    
    task_log_entry = db_session.query(TaskLog).filter(TaskLog.task_id == current_celery_task_id).first()
    
    if not task_log_entry:
        error_msg = f"TaskLog entry not found for task_id {current_celery_task_id} when trying to update to {status}. This indicates a scheduling or DB integrity issue."
        print(f"[VectorizeTask {current_celery_task_id}] CRITICAL: {error_msg}")
        raise ValueError(error_msg) # Propagate error

    # Check current status before updating, especially for STARTED
    if status == TaskStatusEnum.STARTED:
        if task_log_entry.status != TaskStatusEnum.PENDING:
            print(f"[VectorizeTask {current_celery_task_id}] WARNING: TaskLog status was {task_log_entry.status} (not PENDING) when trying to set to STARTED. Started_at: {task_log_entry.started_at}.")
            # If already started, don't overwrite started_at unless it's None
            if task_log_entry.started_at is None:
                 task_log_entry.started_at = datetime.utcnow()
        else: # PENDING -> STARTED
            task_log_entry.started_at = datetime.utcnow()
    elif status in [TaskStatusEnum.SUCCESS, TaskStatusEnum.FAILURE]:
        task_log_entry.finished_at = datetime.utcnow()
    
    task_log_entry.status = status
    task_log_entry.updated_at = datetime.utcnow() # onupdate might handle this if server_default is also func.now()
    
    if result_info:
        task_log_entry.result_info = json.dumps(result_info)
    elif error_info:
        task_log_entry.result_info = json.dumps({"error": error_info, "type": type(error_info).__name__ if not isinstance(error_info, str) else "Exception"})
    
    try:
        db_session.commit()
        # The metadata_id for logging here can be fetched from task_log_entry.image_metadata_id
        log_metadata_id = task_log_entry.image_metadata_id if task_log_entry else "UNKNOWN (log entry missing)"
        print(f"[VectorizeTask {current_celery_task_id}] TaskLog updated to {status} for image_metadata_id {log_metadata_id}")
    except SQLAlchemyError as sqla_exc_commit:
        db_session.rollback()
        log_metadata_id_on_err = task_log_entry.image_metadata_id if task_log_entry else "UNKNOWN (log entry missing)"
        print(f"[VectorizeTask {current_celery_task_id}] SQLAlchemyError during commit for TaskLog update to {status} for image_metadata_id {log_metadata_id_on_err}: {sqla_exc_commit}")
        raise # Re-raise to be caught by the main task handler
    except Exception as exc_commit: 
        db_session.rollback()
        log_metadata_id_on_err = task_log_entry.image_metadata_id if task_log_entry else "UNKNOWN (log entry missing)"
        print(f"[VectorizeTask {current_celery_task_id}] Exception during commit for TaskLog update to {status} for image_metadata_id {log_metadata_id_on_err}: {exc_commit}")
        raise # Re-raise

# --- Celery Task for Vectorizing Image Tags ---
@celery_app.task(bind=True, name="tasks.image.vectorize_and_store_tags", max_retries=3, default_retry_delay=60 * 2)
def vectorize_image_and_store_task(self, metadata_id: int): # metadata_id is passed from dispatcher
    """
    Celery task to generate embeddings for an image's emotion and semantic tags 
    and store them in Milvus. Adheres to "没完全成功就是完全没成功".
    """
    task_celery_id = self.request.id # This is the custom_task_id set by the dispatcher
    task_id_log = f"[VectorizeTask {task_celery_id}]" # For logging prefix
    print(f"{task_id_log} Received for metadata_id: {metadata_id}")

    db = None
    milvus_handler = None

    try:
        db = SessionLocal()

        # Startup check: Ensure PENDING TaskLog exists for this task_id
        task_log_on_startup = db.query(TaskLog).filter(TaskLog.task_id == task_celery_id).first()
        if not task_log_on_startup:
            err_msg = f"CRITICAL: TaskLog entry not found for task_id {task_celery_id} at startup. Aborting task."
            print(f"{task_id_log} {err_msg}")
            raise ValueError(err_msg) # Fail non-retryable for this task instance

        if task_log_on_startup.image_metadata_id != metadata_id:
            # Integrity check: metadata_id in TaskLog should match the one passed to the task.
            # This might happen if task_id was somehow reused or dispatcher logic error.
            err_msg = f"CRITICAL: TaskLog metadata_id ({task_log_on_startup.image_metadata_id}) does not match task argument metadata_id ({metadata_id}) for task_id {task_celery_id}."
            print(f"{task_id_log} {err_msg}")
            # Update this specific (wrongly associated) task log to failure.
            _update_task_log_vectorization(db, task_celery_id, TaskStatusEnum.FAILURE, error_info=err_msg)
            raise ValueError(err_msg) # Fail non-retryable

        if task_log_on_startup.status != TaskStatusEnum.PENDING:
            warn_msg = f"WARNING: Task {task_celery_id} found with status {task_log_on_startup.status}, not PENDING, at startup."
            print(f"{task_id_log} {warn_msg}")
            if task_log_on_startup.status in [TaskStatusEnum.SUCCESS, TaskStatusEnum.FAILURE]:
                info_msg = f"Task {task_celery_id} already in terminal state {task_log_on_startup.status}. Skipping."
                print(f"{task_id_log} {info_msg}")
                return {"status": "skipped", "reason": info_msg, "current_log_status": task_log_on_startup.status.value}
        
        # Update TaskLog to STARTED
        _update_task_log_vectorization(db, task_celery_id, TaskStatusEnum.STARTED)

        metadata = db.query(ImageMetadata).filter(ImageMetadata.id == metadata_id).first()
        if not metadata: # Should be caught by dispatcher, but good to double check
            err_msg = f"ImageMetadata record with id {metadata_id} not found after task started."
            print(f"{task_id_log} {err_msg}")
            _update_task_log_vectorization(db, task_celery_id, TaskStatusEnum.FAILURE, error_info=err_msg)
            return {"status": "failure", "reason": err_msg}

        if metadata.is_vectorized: # Check if already vectorized by another concurrent task or previous run
            msg = f"Image metadata_id {metadata_id} (MinIO: {metadata.minio_object_name}) is already marked as vectorized. Skipping."
            print(f"{task_id_log} {msg}")
            _update_task_log_vectorization(db, task_celery_id, TaskStatusEnum.SUCCESS, result_info={"message": msg, "skipped_due_to_already_vectorized": True})
            return {"status": "success", "message": msg, "skipped": True}

        minio_object_name_pk = metadata.minio_object_name
        print(f"{task_id_log} Processing for MinIO object: {minio_object_name_pk}")

        # Initialize MilvusHandler (connects and ensures collections exist)
        try:
            milvus_handler = MilvusHandler()
            milvus_handler.ensure_all_collections_exist()
        except Exception as mh_exc:
            err_msg = f"Failed to initialize MilvusHandler or ensure collections: {str(mh_exc)}"
            print(f"{task_id_log} {err_msg}")
            _update_task_log_vectorization(db, task_celery_id, TaskStatusEnum.FAILURE, error_info=err_msg)
            # This is a system/infra issue, Celery's retry mechanism is appropriate here.
            raise self.retry(exc=mh_exc)

        # --- Revised vectorization logic based on "没完全成功就是完全没成功" ---
        emotion_tags_need_vectorization = False
        emotion_vectorization_successful = False
        semantic_tags_need_vectorization = False
        semantic_vectorization_successful = False
        
        error_details = []

        # 1. Process Emotion Tags
        if metadata.emotion_tags_text:
            try:
                tags_data = json.loads(metadata.emotion_tags_text)
                text_list_for_embedding = []
                if isinstance(tags_data, dict):
                    text_list_for_embedding = tags_data.get("emotion_tags", [])
                elif isinstance(tags_data, list):
                    text_list_for_embedding = tags_data # tags_data is already the list of tags
                else:
                    # Fallback or error for unexpected type
                    error_details.append(f"Unexpected data type for emotion_tags_text after JSON parsing: {type(tags_data)}")
                    print(f"{task_id_log} WARNING: {error_details[-1]}")
                
                text_to_embed = ", ".join(text_list_for_embedding)
                if text_to_embed: # Only proceed if there's actual text
                    emotion_tags_need_vectorization = True
                    print(f"{task_id_log} Emotion tags for {minio_object_name_pk}: '{text_to_embed[:100]}...'")
                    emotion_vector = get_text_embedding(text_to_embed)
                    if emotion_vector:
                        milvus_handler.insert_vectors(
                            milvus_handler.collection_emotion_tags_name,
                            minio_object_names=[minio_object_name_pk],
                            vectors=[emotion_vector]
                        )
                        emotion_vectorization_successful = True
                        print(f"{task_id_log} Emotion vector stored for {minio_object_name_pk}.")
                    else:
                        error_details.append(f"Failed to generate emotion embedding for {minio_object_name_pk}.")
                        print(f"{task_id_log} {error_details[-1]}")
                else: # Text was empty after parsing
                    emotion_vectorization_successful = True # No content to vectorize = success for this part
            except Exception as e_em:
                error_details.append(f"Error processing emotion tags for {minio_object_name_pk}: {str(e_em)}")
                print(f"{task_id_log} {error_details[-1]}")
        else: # No emotion_tags_text
            emotion_vectorization_successful = True # No content = success for this part

        # 2. Process Semantic Tags
        if metadata.semantic_tags_text:
            try:
                tags_data = json.loads(metadata.semantic_tags_text)
                text_list_for_embedding = []
                if isinstance(tags_data, dict):
                    text_list_for_embedding = tags_data.get("semantic_tags", [])
                elif isinstance(tags_data, list):
                    text_list_for_embedding = tags_data # tags_data is already the list of tags
                else:
                    # Fallback or error for unexpected type
                    error_details.append(f"Unexpected data type for semantic_tags_text after JSON parsing: {type(tags_data)}")
                    print(f"{task_id_log} WARNING: {error_details[-1]}")

                text_to_embed = ", ".join(text_list_for_embedding)
                if text_to_embed: # Only proceed if there's actual text
                    semantic_tags_need_vectorization = True
                    print(f"{task_id_log} Semantic tags for {minio_object_name_pk}: '{text_to_embed[:100]}...'")
                    semantic_vector = get_text_embedding(text_to_embed)
                    if semantic_vector:
                        milvus_handler.insert_vectors(
                            milvus_handler.collection_semantic_tags_name,
                            minio_object_names=[minio_object_name_pk],
                            vectors=[semantic_vector]
                        )
                        semantic_vectorization_successful = True
                        print(f"{task_id_log} Semantic vector stored for {minio_object_name_pk}.")
                    else:
                        error_details.append(f"Failed to generate semantic embedding for {minio_object_name_pk}.")
                        print(f"{task_id_log} {error_details[-1]}")
                else: # Text was empty after parsing
                    semantic_vectorization_successful = True # No content = success for this part
            except Exception as e_sem:
                error_details.append(f"Error processing semantic tags for {minio_object_name_pk}: {str(e_sem)}")
                print(f"{task_id_log} {error_details[-1]}")
        else: # No semantic_tags_text
            semantic_vectorization_successful = True # No content = success for this part

        # 3. Determine overall task success and update ImageMetadata
        overall_task_successful = emotion_vectorization_successful and semantic_vectorization_successful
        
        task_result_payload = {
            "emotion_tags_needed_vectorization": emotion_tags_need_vectorization,
            "emotion_vectorization_attempt_successful": emotion_vectorization_successful,
            "semantic_tags_needed_vectorization": semantic_tags_need_vectorization,
            "semantic_vectorization_attempt_successful": semantic_vectorization_successful,
            "errors": error_details if error_details else None
        }

        if overall_task_successful:
            metadata.is_vectorized = True # Mark as vectorized only if all necessary parts succeeded
            db.commit() # Commit ImageMetadata.is_vectorized update
            success_msg = f"Vectorization process completed successfully for metadata_id {metadata_id}."
            print(f"{task_id_log} {success_msg}")
            _update_task_log_vectorization(db, task_celery_id, TaskStatusEnum.SUCCESS, result_info=task_result_payload)
            return {"status": "success", "message": success_msg, **task_result_payload}
        else:
            # Failure case: "没完全成功就是完全没成功"
            if metadata.is_vectorized: # Ensure it's False if task fails
                metadata.is_vectorized = False
                db.commit() # Commit change if it was True
            
            failure_reason = f"Vectorization failed for metadata_id {metadata_id}. Details: {'; '.join(error_details)}"
            print(f"{task_id_log} {failure_reason}")
            _update_task_log_vectorization(db, task_celery_id, TaskStatusEnum.FAILURE, error_info=failure_reason)
            # This task instance fails. Beat scheduler will pick it up based on is_vectorized=False.
            # No self.retry() here for this business logic failure.
            return {"status": "failure", "reason": failure_reason, **task_result_payload}

    except ValueError as ve: # From _update_task_log_vectorization or startup PENDING log check
        if db: db.rollback()
        # Error already logged by the raiser. Let Celery mark as FAILED.
        raise
    except ConnectionError as conn_err: # From MilvusHandler connection attempt
        err_msg = f"Milvus connection error for metadata_id {metadata_id}: {str(conn_err)}"
        print(f"{task_id_log} {err_msg}")
        if db: 
            db.rollback()
            _update_task_log_vectorization(db, task_celery_id, TaskStatusEnum.FAILURE, error_info=err_msg)
        raise self.retry(exc=conn_err) # Celery retry for connection issues
    except Exception as e: # Catch-all for other unexpected errors
        err_msg = f"Unhandled error in vectorize task for metadata_id {metadata_id}: {type(e).__name__} - {str(e)}"
        print(f"{task_id_log} {err_msg}")
        if db:
            db.rollback()
            try:
                _update_task_log_vectorization(db, task_celery_id, TaskStatusEnum.FAILURE, error_info=err_msg)
            except ValueError: # If PENDING log was missing
                 print(f"{task_id_log} CRITICAL: Could not update TaskLog to FAILURE as PENDING log was likely missing for task {task_celery_id}.")
            except Exception as log_e:
                 print(f"{task_id_log} Further error trying to update TaskLog to FAILURE for task {task_celery_id}: {log_e}")
        raise self.retry(exc=e) # Celery default retry for other unhandled exceptions
    finally:
        if milvus_handler: # MilvusHandler might not be initialized if connection fails early
            milvus_handler.disconnect()
        if db:
            db.close()
        print(f"[VectorizeTask {task_celery_id}] Finished processing for metadata_id: {metadata_id}")


# --- Celery Beat Scheduler Task ---
# To enable this, you'll need to configure Celery Beat,
# for example, in your celery_worker/celery_setup.py:
#
# from celery.schedules import crontab
# app.conf.beat_schedule = {
#     'schedule-pending-vectorizations-every-5-minutes': {
#         'task': 'tasks.image.schedule_pending_vectorizations', # Make sure this name matches below
#         'schedule': crontab(minute='*/5'), # Example: every 5 minutes
#     },
# }

@celery_app.task(name="tasks.image.schedule_pending_vectorizations")
def schedule_pending_vectorizations_task():
    """
    Periodically scans for ImageMetadata records that are not yet vectorized
    and dispatches vectorize_image_and_store_task for each.
    """
    task_name_log = f"[SchedulerTask Vectorization - {datetime.utcnow().isoformat()}]" # More specific name
    print(f"{task_name_log} Scanning for images pending vectorization...")
    db = None # Initialize db
    dispatched_count = 0
    # Define a batch limit, can be made configurable from settings
    BATCH_LIMIT_VECTORIZATION_SCHEDULER = 100 
    
    try:
        db = SessionLocal()
        pending_images = db.query(ImageMetadata).filter(
            ImageMetadata.is_vectorized == False,
            ImageMetadata.is_emotion_tagged == True, 
            ImageMetadata.is_semantic_tagged == True
        ).limit(BATCH_LIMIT_VECTORIZATION_SCHEDULER).all()

        if not pending_images:
            print(f"{task_name_log} No images found pending vectorization.")
            return {"status": "success", "message": "No images pending vectorization.", "dispatched_count": 0}
        
        print(f"{task_name_log} Found {len(pending_images)} image(s) for vectorization retry (limit {BATCH_LIMIT_VECTORIZATION_SCHEDULER}).")
        
        for image_meta in pending_images:
            custom_task_id = uuid.uuid4().hex
            task_log_created = False
            celery_task_submitted = False
            
            try:
                # 1. Create PENDING TaskLog entry
                new_task_log_entry = TaskLog(
                    task_id=custom_task_id,
                    task_name=vectorize_image_and_store_task.name, # Actual name of the task being dispatched
                    task_type=TaskTypeEnum.RETRY_VECTORIZATION, # This is a retry/scheduled vectorization
                    status=TaskStatusEnum.PENDING,
                    image_metadata_id=image_meta.id,
                    created_at=datetime.utcnow()
                )
                db.add(new_task_log_entry)
                # Note: We are not incrementing a specific retry counter on ImageMetadata for vectorization here,
                # unlike the tagging retry. is_vectorized=False is the main condition.
                # If a retry limit for vectorization is needed, a similar counter should be added to ImageMetadata.
                db.commit()
                task_log_created = True
                print(f"{task_name_log} Successfully created PENDING TaskLog {custom_task_id} for metadata_id: {image_meta.id}.")

                # 2. Dispatch Celery task
                # TODO: Replace 'default_queue_for_retry_vectorization' with actual queue name
                target_queue = 'retry_vectorization_queue' # Placeholder
                vectorize_image_and_store_task.apply_async(
                    args=[image_meta.id],
                    task_id=custom_task_id,
                    queue=target_queue
                )
                celery_task_submitted = True
                print(f"{task_name_log} Successfully dispatched Celery task {custom_task_id} to queue '{target_queue}' for metadata_id: {image_meta.id}.")
                dispatched_count += 1

            except SQLAlchemyError as e_sql:
                db.rollback()
                print(f"{task_name_log} SQLAlchemyError for metadata_id {image_meta.id} (custom_task_id: {custom_task_id}): {e_sql}. TaskLog created: {task_log_created}, Celery task submitted: {celery_task_submitted}.")
            except Exception as e_dispatch:
                # Catches Celery dispatch errors or other unexpected issues
                # db.rollback() might be needed if any DB session interaction happened before commit and then this error occurred.
                # If task_log_created is True, it means DB commit for TaskLog was successful.
                print(f"{task_name_log} Exception dispatching Celery task for metadata_id {image_meta.id} (custom_task_id: {custom_task_id}): {e_dispatch}. TaskLog created: {task_log_created}.")
                if task_log_created: # Orphan TaskLog
                    print(f"{task_name_log} CRITICAL: Orphan PENDING TaskLog {custom_task_id} may exist for metadata_id {image_meta.id}.")

        return {"status": "success", "dispatched_count": dispatched_count, "batch_limit": BATCH_LIMIT_VECTORIZATION_SCHEDULER}

    except Exception as e_outer:
        print(f"{task_name_log} General error in beat task: {e_outer}")
        if db: 
            db.rollback()
        return {"status": "error", "reason": str(e_outer), "dispatched_count": dispatched_count}
    finally:
        if db:
            db.close()
        print(f"{task_name_log} Finished scan. Processed {dispatched_count} tasks for retry dispatch.")
