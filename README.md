# Vector Meme Selector (向量表情包选择器)

本项目是一个 FastAPI 后端服务，通过 LLM 对图片进行智能打标（情感、语义），将标签向量化存入 Milvus，并支持通过自然语言进行向量搜索，快速找到相关图片。

## 核心算法流程

1.  **图片上传**: 用户上传图片，系统存入 MinIO，并在 PostgreSQL 中记录元数据。
2.  **智能打标 (Celery)**: 异步任务调用 LLM API 对图片进行情感和语义标签提取，结果存入 PostgreSQL。
3.  **标签向量化 (Celery)**: 将提取的文本标签通过外部 Embedding API 转换为向量，存入 Milvus 的对应集合中（情感向量集、语义向量集）。
4.  **自然语言搜索**:
    *   用户输入文本被转换为查询向量。
    *   在 Milvus 的情感和语义向量集合中并行搜索相似图片。
    *   融合两个集合的搜索结果并加权打分，返回最匹配的图片（提供 MinIO 临时访问 URL）。
5.  **任务追踪与重试**: 通过 `TaskLog` 表追踪异步任务状态，Celery Beat 任务负责失败任务的自动重试。

## 项目组件结构

*   **`app/`**: FastAPI 应用 (API路由, 核心配置, 数据库模型, 服务逻辑, 工具函数)
*   **`celery_worker/`**: Celery 异步任务 (图片打标, 标签向量化, 失败重试 Beat 任务)
*   **`nginx/`**: Nginx 配置 (MinIO 反向代理)
*   **`docker-compose.deploy.yml`**: 生产环境 Docker Compose 部署文件。
*   **`Dockerfile`**: 应用 Docker 镜像构建文件。
*   **`.env.example`**: 环境变量配置示例。

## 使用 `docker-compose.deploy.yml` 一键部署

1.  **环境准备**: 安装 Docker 和 Docker Compose。
2.  **配置环境变量**: 复制 `.env.example` 为 `.env` 并修改其中的配置项（数据库、MinIO、LLM/Embedding API密钥、端口等）。
3.  **启动服务**:
    ```bash
    docker-compose -f docker-compose.deploy.yml up --build -d
    ```
4.  **访问**: 根据 `.env` 中配置的端口访问 FastAPI 应用、MinIO 等。
5.  **日志与停止**:
    ```bash
    docker-compose -f docker-compose.deploy.yml logs -f [service_name] # 查看日志
    docker-compose -f docker-compose.deploy.yml down [-v]             # 停止服务 [可选-v删除数据卷]
    ```

**主要服务组件**: `app` (FastAPI & Celery), `postgres_db`, `minio`, `redis`, `milvus_standalone`, `nginx`。
