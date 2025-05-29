from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, status, Request
from fastapi.responses import JSONResponse # Import JSONResponse
from sqlalchemy.orm import Session
from typing import Any, Dict
import json

from app.core.db import get_db
from app.api.dependencies import verify_preshared_token # <--- 导入新的依赖项
from app.services.image_service import ImageService
from app.schemas.image_schema import (
    ImageUploadAcceptedResponse,
    TaskStatusResponse,
    ErrorResponse,
    # TaggingResultDetail # This can be used to type the 'result' field if it's a success
)
# Celery app and AsyncResult might not be directly needed here if SQL is the source of truth
# from celery_worker.celery_setup import celery_app
# from celery.result import AsyncResult
from app.models.task_log import TaskLog, TaskStatusEnum # Import TaskLog model and Enum
from app.models.image_metadata import ImageMetadata # Import ImageMetadata model
from app.core.limiter_config import limiter # Import the limiter instance
from app.core.config import settings # Import settings for rate limit strings

router = APIRouter(
    prefix="/images",
    responses={
        status.HTTP_400_BAD_REQUEST: {"description": "错误的请求", "model": ErrorResponse},
        status.HTTP_404_NOT_FOUND: {"description": "未找到资源", "model": ErrorResponse},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "服务器内部错误", "model": ErrorResponse},
    }
)

@router.post(
    "/upload", 
    response_model=ImageUploadAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传图片进行异步处理和打标",
    description="接收图片文件，存储到MinIO，提交后台任务进行LLM打标，并记录任务。返回任务ID以便追踪。",
    dependencies=[Depends(verify_preshared_token)] # <--- 应用认证依赖
)
@limiter.limit(settings.API_IMAGE_UPLOAD_RATE_LIMIT) # Apply rate limit for image uploads
async def upload_image_endpoint(
    request: Request, # Added Request parameter for slowapi
    file: UploadFile = File(..., description="要上传的图片文件 (支持的格式: png, jpg, jpeg, gif, webp)"),
    db: Session = Depends(get_db)
):
    try:
        response_data = await ImageService.process_upload_image(db=db, file=file)
        # Check if the image was a duplicate
        if response_data.get("upload_status") == "duplicate":
            # For duplicates, return 200 OK with the response data
            return JSONResponse(content=response_data, status_code=status.HTTP_200_OK)
        
        # For new uploads, FastAPI will use the status_code from the decorator (202 ACCEPTED)
        return response_data
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        print(f"上传图片端点发生意外错误: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"处理图片时发生意外的服务器错误: {str(e)}"
        )

@router.get(
    "/task_status/{task_id}",
    response_model=TaskStatusResponse,
    summary="查询后台任务的状态和结果 (从SQL数据库)",
    description="根据任务ID从SQL数据库查询后台任务的执行状态、结果或其他相关信息。"
)
@limiter.limit(settings.API_TASK_STATUS_RATE_LIMIT) # Apply rate limit for task status checks
async def get_task_status_from_sql_endpoint(request: Request, task_id: str, db: Session = Depends(get_db)): # Added Request
    task_log_entry = db.query(TaskLog).filter(TaskLog.task_id == task_id).first()

    if not task_log_entry:
        # Option 1: Still check Celery's AsyncResult as a fallback for very recent tasks
        # celery_task_result = AsyncResult(task_id, app=celery_app)
        # if celery_task_result.state != 'PENDING': # PENDING often means unknown to Celery
        #     # ... (logic from previous Redis-based status check) ...
        # else:
        #     raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务ID {task_id} 的日志未找到，且任务不在活跃处理队列中。")
        # Option 2: SQL is the source of truth. If not in SQL, it's not found or wasn't logged.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"任务ID {task_id} 的日志记录未找到。")

    parsed_result = None
    if task_log_entry.result_info:
        try:
            parsed_result = json.loads(task_log_entry.result_info)
        except json.JSONDecodeError:
            parsed_result = {"raw_result_info": task_log_entry.result_info, "parsing_error": "Could not parse as JSON."}
    
    # If task was successful, try to fetch and include actual tags from ImageMetadata
    if task_log_entry.status == TaskStatusEnum.SUCCESS and isinstance(parsed_result, dict):
        metadata_id = parsed_result.get("metadata_id")
        if metadata_id:
            image_meta_record = db.query(ImageMetadata).filter(ImageMetadata.id == metadata_id).first()
            if image_meta_record:
                try:
                    if image_meta_record.emotion_tags_text:
                        parsed_result["emotion_tags"] = json.loads(image_meta_record.emotion_tags_text)
                    else:
                        parsed_result["emotion_tags"] = [] # Or None, depending on preference
                    
                    if image_meta_record.semantic_tags_text:
                        parsed_result["semantic_tags"] = json.loads(image_meta_record.semantic_tags_text)
                    else:
                        parsed_result["semantic_tags"] = [] # Or None
                except json.JSONDecodeError:
                    parsed_result["tags_parsing_error"] = "Failed to parse tags from ImageMetadata."
    
    return TaskStatusResponse(
        task_id=task_log_entry.task_id,
        status=task_log_entry.status.value, # Get string value from Enum
        result=parsed_result # This now potentially includes actual tags
    )

# Example of how you might list tasks (optional, for admin or debugging)
# @router.get("/tasks_log", summary="列出所有任务日志 (示例)")
# async def list_task_logs(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
#     tasks = db.query(TaskLog).order_by(TaskLog.created_at.desc()).offset(skip).limit(limit).all()
#     return tasks
