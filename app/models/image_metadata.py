from sqlalchemy import Column, Integer, String, Text, Boolean
from app.core.db import Base # Import Base from our db setup

class ImageMetadata(Base):
    __tablename__ = "image_metadata"

    id = Column(Integer, primary_key=True, index=True)
    sha256_hash = Column(String(64), unique=True, nullable=False, index=True) # SHA256 哈希值
    filename = Column(String(255), nullable=False, index=True) # 原始文件名
    minio_object_name = Column(String(255), unique=True, nullable=False, index=True) # MinIO中的UUID文件名
    minio_bucket_name = Column(String(100), nullable=False)
    content_type = Column(String(100), nullable=True)
    size_bytes = Column(Integer, nullable=True)
    
    # 两阶段打标结果
    emotion_tags_text = Column(Text, nullable=True) # 存储情感标签的JSON字符串
    semantic_tags_text = Column(Text, nullable=True) # 存储语义标签的JSON字符串

    # 状态标志位
    is_emotion_tagged = Column(Boolean, default=False, nullable=False)
    is_semantic_tagged = Column(Boolean, default=False, nullable=False)
    is_vectorized = Column(Boolean, default=False, nullable=False) # 预留给向量化

    # 新增字段，用于跟踪 Beat 任务发起的 LLM 打标重试次数
    llm_tagging_beat_retry_count = Column(Integer, default=0, nullable=False)

    # 可以添加更多字段，例如：
    # from sqlalchemy import DateTime, func
    # created_at = Column(DateTime(timezone=True), server_default=func.now())
    # updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    # user_id = Column(Integer, ForeignKey("users.id"), nullable=True) # 如果有用户概念

    def __repr__(self):
        return f"<ImageMetadata(id={self.id}, filename='{self.filename}', minio_object='{self.minio_object_name}')>"
