# Dockerfile
FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive
# Ensures python output is sent straight to terminal (useful for logs)
ENV PYTHONUNBUFFERED=1

ENV HTTP_PROXY="http://192.168.44.2:10820"
ENV HTTPS_PROXY="http://192.168.44.2:10820"

WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
# Make sure to have a .dockerignore file to exclude unnecessary files/dirs
COPY . .

# Make the start script executable
RUN chmod +x ./start_dev_services.sh

# Command to run when the container starts
CMD ["./start_dev_services.sh", "start_and_keep_alive"]
