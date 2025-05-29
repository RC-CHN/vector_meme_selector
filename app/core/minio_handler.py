from minio import Minio
from minio.error import S3Error
from .config import settings # Import app settings
from typing import Optional # For type hinting
from urllib.parse import urlparse, urlunparse # For URL rebasing
from datetime import timedelta # For presigned URL expiration

class MinioHandler:
    _client = None

    @classmethod
    def get_client(cls):
        if cls._client is None:
            try:
                cls._client = Minio(
                    settings.MINIO_ENDPOINT,
                    access_key=settings.MINIO_ACCESS_KEY,
                    secret_key=settings.MINIO_SECRET_KEY,
                    secure=settings.MINIO_USE_SSL
                )
                print(f"MinIO client initialized for endpoint: {settings.MINIO_ENDPOINT}")
                # Check and create bucket if it doesn't exist
                cls._ensure_bucket_exists(cls._client, settings.MINIO_BUCKET_NAME)
            except Exception as e:
                print(f"Error initializing MinIO client: {e}")
                # Depending on the desired behavior, you might want to raise the exception
                # or handle it by setting _client to None and letting operations fail.
                cls._client = None # Ensure it's None if initialization fails
                raise  # Re-raise the exception to make the app aware of the failure
        return cls._client

    @staticmethod
    def _ensure_bucket_exists(client: Minio, bucket_name: str):
        try:
            found = client.bucket_exists(bucket_name)
            if not found:
                client.make_bucket(bucket_name)
                print(f"MinIO bucket '{bucket_name}' created successfully.")
            else:
                print(f"MinIO bucket '{bucket_name}' already exists.")
        except S3Error as e:
            print(f"Error checking or creating MinIO bucket '{bucket_name}': {e}")
            raise # Re-raise to indicate a critical failure in setup

    @classmethod
    def upload_file(cls, bucket_name: str, object_name: str, data_stream, length: int, content_type: str):
        client = cls.get_client()
        if not client:
            raise ConnectionError("MinIO client is not available.")
        try:
            client.put_object(
                bucket_name,
                object_name,
                data_stream,
                length=length,
                content_type=content_type
            )
            print(f"Successfully uploaded '{object_name}' to MinIO bucket '{bucket_name}'.")
            # Construct URL (optional, depends on MinIO setup and public access)
            # minio_url = f"{'https://' if settings.MINIO_USE_SSL else 'http://'}{settings.MINIO_ENDPOINT}/{bucket_name}/{object_name}"
            # return minio_url
        except S3Error as e:
            print(f"MinIO S3 error during upload of '{object_name}': {e}")
            raise

    @classmethod
    def generate_presigned_url(cls, bucket_name: str, object_name: str) -> Optional[str]:
        """
        Generates a presigned URL for GETting an object from MinIO.
        Handles URL rebasing if MINIO_PUBLIC_ENDPOINT is set.
        """
        client = cls.get_client() # This client uses settings.MINIO_ENDPOINT (e.g., "minio:9000")
        if not client:
            print("Error: MinIO client is not available for generating presigned URL.")
            return None
        
        try:
            expiration_seconds = settings.PRESIGNED_URL_EXPIRATION_SECONDS
            # presigned_url will be based on the client's endpoint (e.g., http://minio:9000/...)
            # The signature in this URL is calculated for Host: minio:9000
            presigned_url = client.presigned_get_object(
                bucket_name,
                object_name,
                expires=timedelta(seconds=expiration_seconds)
            )

            # If a public endpoint is defined, rebase the URL to use it.
            # The Nginx proxy will handle rewriting the Host header back to what MinIO expects.
            if settings.MINIO_PUBLIC_ENDPOINT:
                original_parsed_url = urlparse(presigned_url)
                public_endpoint_parsed = urlparse(settings.MINIO_PUBLIC_ENDPOINT)

                if not public_endpoint_parsed.scheme or not public_endpoint_parsed.netloc:
                    print(f"Warning: MINIO_PUBLIC_ENDPOINT ('{settings.MINIO_PUBLIC_ENDPOINT}') is not a valid full URL. Cannot rebase presigned URL. Returning original URL: {presigned_url}")
                    return presigned_url

                rebased_url_parts = (
                    public_endpoint_parsed.scheme,    # Scheme from public endpoint (e.g., http)
                    public_endpoint_parsed.netloc,   # Netloc from public endpoint (e.g., localhost:9000)
                    original_parsed_url.path,        # Path from original presigned URL
                    original_parsed_url.params,      # Params from original
                    original_parsed_url.query,       # Query (including signature) from original
                    original_parsed_url.fragment     # Fragment from original
                )
                rebased_url = urlunparse(rebased_url_parts)
                # print(f"DEBUG: Successfully generated and rebased presigned URL for '{object_name}': {rebased_url}")
                return rebased_url
            else:
                # print(f"DEBUG: Successfully generated (but not rebased) presigned URL for '{object_name}': {presigned_url}")
                # This URL (e.g., http://minio:9000/...) won't be accessible outside Docker without MINIO_PUBLIC_ENDPOINT and a proxy.
                return presigned_url

        except S3Error as e:
            print(f"MinIO S3 error generating presigned URL for '{object_name}': {e}")
            return None
        except Exception as e:
            print(f"Unexpected error generating presigned URL for '{object_name}': {e}")
            return None

# Example of how to get the client instance:
# minio_client = MinioHandler.get_client()
# if minio_client:
#     # Use the client
#     pass

# This handler can be expanded with more methods like download_file, delete_file, list_files etc.
