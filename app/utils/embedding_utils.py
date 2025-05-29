import requests
import json
from typing import List, Optional
import numpy as np

from app.core.config import settings

# Constants are now primarily sourced from settings for runtime configuration
# EMBEDDING_MODEL_NAME = settings.EMBEDDING_MODEL_NAME (accessed via function default)
# REQUEST_TIMEOUT = settings.EMBEDDING_REQUEST_TIMEOUT (accessed via function default or directly)

def normalize_vector(vector: List[float]) -> List[float]:
    """
    Performs L2 normalization on a vector.
    """
    if not vector:
        return []
    np_vector = np.array(vector, dtype=np.float32)
    norm = np.linalg.norm(np_vector)
    if norm == 0:
        # Return the original vector (or a zero vector of the same dimension)
        # if norm is zero to avoid division by zero.
        print("Warning: Zero norm vector encountered during normalization.")
        return vector 
    normalized_vector = np_vector / norm
    return normalized_vector.tolist()

def get_text_embedding(
    text_to_embed: str,
    api_key: str = settings.EMBEDDING_API_KEY,
    api_base_url: str = settings.EMBEDDING_API_BASE_URL,
    model_name: str = settings.EMBEDDING_MODEL_NAME,
    encoding_format: str = "float",
    request_timeout: int = settings.EMBEDDING_REQUEST_TIMEOUT
) -> Optional[List[float]]:
    """
    Generates an embedding vector for the given text using the configured embedding model
    via an OpenAI-compatible API.

    Args:
        text_to_embed (str): The text string to embed.
        api_key (str): API key for the embedding service.
        api_base_url (str): Base URL for the embedding service API (e.g., reverse proxy).
        model_name (str): Name of the embedding model to use.
        encoding_format (str): Encoding format for the embedding, typically "float".
        request_timeout (int): Timeout for the request in seconds.

    Returns:
        Optional[List[float]]: The embedding vector as a list of floats if successful,
                               None otherwise.
    """
    if not text_to_embed:
        print("Error: Input text for embedding cannot be empty.")
        return None

    endpoint_url = f"{api_base_url.rstrip('/')}/embeddings"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    payload = {
        "model": model_name,
        "input": text_to_embed,
        "encoding_format": encoding_format
        # 'dimensions' parameter is not typically used for bge-m3 directly in payload
        # if the service is specifically for bge-m3 and outputs fixed dimensions.
        # If the model (like text-embedding-3-*) supports it, it could be added:
        # if model_name.startswith("text-embedding-3"): # Example condition
        #    payload["dimensions"] = settings.EMBEDDING_DIMENSION
    }

    print(f"DEBUG [embedding_utils]: Calling Embedding API Endpoint: {endpoint_url} with model {model_name}")

    try:
        response = requests.post(endpoint_url, headers=headers, json=payload, timeout=request_timeout)
        
        response.raise_for_status()  # Raises HTTPError for 4xx/5xx status codes
        
        response_json = response.json()

        if "data" in response_json and len(response_json["data"]) > 0 and "embedding" in response_json["data"][0]:
            embedding_vector = response_json["data"][0]["embedding"]
            if isinstance(embedding_vector, list) and all(isinstance(n, (int, float)) for n in embedding_vector):
                # Ensure the vector has the expected dimension
                if len(embedding_vector) == settings.EMBEDDING_DIMENSION:
                    normalized_embedding = normalize_vector(embedding_vector)
                    # print(f"DEBUG [embedding_utils]: Original vector (first 3): {embedding_vector[:3]}, Normalized: {normalized_embedding[:3]}") # Optional: for debugging
                    return normalized_embedding
                else:
                    print(f"Error: API returned embedding with unexpected dimension {len(embedding_vector)}. Expected {settings.EMBEDDING_DIMENSION} for {model_name}.")
                    return None
            else:
                print(f"Error: API returned embedding in an unexpected format. Embedding: {embedding_vector}")
                return None
        else:
            print(f"Error: API response did not contain the expected embedding data. Response: {response_json}")
            return None

    except requests.exceptions.Timeout:
        print(f"Error: Request to Embedding API timed out ({request_timeout}s). URL: {endpoint_url}")
        return None
    except requests.exceptions.HTTPError as e:
        error_details = "No response body"
        if e.response is not None:
            try:
                error_details = e.response.json()
            except json.JSONDecodeError:
                error_details = e.response.text
        print(f"Error: Embedding API HTTP error. Status: {e.response.status_code if e.response else 'N/A'}, Details: {error_details}, URL: {endpoint_url}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"Error: Connection or other network error during Embedding API request: {e}, URL: {endpoint_url}")
        return None
    except Exception as e:
        print(f"Error: Unknown error while processing Embedding API response: {e}")
        return None

if __name__ == '__main__':
    print("Testing get_text_embedding function using dedicated embedding settings...")
    
    if not settings.EMBEDDING_API_KEY or settings.EMBEDDING_API_KEY == "YOUR_EMBEDDING_API_KEY":
        print("ERROR: EMBEDDING_API_KEY not configured in .env or app/core/config.py. Please set it up.")
    elif not settings.EMBEDDING_API_BASE_URL or settings.EMBEDDING_API_BASE_URL == "YOUR_EMBEDDING_API_BASE_URL":
        print("ERROR: EMBEDDING_API_BASE_URL not configured in .env or app/core/config.py.")
    else:
        print(f"Using Embedding API Base: {settings.EMBEDDING_API_BASE_URL}")
        print(f"Using Embedding Model: {settings.EMBEDDING_MODEL_NAME}")
        print(f"Expected Embedding Dimension: {settings.EMBEDDING_DIMENSION}")
        print(f"Request Timeout: {settings.EMBEDDING_REQUEST_TIMEOUT}")

        test_text = "你好，世界！这是一个测试。"
        print(f"\nInput text: \"{test_text}\"")
        vector = get_text_embedding(test_text)

        if vector:
            print("Successfully retrieved embedding.")
            print(f"  Dimension: {len(vector)}")
            print(f"  First 3 elements: {vector[:3]}")
            print(f"  Last 3 elements: {vector[-3:]}")
        else:
            print("Failed to retrieve embedding.")

        test_text_2 = "This is another test for the embedding model."
        print(f"\nInput text: \"{test_text_2}\"")
        vector_2 = get_text_embedding(test_text_2)
        if vector_2:
            print("Successfully retrieved embedding for second text.")
            print(f"  Dimension: {len(vector_2)}")
        else:
            print("Failed to retrieve embedding for second text.")
    print("\nTest finished.")
