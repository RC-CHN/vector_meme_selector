from pydantic import BaseModel, HttpUrl
from typing import Optional, List, Any # Any for potential additional metadata fields

class SearchQuery(BaseModel):
    """
    Request schema for search queries.
    """
    query: str

class SearchResultItem(BaseModel):
    """
    Response schema for a single search result item.
    """
    image_url: HttpUrl # Presigned URL for direct access to the image
    score: float # Fused and normalized similarity score [0, 1]
    
    # Requested fields:
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    
    # Removed fields:
    # metadata_id: int
    # minio_object_name: str
    # filename: Optional[str] = None

    class Config:
        # orm_mode = True # Pydantic V1 style
        # For Pydantic V2, use model_config:
        from pydantic import ConfigDict
        model_config = ConfigDict(from_attributes=True)


class SearchResponse(BaseModel):
    """
    Overall response schema for the search API.
    """
    results: List[SearchResultItem]
    count: int
    
    # Optional: Add query details or pagination info if needed in the future
    # query_received: Optional[str] = None
    # page: Optional[int] = None
    # page_size: Optional[int] = None
    # total_pages: Optional[int] = None
