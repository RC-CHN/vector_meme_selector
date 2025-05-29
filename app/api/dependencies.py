from fastapi import Header, HTTPException, status
from typing import Optional

from app.core.config import settings

async def verify_preshared_token(x_auth_token: Optional[str] = Header(None, alias="X-Auth-Token")):
    """
    Dependency to verify a preshared token in the X-Auth-Token header.
    """
    if not x_auth_token or x_auth_token != settings.API_PRESHARED_AUTH_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API token. Please include a valid X-Auth-Token header.",
            # headers={"WWW-Authenticate": "Bearer"}, # Bearer is usually for OAuth, not simple preshared.
                                                    # For preshared, a custom message is often enough.
        )
    # If token is valid, the dependency successfully completes.
    # No explicit return value is needed if it's just for validation.
