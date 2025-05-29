import asyncio # 新增
import httpx # 新增
from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.core.config import settings
from app.core.db import create_db_tables, engine # Import engine for potential advanced use
from app.api.v1 import image_routes # Import your API router
from app.api.v1 import search_routes as search_api_v1 # Import the new search router
from app.core.minio_handler import MinioHandler # To initialize on startup
from app.core.milvus_handler import MilvusHandler # For Milvus startup check
from app.core.limiter_config import limiter # Import the limiter instance
from slowapi.errors import RateLimitExceeded # Import the exception for the handler
from fastapi import Request # To type hint the request in the handler
from fastapi.responses import JSONResponse # To return a JSON response for rate limit errors

# Lifespan context manager for startup and shutdown events
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup events
    print("应用程序启动中...")
    print(f"加载配置: APP_NAME='{settings.APP_NAME}', DEBUG_MODE={settings.DEBUG_MODE}")
    
    # 1. 创建数据库表 (如果尚不存在)
    # 注意: 在生产环境中，通常使用 Alembic 等迁移工具管理数据库模式
    try:
        create_db_tables()
        print("数据库表检查/创建完成。")
    except Exception as e:
        print(f"数据库表创建失败: {e}")
        # 根据需要决定是否因数据库问题阻止应用启动

    # 2. 初始化 MinIO 客户端并检查存储桶
    # MinioHandler.get_client() 会在首次调用时执行此操作
    try:
        MinioHandler.get_client() # This will initialize and check bucket
        print("MinIO 客户端初始化并检查存储桶完成。")
    except Exception as e:
        print(f"MinIO 初始化失败: {e}")
        # 根据需要决定是否因 MinIO 问题阻止应用启动

    # 3. 初始化 Milvus 客户端并检查连接/集合
    milvus_handler_instance = None # To manage disconnect if needed
    try:
        print("Milvus 连接和集合检查中...")
        # Instantiating MilvusHandler attempts connection.
        # ensure_all_collections_exist performs further checks and setup.
        milvus_handler_instance = MilvusHandler() # This connects
        milvus_handler_instance.ensure_all_collections_exist() # This checks/creates collections
        # MilvusHandler already prints success/failure details internally.
        print("Milvus 连接和集合检查完成。")
    except ConnectionError as ce: # Specific error from MilvusHandler if connection fails
        print(f"Milvus 连接失败 (在启动检查期间): {ce}")
        # 根据需要决定是否因 Milvus 问题阻止应用启动
    except Exception as e:
        print(f"Milvus 初始化或集合检查失败: {e}")
        # 根据需要决定是否因 Milvus 问题阻止应用启动

    # 4. (可选) 启动后执行健康检查请求
    async def _perform_startup_healthcheck():
        # 等待一小段时间，确保服务器已启动并监听端口
        await asyncio.sleep(1) # 延迟1秒
        healthcheck_url = f"http://{settings.SERVER_HOST}:{settings.SERVER_PORT}/"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(healthcheck_url)
                if response.status_code == 200:
                    print(f"启动健康检查成功: GET {healthcheck_url} - Status {response.status_code}")
                else:
                    print(f"启动健康检查警告: GET {healthcheck_url} - Status {response.status_code} - Response: {response.text[:200]}")
        except httpx.RequestError as exc:
            print(f"启动健康检查失败: 无法连接到 {healthcheck_url} - Error: {exc}")
        except Exception as e:
            print(f"启动健康检查时发生意外错误: {e}")

    # 将健康检查作为后台任务调度，不阻塞启动流程
    # 注意: 这会在 lifespan 的 startup 部分完成后，由事件循环在稍后执行
    asyncio.create_task(_perform_startup_healthcheck())
    print("已调度启动后的健康检查任务。")

    print("应用程序启动完成。")
    yield
    # Shutdown events
    print("应用程序关闭中...")
    if hasattr(engine, 'dispose'): # SQLAlchemy engine
        engine.dispose()
        print("数据库连接池已关闭。")
    
    if milvus_handler_instance:
        try:
            milvus_handler_instance.disconnect()
            print("Milvus 连接已关闭。")
        except Exception as e:
            print(f"关闭 Milvus 连接时出错: {e}")
            
    print("应用程序已关闭。")

# Create FastAPI app instance with lifespan manager
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    debug=settings.DEBUG_MODE,
    lifespan=lifespan,
    # Możesz dodać inne parametry, np. description, docs_url, redoc_url
    # description="这是一个处理图片并进行 LLM 打标的 API 服务。",
    # docs_url="/api/docs",  # Swagger UI
    # redoc_url="/api/redoc" # ReDoc
)

# Make the limiter available in the app state
app.state.limiter = limiter

# Add an exception handler for RateLimitExceeded.
# This is the standard way to handle it when not using (or if unable to use) FastAPIInstrumentor.
@app.exception_handler(RateLimitExceeded)
async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429, # HTTP 429 Too Many Requests
        content={"detail": f"Rate limit exceeded: {exc.detail}"}
    )

# Include API routers
# 所有来自 image_routes 的路由都会有 /api/v1 的前缀 (在 image_routes.py 中定义了 /images, 所以完整路径是 /api/v1/images)
app.include_router(image_routes.router, prefix="/api/v1", tags=["Images"]) # Added a tag for consistency
app.include_router(search_api_v1.router, prefix="/api/v1", tags=["Search"])


@app.get("/", tags=["Root"], summary="API 根路径健康检查")
async def read_root():
    """
    一个简单的健康检查端点，确认 API 是否正在运行。
    """
    return {"message": f"欢迎使用 {settings.APP_NAME} v{settings.APP_VERSION}"}

# 如果你需要直接从这个文件运行 (例如，用于非常简单的本地测试，不推荐用于生产)
# if __name__ == "__main__":
#     import uvicorn
#     # 注意: Uvicorn 通常从命令行运行: uvicorn app.main:app --reload
#     uvicorn.run(app, host="0.0.0.0", port=8000)
