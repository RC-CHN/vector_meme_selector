import asyncio
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.milvus_handler import MilvusHandler
from app.core.minio_handler import MinioHandler
from app.utils.embedding_utils import get_text_embedding # Already handles normalization
from app.models.image_metadata import ImageMetadata
from app.schemas.search_schema import SearchResultItem, SearchResponse

class SearchService:
    def __init__(self, db: Session):
        self.db = db
        # MilvusHandler will be instantiated within perform_search to manage its lifecycle per call.

    def _normalize_ip_score(self, ip_score: float) -> float:
        """Converts an IP score (typically -1 to 1 for normalized vectors) to a [0, 1] similarity score."""
        # Ensure score is within expected IP range for normalized vectors before transformation
        # Clamping might be an option if scores can sometimes go slightly out of [-1, 1] due to precision
        # ip_score = max(-1.0, min(1.0, ip_score)) 
        return (ip_score + 1) / 2

    async def _search_collection_in_thread(
        self, 
        milvus_handler: MilvusHandler,
        collection_name: str, 
        query_vector: List[float], 
        top_k: int
    ) -> List[Dict[str, Any]]:
        """
        Helper to run synchronous Milvus search in a separate thread.
        Compatible with Python 3.8.
        """
        loop = asyncio.get_event_loop() # get_event_loop() is fine for 3.8
        try:
            # Milvus search is blocking, run in default executor (thread pool)
            results = await loop.run_in_executor(
                None, # Uses the default ThreadPoolExecutor
                milvus_handler.search_vectors, 
                collection_name, 
                query_vector, 
                top_k
            )
            return results
        except Exception as e:
            print(f"Error during threaded search in {collection_name}: {e}")
            return [] # Return empty list on error to allow other searches to proceed

    async def perform_search(self, query_text: str) -> SearchResponse:
        query_vector = get_text_embedding(text_to_embed=query_text)
        if not query_vector:
            print(f"SearchService: Failed to generate embedding for query: {query_text}")
            return SearchResponse(results=[], count=0)

        milvus_handler = None
        try:
            # It's generally better to manage MilvusHandler's lifecycle (connect/disconnect)
            # per operation or for a batch of operations if they are close together.
            milvus_handler = MilvusHandler() 
            # ensure_all_collections_exist can be time-consuming if collections don't exist.
            # For a search query, we assume collections are already there and loaded.
            # If not, the search in milvus_handler.search_vectors might fail or be slow.
            # Consider if ensure_all_collections_exist is critical path for every search.
            # milvus_handler.ensure_all_collections_exist() 

            # Perform searches in parallel using the helper
            emotion_results_task = self._search_collection_in_thread(
                milvus_handler,
                settings.SEARCH_EMOTION_COLLECTION_NAME,
                query_vector,
                settings.SEARCH_TOP_K_INDIVIDUAL
            )
            semantic_results_task = self._search_collection_in_thread(
                milvus_handler,
                settings.SEARCH_SEMANTIC_COLLECTION_NAME,
                query_vector,
                settings.SEARCH_TOP_K_INDIVIDUAL
            )

            raw_emotion_results, raw_semantic_results = await asyncio.gather(
                emotion_results_task,
                semantic_results_task,
                return_exceptions=True # Allows one task to fail without stopping the other
            )
            
            # Handle potential exceptions from gather
            if isinstance(raw_emotion_results, Exception):
                print(f"SearchService: Error in emotion search: {raw_emotion_results}")
                raw_emotion_results = []
            if isinstance(raw_semantic_results, Exception):
                print(f"SearchService: Error in semantic search: {raw_semantic_results}")
                raw_semantic_results = []

        finally:
            if milvus_handler:
                milvus_handler.disconnect() # Ensure disconnection

        # Process and fuse scores
        fused_scores: Dict[str, Dict[str, Any]] = {} 

        for res in raw_emotion_results:
            object_name = res.get("minio_object_name")
            raw_score = res.get("score")
            if object_name is None or raw_score is None:
                print(f"SearchService: Skipping invalid emotion result: {res}")
                continue
            norm_score = self._normalize_ip_score(raw_score)
            if object_name not in fused_scores:
                fused_scores[object_name] = {"emotion_score": 0.0, "semantic_score": 0.0}
            fused_scores[object_name]["emotion_score"] = norm_score

        for res in raw_semantic_results:
            object_name = res.get("minio_object_name")
            raw_score = res.get("score")
            if object_name is None or raw_score is None:
                print(f"SearchService: Skipping invalid semantic result: {res}")
                continue
            norm_score = self._normalize_ip_score(raw_score)
            if object_name not in fused_scores:
                fused_scores[object_name] = {"emotion_score": 0.0, "semantic_score": 0.0}
            fused_scores[object_name]["semantic_score"] = norm_score
        
        final_results_data = []
        for object_name, scores_data in fused_scores.items():
            fused_score = (scores_data.get("emotion_score", 0.0) * settings.SEARCH_WEIGHT_EMOTION) + \
                          (scores_data.get("semantic_score", 0.0) * settings.SEARCH_WEIGHT_SEMANTIC)
            final_results_data.append({
                "minio_object_name": object_name,
                "fused_score": fused_score
            })

        final_results_data.sort(key=lambda x: x["fused_score"], reverse=True)
        top_fused_results = final_results_data[:settings.SEARCH_TOP_K_FUSED]

        output_results: List[SearchResultItem] = []
        minio_handler_for_urls = MinioHandler() # Get client for generating URLs

        for item_data in top_fused_results:
            metadata = self.db.query(ImageMetadata).filter(ImageMetadata.minio_object_name == item_data["minio_object_name"]).first()
            if metadata:
                presigned_url = minio_handler_for_urls.generate_presigned_url( # Use classmethod directly
                    bucket_name=settings.MINIO_BUCKET_NAME,
                    object_name=metadata.minio_object_name
                )
                if presigned_url:
                    output_results.append(
                        SearchResultItem(
                            image_url=presigned_url,
                            score=round(item_data["fused_score"], 8), # Round score for cleaner output
                            content_type=metadata.content_type,
                            size_bytes=metadata.size_bytes
                            # Removed:
                            # metadata_id=metadata.id,
                            # minio_object_name=metadata.minio_object_name,
                            # filename=metadata.filename,
                        )
                    )
                else:
                    print(f"SearchService: Failed to generate presigned URL for {metadata.minio_object_name}")
            else:
                 print(f"SearchService: Metadata not found for minio_object_name {item_data['minio_object_name']}")

        return SearchResponse(results=output_results, count=len(output_results))
