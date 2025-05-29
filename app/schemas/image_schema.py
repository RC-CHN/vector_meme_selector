from pydantic import BaseModel, Field
from typing import Optional, List, Any # Added Any

# Pydantic schema for response when an image is successfully processed
class ImageUploadResponse(BaseModel):
    message: str = "文件上传、打标并成功保存元数据。"
    filename: str
    minio_object_name: str
    minio_bucket: str
    metadata_id: int
    emotion_tags: Optional[List[str]] = Field(default_factory=list, description="情感标签列表")
    semantic_tags: Optional[List[str]] = Field(default_factory=list, description="语义标签列表")
    # Optional: include status flags if needed in response
    # is_emotion_tagged: bool
    # is_semantic_tagged: bool
    # is_vectorized: bool
    partial_tagging_info: Optional[str] = Field(default=None, description="部分打标成功时的提示信息")


    class Config:
        from_attributes = True 

# Schema for returning a list of image metadata (if you implement a GET endpoint later)
class ImageMetadataDetail(BaseModel):
    id: int
    filename: str
    minio_object_name: str
    minio_bucket_name: str
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    emotion_tags_text: Optional[str] = Field(default=None, description="情感标签 (原始JSON字符串)") # Or parse to List[str]
    semantic_tags_text: Optional[str] = Field(default=None, description="语义标签 (原始JSON字符串)") # Or parse to List[str]
    is_emotion_tagged: bool
    is_semantic_tagged: bool
    is_vectorized: bool
    # created_at: Optional[datetime] = None # If you add timestamps
    # updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

# Schema for error responses (optional, but good practice)
class ErrorResponse(BaseModel):
    detail: str

# Schema for the response when an image upload is accepted for async processing
class ImageUploadAcceptedResponse(BaseModel):
    message: str
    upload_status: str = Field(description="上传状态: 'new' 表示新上传, 'duplicate' 表示图片已存在")
    filename: str
    metadata_id: int # 对于 'new' 是新生成的ID, 对于 'duplicate' 是已存在的ID
    minio_object_name: Optional[str] = Field(default=None, description="MinIO中的对象名, 'new'时为新名称, 'duplicate'时为已存在的名称")
    task_id: Optional[str] = Field(default=None, description="Celery任务ID, 'new'时为新任务ID, 'duplicate'时可能为关联的最新任务ID或无")

# Schema for the result part of a successful tagging task
class TaggingResultDetail(BaseModel):
    status: str # e.g., "success"
    metadata_id: int
    emotion_tags_count: int
    semantic_tags_count: int

# Schema for querying task status
class TaskStatusResponse(BaseModel):
    task_id: str
    status: str  # Celery task states: PENDING, STARTED, SUCCESS, FAILURE, RETRY, REVOKED
    result: Optional[Any] = None # Can be TaggingResultDetail on success, or error info on failure
    # result: Optional[Union[TaggingResultDetail, Dict[str, Any]]] = None # More specific for success/failure
