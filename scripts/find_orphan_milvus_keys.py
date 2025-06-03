import os
import psycopg2
from pymilvus import connections, utility, Collection
from dotenv import load_dotenv
from typing import Set, List

# --- Configuration ---
# Load environment variables from .env file in the project root
# Assuming this script is in a 'scripts' subdirectory of the project root
dotenv_path = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(dotenv_path=dotenv_path)

# PostgreSQL connection details from environment variables
DB_HOST = "localhost"
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB")
DB_USER = os.getenv("POSTGRES_USER")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")

# Milvus connection details from environment variables
MILVUS_HOST = "localhost"
MILVUS_PORT = os.getenv("MILVUS_PORT", "19530")
MILVUS_ALIAS = "orphan_check_alias" # Use a specific alias for this script

# Milvus collection names (should match your project's config)
EMOTION_COLLECTION_NAME = os.getenv("SEARCH_EMOTION_COLLECTION_NAME", "emotion_tags_collection")
SEMANTIC_COLLECTION_NAME = os.getenv("SEARCH_SEMANTIC_COLLECTION_NAME", "semantic_tags_collection")

# --- Print Configuration ---
print("--- Script Configuration ---")
print(f"PostgreSQL Host: {DB_HOST}")
print(f"PostgreSQL Port: {DB_PORT}")
print(f"PostgreSQL DB Name: {DB_NAME}")
print(f"PostgreSQL User: {DB_USER}")
print(f"PostgreSQL Password Set: {'Yes' if DB_PASSWORD else 'No'}") # Avoid printing actual password
print(f"Milvus Host: {MILVUS_HOST}")
print(f"Milvus Port: {MILVUS_PORT}")
print(f"Milvus Emotion Collection: {EMOTION_COLLECTION_NAME}")
print(f"Milvus Semantic Collection: {SEMANTIC_COLLECTION_NAME}")
print("--------------------------\n")

# --- Helper Functions ---

def get_postgres_minio_object_names() -> Set[str]:
    """Fetches all minio_object_name from the ImageMetadata table in PostgreSQL."""
    conn = None
    try:
        print(f"Connecting to PostgreSQL: {DB_USER}@{DB_HOST}:{DB_PORT}/{DB_NAME}")
        conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT minio_object_name FROM image_metadata WHERE minio_object_name IS NOT NULL;")
        rows = cur.fetchall()
        names = {row[0] for row in rows}
        print(f"Fetched {len(names)} unique minio_object_names from PostgreSQL.")
        return names
    except Exception as e:
        print(f"Error connecting to or querying PostgreSQL: {e}")
        return set()
    finally:
        if conn:
            conn.close()

def get_milvus_collection_minio_object_names(collection_name: str) -> Set[str]:
    """
    Fetches all minio_object_name (primary keys) from a specific Milvus collection.
    """
    names: Set[str] = set()
    try:
        if not utility.has_collection(collection_name, using=MILVUS_ALIAS):
            print(f"Milvus collection '{collection_name}' does not exist. Skipping.")
            return names

        collection = Collection(collection_name, using=MILVUS_ALIAS)
        collection.load() # Ensure collection is loaded
        total_entities = collection.num_entities
        
        if total_entities == 0:
            print(f"Milvus collection '{collection_name}' is empty.")
            return names

        print(f"Querying all minio_object_names from Milvus collection '{collection_name}' (approx {total_entities} entities)...")
        
        # Query all entities and get the PK field.
        # The expression `minio_object_name != ""` should work for non-empty string PKs.
        res = collection.query(
            expr="minio_object_name != ''", 
            output_fields=["minio_object_name"],
            consistency_level="Strong",
            limit = total_entities + 100 # Ensure we get all, add a small buffer
        )
        
        for item in res:
            names.add(item["minio_object_name"])
        
        retrieved_count = len(names)
        print(f"Fetched {retrieved_count} unique minio_object_names from Milvus collection '{collection_name}'.")
        
        return names

    except Exception as e:
        print(f"Error connecting to or querying Milvus collection '{collection_name}': {e}")
        return names

# --- Main Script Logic ---
if __name__ == "__main__":
    print("Starting script to find orphan Milvus keys...")

    # 1. Connect to Milvus
    try:
        print(f"Connecting to Milvus: {MILVUS_HOST}:{MILVUS_PORT} with alias '{MILVUS_ALIAS}'")
        connections.connect(alias=MILVUS_ALIAS, host=MILVUS_HOST, port=MILVUS_PORT)
        print("Successfully connected to Milvus.")
    except Exception as e:
        print(f"Fatal: Could not connect to Milvus. Error: {e}")
        exit(1)

    # 2. Get names from PostgreSQL
    pg_names = get_postgres_minio_object_names()
    if not pg_names and (DB_HOST and DB_NAME and DB_USER): 
        print("Warning: Could not fetch names from PostgreSQL or table is empty. Proceeding with empty set for PG.")

    # 3. Get names from Milvus collections
    milvus_emotion_names = get_milvus_collection_minio_object_names(EMOTION_COLLECTION_NAME)
    milvus_semantic_names = get_milvus_collection_minio_object_names(SEMANTIC_COLLECTION_NAME)

    # 4. Combine and find differences
    all_milvus_names = milvus_emotion_names.union(milvus_semantic_names)
    print(f"\nTotal unique minio_object_names found in Milvus (both collections combined): {len(all_milvus_names)}")
    print(f"Total unique minio_object_names found in PostgreSQL: {len(pg_names)}")

    orphan_names_in_milvus = all_milvus_names - pg_names

    missing_names_in_milvus = pg_names - all_milvus_names
    if missing_names_in_milvus:
        print(f"\nFound {len(missing_names_in_milvus)} minio_object_names in PostgreSQL that are NOT in Milvus:")
        for i, name in enumerate(list(missing_names_in_milvus)[:100]): # Print first 100 missing
            print(f"  {i+1}. {name}")
        if len(missing_names_in_milvus) > 100:
            print(f"  ... and {len(missing_names_in_milvus) - 100} more missing names not printed.")
        
        output_file_path_missing = "missing_in_milvus_keys.txt"
        with open(output_file_path_missing, "w") as f:
            for name in missing_names_in_milvus:
                f.write(name + "\n")
        print(f"\nFull list of {len(missing_names_in_milvus)} missing keys (in PG, not Milvus) written to {output_file_path_missing}")
    else:
        print("\nNo minio_object_names found in PostgreSQL that are missing from Milvus.")

    if orphan_names_in_milvus:
        print(f"\nFound {len(orphan_names_in_milvus)} minio_object_names in Milvus that are NOT in PostgreSQL:")
        for i, name in enumerate(list(orphan_names_in_milvus)[:100]): # Print first 100 orphans
            print(f"  {i+1}. {name}")
        if len(orphan_names_in_milvus) > 100:
            print(f"  ... and {len(orphan_names_in_milvus) - 100} more orphans not printed.")
        
        output_file_path_orphan = "orphan_milvus_keys.txt" # Changed variable name for clarity
        with open(output_file_path_orphan, "w") as f:
            for name in orphan_names_in_milvus:
                f.write(name + "\n")
        print(f"\nFull list of {len(orphan_names_in_milvus)} orphan keys (in Milvus, not PG) written to {output_file_path_orphan}")
    else:
        print("\nNo orphan minio_object_names found in Milvus (in Milvus, not PG).")

    # 5. Disconnect from Milvus
    try:
        connections.disconnect(MILVUS_ALIAS)
        print("Successfully disconnected from Milvus.")
    except Exception as e:
        print(f"Error disconnecting from Milvus: {e}")

    print("\nScript finished.")
