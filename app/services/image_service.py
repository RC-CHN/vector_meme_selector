import uuid
import io
import json # Added for json.dumps in error handling
import hashlib # For SHA256 hashing
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError # Import for DB error handling
from fastapi import UploadFile, HTTPException, status
import datetime # For TaskLog created_at

from app.core.config import settings
from app.core.minio_handler import MinioHandler
from app.models.image_metadata import ImageMetadata
from app.models.task_log import TaskLog, TaskStatusEnum, TaskTypeEnum # Import TaskTypeEnum
from app.schemas.image_schema import ImageUploadAcceptedResponse 
from celery_worker.tasks.image_tasks import generate_llm_tags_task
from typing import Dict, Any

class ImageService:

    @staticmethod
    def _is_allowed_file(filename: str) -> bool:
        return '.' in filename and \
               filename.rsplit('.', 1)[1].lower() in settings.ALLOWED_EXTENSIONS

    @staticmethod
    async def process_upload_image(db: Session, file: UploadFile) -> Dict[str, Any]: # Return type changed
        if not file.filename:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="没有选择文件")

        if not ImageService._is_allowed_file(file.filename):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不允许的文件类型")

        original_filename = file.filename # FastAPI UploadFile already handles secure_filename internally
        file_extension = original_filename.rsplit('.', 1)[1].lower()
        unique_object_name = f"{uuid.uuid4().hex}.{file_extension}"
        
        try:
            image_bytes = await file.read() # 读取文件内容
            
            # Calculate SHA256 hash
            sha256_hash = hashlib.sha256(image_bytes).hexdigest()
            print(f"Calculated SHA256 hash for {original_filename}: {sha256_hash}")

            # Check for existing image with the same hash
            existing_metadata = db.query(ImageMetadata).filter(ImageMetadata.sha256_hash == sha256_hash).first()
            if existing_metadata:
                print(f"Duplicate image detected for {original_filename} with hash {sha256_hash}. Existing metadata ID: {existing_metadata.id}")
                # Try to find the latest associated task
                latest_task = db.query(TaskLog).filter(TaskLog.image_metadata_id == existing_metadata.id).order_by(TaskLog.created_at.desc()).first()
                return {
                    "message": "图片已存在。",
                    "upload_status": "duplicate",
                    "filename": original_filename, # Or existing_metadata.filename
                    "metadata_id": existing_metadata.id,
                    "minio_object_name": existing_metadata.minio_object_name,
                    "task_id": latest_task.task_id if latest_task else None
                }

            # If not a duplicate, proceed with upload and new metadata creation
            image_stream = io.BytesIO(image_bytes)
            image_size = len(image_bytes)
            content_type = file.content_type or f"image/{file_extension}" # Fallback content type

            # 1. 上传到 MinIO
            print(f"正在上传 {original_filename} (hash: {sha256_hash}) 为 {unique_object_name} 到 MinIO 存储桶 {settings.MINIO_BUCKET_NAME}...")
            minio_client = MinioHandler.get_client()
            if not minio_client:
                 raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MinIO 服务不可用")

            MinioHandler.upload_file(
                bucket_name=settings.MINIO_BUCKET_NAME,
                object_name=unique_object_name,
                data_stream=image_stream, # Pass the BytesIO stream
                length=image_size,
                content_type=content_type
            )
            print(f"成功上传到 MinIO: {unique_object_name}")

            # 2. 准备 ImageMetadata 对象 (但不立即提交)
            print(f"正在为 {original_filename} (hash: {sha256_hash}) 准备元数据对象...")
            db_metadata = ImageMetadata(
                sha256_hash=sha256_hash, # Add the hash
                filename=original_filename,
                minio_object_name=unique_object_name,
                minio_bucket_name=settings.MINIO_BUCKET_NAME,
                content_type=content_type,
                size_bytes=image_size,
                is_emotion_tagged=False,
                is_semantic_tagged=False,
                is_vectorized=False
            )

            # 3. Create ImageMetadata and PENDING TaskLog, then dispatch Celery task
            custom_celery_task_id = uuid.uuid4().hex
            
            db.add(db_metadata) # Add metadata to session first to ensure it's persisted with TaskLog

            # Create PENDING TaskLog entry
            db_task_log = TaskLog(
                task_id=custom_celery_task_id,
                task_name=generate_llm_tags_task.name,
                task_type=TaskTypeEnum.INITIAL_TAGGING, # Specify task type
                status=TaskStatusEnum.PENDING,
                image_metadata_id=None, # Will be set after db_metadata gets an ID
                created_at=datetime.datetime.utcnow() 
            )
            db.add(db_task_log)

            try:
                # Commit ImageMetadata and initial TaskLog (without image_metadata_id yet)
                # We need image_metadata.id first.
                db.flush() # Assigns IDs without full commit
                if db_metadata.id is None: # Should not happen if flush worked
                    db.rollback()
                    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to obtain metadata ID before task dispatch.")

                db_task_log.image_metadata_id = db_metadata.id # Now set the foreign key
                db.commit() # Commit both ImageMetadata and fully populated TaskLog
                print(f"Successfully committed ImageMetadata (ID: {db_metadata.id}) and PENDING TaskLog (Task ID: {custom_celery_task_id}) to DB.")

            except SQLAlchemyError as db_exc:
                db.rollback()
                print(f"Database error during initial metadata/TaskLog commit for {original_filename}: {db_exc}")
                raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error saving image metadata or task log.")
            
            # If DB commit was successful, dispatch Celery task
            try:
                target_queue_initial_tagging = 'initial_tagging_queue' # Placeholder
                generate_llm_tags_task.apply_async(
                    args=[db_metadata.id],
                    task_id=custom_celery_task_id, # Use the same custom ID
                    queue=target_queue_initial_tagging
                )
                print(f"Successfully dispatched Celery task {custom_celery_task_id} to queue '{target_queue_initial_tagging}' for metadata_id {db_metadata.id}.")
                
                return {
                    "message": "图片已成功上传，后台智能分析和打标任务已提交。",
                    "upload_status": "new",
                    "filename": original_filename,
                    "minio_object_name": unique_object_name,
                    "metadata_id": db_metadata.id,
                    "task_id": custom_celery_task_id
                }
            except Exception as celery_dispatch_exc:
                # CRITICAL: TaskLog is PENDING in DB, but Celery dispatch failed.
                print(f"CRITICAL: Failed to dispatch Celery task {custom_celery_task_id} for metadata_id {db_metadata.id} after DB commit: {celery_dispatch_exc}")
                # Attempt to update TaskLog to FAILURE to avoid orphan PENDING log
                try:
                    # Re-fetch task_log for update in a new state if needed, or use db_task_log if session is still valid
                    # For simplicity, assume db_task_log is still usable or re-fetch.
                    # This is a best-effort to mark the log. A separate cleanup for orphans is more robust.
                    task_log_to_fail = db.query(TaskLog).filter(TaskLog.task_id == custom_celery_task_id).first()
                    if task_log_to_fail:
                        task_log_to_fail.status = TaskStatusEnum.FAILURE
                        task_log_to_fail.result_info = json.dumps({"error": "Celery task dispatch failed after DB commit.", "exception": str(celery_dispatch_exc)})
                        task_log_to_fail.finished_at = datetime.datetime.utcnow()
                        db.commit()
                        print(f"Updated orphan TaskLog {custom_celery_task_id} to FAILURE due to Celery dispatch error.")
                    else:
                        print(f"Could not find TaskLog {custom_celery_task_id} to mark as FAILURE after dispatch error (this should not happen).")
                except Exception as log_update_exc:
                    db.rollback()
                    print(f"Further error trying to update orphan TaskLog {custom_celery_task_id} to FAILURE: {log_update_exc}")

                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="图片上传和元数据记录成功，但后台处理任务提交失败。请联系管理员。"
                )

        except HTTPException: # Re-raise known HTTPExceptions
            raise
        except ConnectionError as conn_err: # Specific for MinIO client not available during upload
             print(f"MinIO 连接错误 (上传阶段): {conn_err}")
             raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"存储服务连接错误: {conn_err}")
        except Exception as e:
            # General exception during the process
            if 'db' in locals() and db.is_active:
                 db.rollback()
            print(f"处理图片 {original_filename} 时发生意外错误: {e}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"处理图片时发生内部错误: {str(e)}")
