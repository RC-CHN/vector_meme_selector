from fastapi import APIRouter, Depends, HTTPException, Body, Request
from sqlalchemy.orm import Session
from typing import List

from app.core.db import get_db
from app.api.dependencies import verify_preshared_token
from app.services.search_service import SearchService
from app.schemas.search_schema import SearchQuery, SearchResponse, SearchResultItem
from app.core.limiter_config import limiter # Import the limiter instance
from app.core.config import settings # Import settings for the rate limit string

router = APIRouter()

@router.post(
    "/search",
    response_model=SearchResponse,
    summary="使用自然语言搜索图片",
    description="使用自然语言生成embeddings, "
                "在情绪和实体两个集合内进行向量搜索, "
                "加权打分并返回图片的presigned URLs.",
    dependencies=[Depends(verify_preshared_token)] # Protect the endpoint
)
@limiter.limit(settings.API_SEARCH_RATE_LIMIT) # Apply rate limiting per IP
async def search_images(
    request: Request, # Added Request parameter for slowapi
    search_query: SearchQuery = Body(..., description="The natural language query string."),
    db: Session = Depends(get_db)
):
    """
    Endpoint to perform synchronous vector search based on a natural language query.
    
    - **query**: The text string to search for.
    
    Returns a list of search results, each including a presigned URL to the image
    and a fused similarity score.
    """
    if not search_query.query or not search_query.query.strip():
        raise HTTPException(status_code=400, detail="Search query cannot be empty.")

    try:
        search_service = SearchService(db=db)
        search_results_response = await search_service.perform_search(query_text=search_query.query)
        
        if not search_results_response.results:
            # It's not an error if no results are found, just return empty list as per schema
            print(f"Search API: No results found for query: '{search_query.query}'")
        
        return search_results_response
        
    except ConnectionError as ce:
        # Specific error for Milvus/MinIO connection issues if raised by handlers
        print(f"Search API Error: Connection error - {ce}")
        raise HTTPException(status_code=503, detail=f"Service unavailable: Connection error - {str(ce)}")
    except ValueError as ve:
        # E.g., if embedding generation fails critically or other input validation
        print(f"Search API Error: Value error - {ve}")
        raise HTTPException(status_code=400, detail=f"Bad request: {str(ve)}")
    except Exception as e:
        # Catch-all for other unexpected errors
        print(f"Search API Error: Unexpected error during search for query '{search_query.query}': {type(e).__name__} - {e}")
        # Log the full traceback for debugging in a real application
        # import traceback
        # traceback.print_exc()
        raise HTTPException(status_code=500, detail="An internal server error occurred during the search.")

# Note: This new router needs to be included in the main FastAPI application
# in app/main.py (or wherever the main app instance and routers are configured).
# Example in app/main.py:
# from app.api.v1 import search_routes as search_api_v1
# app.include_router(search_api_v1.router, prefix="/api/v1", tags=["Search V1"])
