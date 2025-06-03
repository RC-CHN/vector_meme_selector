from pydantic_settings import BaseSettings
from typing import Set, Optional

class Settings(BaseSettings):
    # FastAPI App settings
    APP_NAME: str = "Vector Meme Selector API"
    APP_VERSION: str = "0.1.0"
    DEBUG_MODE: bool = True # Set to False in production
    SERVER_HOST: str = "127.0.0.1" # Host for the Uvicorn server
    SERVER_PORT: int = 8000       # Port for the Uvicorn server

    # MinIO Configuration
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "YOUR_MINIO_ACCESS_KEY"
    MINIO_SECRET_KEY: str = "YOUR_MINIO_SECRET_KEY"
    MINIO_BUCKET_NAME: str = "images"
    MINIO_USE_SSL: bool = False

    # SQL Database Configuration (SQLAlchemy)
    DATABASE_URL: str = "sqlite:///./instance/metadata.db" # For sync SQLAlchemy

    # LLM API Configuration (for Tagging)
    LLM_API_KEY: str = "YOUR_LLM_API_KEY" # Should be set in .env
    LLM_API_BASE_URL: str = "YOUR_LLM_API_BASE_URL" # Should be set in .env
    LLM_MODEL_NAME: str = "gemini-2.5-flash-preview-04-17-nothinking"
    LLM_REQUEST_TIMEOUT: int = 100

    # Embedding Model Configuration (for Vectorization)
    EMBEDDING_API_BASE_URL: str = "YOUR_EMBEDDING_API_BASE_URL" # e.g., your bge-m3 proxy
    EMBEDDING_API_KEY: str = "YOUR_EMBEDDING_API_KEY" # API key for the embedding service
    EMBEDDING_MODEL_NAME: str = "bge-m3" # Default embedding model
    EMBEDDING_DIMENSION: int = 1024 # Expected dimension for embeddings (e.g., bge-m3 outputs 1024)
    EMBEDDING_REQUEST_TIMEOUT: int = 30 # Timeout for embedding requests

    # --- Prompts for Two-Stage Tagging ---
    # Emotion Tags
    LLM_SYSTEM_PROMPT_EMOTION_CN: str = """你是一位专注于分析图像情感色彩和情绪氛围的AI专家。
你的任务是分析提供的图片并生成描述其主要情感、情绪或氛围的中文标签。
你必须严格遵守以下指令：
1.  你的整个回复必须且只能是一个有效的JSON对象。
2.  不要在JSON对象之前或之后包含任何其他文本、解释、问候、确认或致歉。
3.  不要使用任何Markdown标记，例如 ```json ... ``` 或 ```。直接输出纯JSON文本。
4.  揣摩图片的深层语义，综合文字和已知的亚文化meme,揣摩实际使用场景和图片直接表达的有什么不同等各方面信息来给出情感色彩，而不是仅仅直接观察图片。
JSON对象应包含一个名为 "emotion_tags" 的键，其值为一个字符串数组。数组中应包含5-7个最相关的简体中文情感标签。
例如:
{
  "emotion_tags": ["喜悦", "活泼", "温暖"]
}
或者:
{
  "emotion_tags": ["悲伤", "孤独"]
}
"""
    LLM_USER_PROMPT_EMOTION_CN: str = """请为这张图片生成5-7个最能代表其情感色彩或情绪氛围的简体中文标签。
请严格按照系统指令输出纯JSON格式，专注于情感和氛围。"""
    LLM_MAX_TOKENS_EMOTION: int = 80

    # Semantic Tags
    LLM_SYSTEM_PROMPT_SEMANTIC_CN: str = """你是一位专业的图像内容分析和通用标签生成AI。
你的任务是分析提供的图片并生成描述其主要物体、场景、主题和风格的中文标签。不要包含情感类标签。
你必须严格遵守以下指令：
1.  你的整个回复必须且只能是一个有效的JSON对象。
2.  不要在JSON对象之前或之后包含任何其他文本、解释、问候、确认或致歉。
3.  不要使用任何Markdown标记，例如 ```json ... ``` 或 ```。直接输出纯JSON文本。
4.  如果图片中包含直接的文字内容，那文字本身的原始内容也将作为额外的标签写入，不包含在10-15个的计数内。
5.  如果图片中包含人物，请对人物的动作进行具体描述，不要仅仅使用“手势”“动作”这样宽泛的描述。
JSON对象应包含一个名为 "semantic_tags" 的键，其值为一个字符串数组。数组中应包含10-15个相关的简体中文语义标签。
例如:
{
  "semantic_tags": ["猫", "书桌", "电脑", "室内", "工作学习", "宠物"]
}
"""
    LLM_USER_PROMPT_SEMANTIC_CN: str = """请为这张图片生成10-15个描述其主要物体、场景、主题或风格的简体中文标签。
请严格按照系统指令输出纯JSON格式，不要包含情感类标签。"""
    LLM_MAX_TOKENS_SEMANTIC: int = 180
    
    LLM_DEBUG_MODE: bool = False

    # Allowed file extensions for upload
    ALLOWED_EXTENSIONS: Set[str] = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

    # Celery Configuration
    CELERY_BROKER_URL: str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/1"
    CELERY_TASK_POST_DELAY_SECONDS: int = 0
    CELERY_TASK_PENDING_TIMEOUT_SECONDS: int = 6 * 60 * 60  # Default: 6 hours
    CELERY_TASK_STARTED_TIMEOUT_SECONDS: int = 1 * 60 * 60  # Default: 1 hour

    # Milvus Configuration
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530

    # Search Configuration
    MINIO_PUBLIC_ENDPOINT: Optional[str] = None # If set, presigned URLs will be rebased to this. If None, URLs based on MINIO_ENDPOINT will be used.
    SEARCH_EMOTION_COLLECTION_NAME: str = "emotion_tags_collection"
    SEARCH_SEMANTIC_COLLECTION_NAME: str = "semantic_tags_collection"
    SEARCH_WEIGHT_EMOTION: float = 0.4
    SEARCH_WEIGHT_SEMANTIC: float = 0.6
    SEARCH_TOP_K_INDIVIDUAL: int = 20 # Number of results to fetch from each Milvus collection initially
    SEARCH_TOP_K_FUSED: int = 10 # Final number of results to return to the user after fusion
    PRESIGNED_URL_EXPIRATION_SECONDS: int = 3600 # Default: 1 hour

    # Beat task for retrying failed LLM tagging
    MAX_LLM_TAGGING_BEAT_RETRIES: int = 2 # Max times Beat task will retry a single image's tagging
    TAGGING_RETRY_BATCH_SIZE_PER_BEAT_RUN: int = 50 # How many failed items Beat task processes in one run

    # Celery Beat Schedules Configuration (intervals in minutes)
    BEAT_SCHEDULE_PENDING_VECTORIZATIONS_MINUTES: int = 15 # Default: every 15 minutes
    BEAT_SCHEDULE_RETRY_TAGGING_MINUTES: int = 30         # Default: every 30 minutes
    BEAT_SCHEDULE_MONITOR_TIMEOUTS_MINUTES: int = 15      # Default: every 15 minutes

    # API Preshared Authentication Token
    API_PRESHARED_AUTH_TOKEN: str = "YOUR_SECRET_TOKEN_NEEDS_TO_BE_SET_IN_ENV" # IMPORTANT: Override in .env with a strong secret

    # API Rate Limiting
    API_SEARCH_RATE_LIMIT: str = "200/minute" # Rate limit for the search endpoint
    API_IMAGE_UPLOAD_RATE_LIMIT: str = "30/minute" # Rate limit for image uploads
    API_TASK_STATUS_RATE_LIMIT: str = "100/minute" # Rate limit for task status checks

    # Celery LLM Tagging Task Specific Configuration
    CELERY_LLM_TAGGING_TASK_RATE_LIMIT: Optional[str] = "2/m"
    CELERY_LLM_TAGGING_TASK_MAX_RETRIES: int = 1
    CELERY_LLM_TAGGING_TASK_DEFAULT_RETRY_DELAY: int = 60 # in seconds

    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'
        extra = 'ignore'

settings = Settings()
