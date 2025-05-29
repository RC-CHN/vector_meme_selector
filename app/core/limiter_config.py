# app/core/limiter_config.py
from slowapi import Limiter
from slowapi.util import get_remote_address

# Create a limiter instance.
# We use get_remote_address as the key_func, meaning limits are applied per IP by default.
# No default limits are set here at the global Limiter instance level,
# as we plan to apply limits specifically to routes using decorators.
limiter = Limiter(key_func=get_remote_address)

# If you wanted a global default limit for all routes instrumented, you could add:
# default_limits=["1000/minute"] # Example global limit
# But the current requirement is to limit only the search API specifically.
