# celery_worker/celery_setup.py
from celery import Celery
from celery.schedules import crontab # Import crontab
# We need to ensure that the 'app' module can be found by Python's import system
# when Celery worker starts. This might require setting PYTHONPATH appropriately
# or ensuring the worker is started from the project root.
from app.core.config import settings # Assuming PYTHONPATH is set up for worker

# Create Celery application instance
celery_app = Celery(
    "vector_meme_selector_tasks", # A unique name for your Celery application
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    # Tasks will be auto-discovered in modules listed here.
    # Adjust paths according to the new structure.
    include=[
        'celery_worker.tasks.image_tasks',
        'celery_worker.tasks.vectorization_tasks', # Added vectorization tasks
    ]
)

# Optional Celery configuration
celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone=getattr(settings, 'APP_TIMEZONE', 'Asia/Shanghai'), # Use getattr for safety
    enable_utc=True,
    task_acks_late=True, # Enable for long-running tasks
    broker_heartbeat=30, # Send heartbeat every 30 seconds
    broker_heartbeat_checkrate=10, # Broker checks heartbeat, considers lost after 2*30s = 60s
    # worker_concurrency=4, # Concurrency is set per worker in start_dev_services.sh
)

# Define queues
from kombu import Queue, Exchange

# Define a default exchange (optional, but good practice for clarity)
default_exchange = Exchange('tasks', type='direct')

celery_app.conf.task_queues = (
    Queue('default', default_exchange, routing_key='default'),
    Queue('beat_scheduler_tasks_queue', default_exchange, routing_key='beat.scheduler'),
    Queue('initial_tagging_queue', default_exchange, routing_key='tagging.initial'),
    Queue('retry_tagging_queue', default_exchange, routing_key='tagging.retry'),
    Queue('initial_vectorization_queue', default_exchange, routing_key='vectorization.initial'),
    Queue('retry_vectorization_queue', default_exchange, routing_key='vectorization.retry')
)
celery_app.conf.task_default_queue = 'default'
celery_app.conf.task_default_exchange = 'tasks' # Name of the default exchange
celery_app.conf.task_default_routing_key = 'default'


# Remove static task_routes. Routing will be handled by:
# 1. `apply_async(..., queue='...')` for dynamically dispatched tasks.
# 2. `options={'queue': '...'}` in beat_schedule for Beat-scheduled tasks.
# 3. Default queue for any other tasks not explicitly routed.
celery_app.conf.task_routes = None # Clear any existing static routes or comment them out

# Celery Beat Schedule
celery_app.conf.beat_schedule = {
    'schedule-pending-vectorizations-regularly': { # Renamed for clarity
        'task': 'tasks.image.schedule_pending_vectorizations',
        'schedule': crontab(minute='*/15'),  # Example: every 15 minutes
        'options': {'queue': 'beat_scheduler_tasks_queue'} # Route this Beat task
    },
    'retry-failed-llm-tags-regularly': { # Renamed for clarity
        'task': 'tasks.image.retry_failed_tagging_beat',
        'schedule': crontab(minute='*/30'),  # Example: every 30 minutes
        'options': {'queue': 'beat_scheduler_tasks_queue'} # Route this Beat task
    },
}


# Example of how to customize task base class for shared logic (e.g., DB session)
# from app.core.db import SessionLocal
# class BaseTaskWithDB(celery_app.Task):
#     _db = None
#
#     @property
#     def db(self):
#         if self._db is None:
#             self._db = SessionLocal()
#         return self._db
#
#     def after_return(self, status, retval, task_id, args, kwargs, einfo):
#         super().after_return(status, retval, task_id, args, kwargs, einfo)
#         if self._db is not None:
#             self._db.close()
#             self._db = None
#
# celery_app.Task = BaseTaskWithDB # Uncomment to use this custom base task

if __name__ == '__main__':
    # This entry point is useful if you want to run the worker as:
    # python -m celery_worker.celery_setup worker -l info
    # However, the standard `celery -A celery_worker.celery_setup.celery_app worker ...` is more common.
    # To run beat: celery -A celery_worker.celery_setup.celery_app beat -l info
    celery_app.start()
