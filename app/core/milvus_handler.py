from pymilvus import connections, utility, Collection, DataType, FieldSchema, CollectionSchema
from app.core.config import settings
from typing import List, Dict, Any

class MilvusHandler:
    def __init__(self, alias: str = "default"):
        self.alias = alias
        self.collection_emotion_tags_name = "emotion_tags_collection"
        self.collection_semantic_tags_name = "semantic_tags_collection"
        self.vector_dim = 1024  # bge-m3 outputs 1024 dimensions
        # Max length for a UUID hex (32) + dot (1) + common extension (e.g., jpeg, 4) = 37. Add buffer.
        self.pk_max_length = 64 # Max length for the VARCHAR primary key (MinIO object name)

        try:
            print(f"Attempting to connect to Milvus at {settings.MILVUS_HOST}:{settings.MILVUS_PORT} with alias '{self.alias}'")
            connections.connect(
                alias=self.alias,
                host=settings.MILVUS_HOST,
                port=str(settings.MILVUS_PORT) # Port needs to be a string for connections.connect
            )
            print(f"Successfully connected to Milvus with alias '{self.alias}'.")
        except Exception as e:
            print(f"Error connecting to Milvus: {e}")
            raise ConnectionError(f"Failed to connect to Milvus: {e}")

    def _create_collection_schema(self, collection_description: str) -> CollectionSchema:
        # Primary key field using MinIO object name (UUID string)
        minio_object_name_field = FieldSchema(
            name="minio_object_name", # Field name in Milvus
            dtype=DataType.VARCHAR,
            is_primary=True,
            auto_id=False, # We will provide our own unique MinIO object names
            max_length=self.pk_max_length,
            description="Primary key, MinIO object name (UUID.ext)"
        )
        embedding_vector_field = FieldSchema(
            name="embedding_vector",
            dtype=DataType.FLOAT_VECTOR,
            dim=self.vector_dim,
            description="Embedding vector"
        )
        schema = CollectionSchema(
            fields=[minio_object_name_field, embedding_vector_field],
            description=collection_description,
            enable_dynamic_field=False
        )
        return schema

    def ensure_collection_exists(self, collection_name: str, description: str):
        try:
            if not utility.has_collection(collection_name, using=self.alias):
                print(f"Collection '{collection_name}' does not exist. Creating...")
                schema = self._create_collection_schema(description)
                collection = Collection(
                    name=collection_name,
                    schema=schema,
                    using=self.alias,
                )
                print(f"Collection '{collection_name}' created successfully.")
                
                index_params = {
                    "metric_type": "IP",
                    "index_type": "HNSW",
                    "params": {"M": 16, "efConstruction": 200},
                }
                collection.create_index(
                    field_name="embedding_vector",
                    index_params=index_params
                )
                collection.load()
                print(f"Index created and collection '{collection_name}' loaded.")
            else:
                print(f"Collection '{collection_name}' already exists.")
                collection = Collection(collection_name, using=self.alias)
                if not collection.has_index(index_name=""): # Check for any index on the vector field
                    print(f"Collection '{collection_name}' exists but vector field has no index. Creating index...")
                    index_params = {
                        "metric_type": "IP",
                        "index_type": "HNSW",
                        "params": {"M": 16, "efConstruction": 200},
                    }
                    collection.create_index(field_name="embedding_vector", index_params=index_params)
                    print(f"Index created for collection '{collection_name}'.")
                collection.load() # Ensure collection is loaded
                print(f"Collection '{collection_name}' loaded.")

        except Exception as e:
            print(f"Error ensuring collection '{collection_name}': {e}")
            raise

    def ensure_all_collections_exist(self):
        """Ensures both emotion and semantic tag collections exist."""
        self.ensure_collection_exists(
            self.collection_emotion_tags_name,
            "Collection for storing emotion tag embeddings"
        )
        self.ensure_collection_exists(
            self.collection_semantic_tags_name,
            "Collection for storing semantic tag embeddings"
        )

    def insert_vectors(self, collection_name: str, minio_object_names: List[str], vectors: List[List[float]]) -> List[Any]:
        if not minio_object_names or not vectors:
            print("Warning: MinIO object names or vectors list is empty. Nothing to insert.")
            return []
        if len(minio_object_names) != len(vectors):
            raise ValueError("Length of MinIO object names list must match length of vectors list.")

        try:
            collection = Collection(collection_name, using=self.alias)
            data_to_insert = [minio_object_names, vectors]
            
            print(f"Attempting to insert {len(minio_object_names)} vectors into '{collection_name}'. First name: {minio_object_names[0] if minio_object_names else 'N/A'}")
            mutation_result = collection.insert(data_to_insert)
            collection.flush() 
            print(f"Successfully inserted {len(mutation_result.primary_keys)} vectors. PKs example: {mutation_result.primary_keys[:5]}")
            return mutation_result.primary_keys
        except Exception as e:
            print(f"Error inserting vectors into '{collection_name}': {e}")
            raise

    def search_vectors(self, collection_name: str, vector: List[float], top_k: int, search_params: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """
        Searches for similar vectors in the specified collection.

        Args:
            collection_name (str): The name of the collection to search in.
            vector (List[float]): The query vector.
            top_k (int): The number of most similar results to return.
            search_params (Dict[str, Any], optional): Additional search parameters for Milvus. 
                                                     Defaults to {"metric_type": "IP", "params": {"ef": 128}}.
                                                     'ef' (search_k for HNSW) should generally be > top_k.

        Returns:
            List[Dict[str, Any]]: A list of dictionaries, where each dictionary
                                  contains 'minio_object_name' and 'score' (raw IP distance).
        """
        if not vector:
            print(f"Warning: Query vector is empty for search in '{collection_name}'.")
            return []
        
        default_search_params = {
            "metric_type": "IP", # Metric type must match the one used at index creation
            "params": {"ef": max(top_k + 10, 128)}, # ef (search_k for HNSW) should be > top_k. Ensure a reasonable minimum.
        }
        current_search_params = search_params if search_params else default_search_params

        try:
            collection = Collection(collection_name, using=self.alias)
            
            # Ensure collection is loaded. This is important for search performance.
            # Milvus documentation suggests loading explicitly if not sure.
            # utility.load_state(collection_name, using=self.alias) might be useful,
            # or collection.load() if it's not automatically handled.
            # For now, assume ensure_collection_exists or initial load handles it.
            # If issues arise, explicit loading here might be needed.
            # print(f"DEBUG: Collection '{collection_name}' load state: {utility.load_state(collection_name, using=self.alias)}")
            # print(f"DEBUG: Collection '{collection_name}' num_entities: {collection.num_entities}")


            if collection.is_empty:
                 print(f"Warning: Collection '{collection_name}' is empty. Cannot perform search.")
                 return []
            
            # It's good practice to ensure the collection is loaded before searching
            # utility.wait_for_loading_complete(collection_name, using=self.alias)
            # collection.load() # Re-ensure loaded, though ensure_collection_exists should do it.

            print(f"Searching in '{collection_name}' with top_k={top_k}, params={current_search_params}")
            
            results = collection.search(
                data=[vector],  # Query vectors, a list of lists
                anns_field="embedding_vector",  # Field name of the vector column
                param=current_search_params,
                limit=top_k,
                expr=None,  # No filtering expression for now
                output_fields=['minio_object_name'],  # Fields to return in addition to distance
                consistency_level="Strong"  # Or "Bounded" for eventually consistent results
            )
            
            processed_results = []
            # Results is a list of Hit objects for each query vector. Since we send one query vector, results[0] is what we need.
            if results and results[0]:
                for hit in results[0]:
                    processed_results.append({
                        "minio_object_name": hit.entity.get('minio_object_name'),
                        "score": hit.distance  # This is the raw IP score from Milvus
                    })
            
            print(f"Search in '{collection_name}' found {len(processed_results)} results.")
            return processed_results
        except Exception as e:
            print(f"Error searching vectors in '{collection_name}': {e}")
            # Consider if specific Milvus errors should be handled differently or reraised
            raise # Re-raise the exception for the caller to handle

    def disconnect(self):
        try:
            connections.disconnect(self.alias)
            print(f"Successfully disconnected from Milvus alias '{self.alias}'.")
        except Exception as e:
            print(f"Error disconnecting from Milvus: {e}")

# Example usage:
# milvus_client = None
# def get_milvus_client():
#     global milvus_client
#     if milvus_client is None:
#         try:
#             milvus_client = MilvusHandler()
#             milvus_client.ensure_all_collections_exist()
#         except Exception as e:
#             print(f"Failed to initialize Milvus client: {e}")
#             milvus_client = None # Reset on failure
#     return milvus_client

# Call get_milvus_client() when needed in your app.
# Consider lifecycle management (disconnect on app shutdown).
