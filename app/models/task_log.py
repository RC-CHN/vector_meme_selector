from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func # For server-side default timestamps
import enum

from app.core.db import Base # Assuming your Base is accessible here

# Enum for task statuses
class TaskStatusEnum(str, enum.Enum):
    PENDING = "PENDING"
    STARTED = "STARTED"
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    RETRY = "RETRY" # Typically set by Celery itself if a task is retrying
    REVOKED = "REVOKED" # If you implement task revocation

# Enum for simplified task types, distinguishing initial and retry
class TaskTypeEnum(str, enum.Enum):
    INITIAL_TAGGING = "INITIAL_TAGGING"         # e.g., generate_llm_tags_task called after upload
    RETRY_TAGGING = "RETRY_TAGGING"             # e.g., generate_llm_tags_task called by retry_failed_tagging_beat_task
    INITIAL_VECTORIZATION = "INITIAL_VECTORIZATION" # e.g., vectorize_image_and_store_task called after initial tagging
    RETRY_VECTORIZATION = "RETRY_VECTORIZATION"   # e.g., vectorize_image_and_store_task called by schedule_pending_vectorizations_task
    # If Beat tasks themselves were logged (currently not planned for them to have separate TaskLog entries):
    # TAGGING_BEAT_SCHEDULER = "TAGGING_BEAT_SCHEDULER"
    # VECTORIZATION_BEAT_SCHEDULER = "VECTORIZATION_BEAT_SCHEDULER"

class TaskLog(Base):
    __tablename__ = "task_log"

    id = Column(Integer, primary_key=True, index=True) # Auto-incrementing ID for this log table
    task_id = Column(String(255), unique=True, index=True, nullable=False) # Celery's task UUID (will be custom generated)

    task_name = Column(String(255), nullable=True, index=True) # Name of the Celery task (e.g., "tasks.image.generate_llm_tags")
    
    # New field for simplified task type, distinguishing initial vs retry
    task_type = Column(SAEnum(TaskTypeEnum), nullable=False, index=True)

    status = Column(SAEnum(TaskStatusEnum), default=TaskStatusEnum.PENDING, nullable=False, index=True)
    
    # Timestamp for when the task log record was created (usually when PENDING)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # Timestamp for the last update to this task's status or info
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())
    # Timestamp for when the task started processing
    started_at = Column(DateTime(timezone=True), nullable=True)
    # Timestamp for when the task finished (success or failure)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    # Store result of successful task (e.g., JSON string) or error info for failed task
    result_info = Column(Text, nullable=True) 
    
    # Foreign key to link to the image being processed, if applicable
    image_metadata_id = Column(Integer, ForeignKey("image_metadata.id"), nullable=True, index=True)
    image_metadata = relationship("ImageMetadata") # Optional: define relationship

    def __repr__(self):
        return f"<TaskLog(task_id='{self.task_id}', task_type='{self.task_type}', status='{self.status}')>"
