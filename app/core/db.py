from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from .config import settings
import os

# Create the 'instance' directory if it doesn't exist and using SQLite in instance/
if "sqlite://" in settings.DATABASE_URL and "/instance/" in settings.DATABASE_URL:
    # Construct path relative to the project root or a known base directory
    # Assuming this db.py file is in app/core/
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    instance_path = os.path.join(project_root, "instance")
    os.makedirs(instance_path, exist_ok=True)
    
    # Adjust DATABASE_URL to be an absolute path if it's a relative SQLite path
    # This makes it more robust when running from different locations.
    if settings.DATABASE_URL.startswith("sqlite:///./"):
        db_file_path = settings.DATABASE_URL.replace("sqlite:///./", "")
        absolute_db_url = f"sqlite:///{os.path.join(project_root, db_file_path)}"
        SQLALCHEMY_DATABASE_URL = absolute_db_url
    else:
        SQLALCHEMY_DATABASE_URL = settings.DATABASE_URL
else:
    SQLALCHEMY_DATABASE_URL = settings.DATABASE_URL

# Create SQLAlchemy engine
# For SQLite, connect_args is needed to support multithreading as FastAPI is async
engine_args = {}
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine_args["connect_args"] = {"check_same_thread": False}

engine = create_engine(SQLALCHEMY_DATABASE_URL, **engine_args)

# Create a session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for declarative models
Base = declarative_base()

# Dependency to get DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def create_db_tables():
    """Creates all tables defined by models inheriting from Base."""
    # Import all models here before calling create_all
    # This ensures they are registered with SQLAlchemy's metadata
    from app.models.image_metadata import ImageMetadata # Existing model
    from app.models.task_log import TaskLog       # New TaskLog model
    Base.metadata.create_all(bind=engine)
    print(f"Database tables created (if not exist) for URL: {SQLALCHEMY_DATABASE_URL}")
