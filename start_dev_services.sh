#!/bin/bash

# Define directories
LOG_DIR="logs"
PID_DIR="run/pids" # Directory to store PID files

# Ensure log and PID directories exist
mkdir -p "$LOG_DIR"
mkdir -p "$PID_DIR"

# --- Service Definitions ---
# Array of service names, commands, and their respective log/pid filenames
# Format: "Service Name|Command to run|log_filename_stem|pid_filename_stem"
SERVICES=(
    "Celery Beat|celery -A celery_worker.celery_setup.celery_app beat -l info|beat|beat"
    "Beat/Default Worker|celery -A celery_worker.celery_setup.celery_app worker -l info -Q beat_scheduler_tasks_queue,default -c 1 -n worker_beat_default@%h|worker_beat_default|beat_default_worker"
    "Initial Tagging Worker|celery -A celery_worker.celery_setup.celery_app worker -l info -Q initial_tagging_queue -c 2 -n worker_initial_tagging@%h|worker_initial_tagging|initial_tagging_worker"
    "Retry Tagging Worker|celery -A celery_worker.celery_setup.celery_app worker -l info -Q retry_tagging_queue -c 1 -n worker_retry_tagging@%h|worker_retry_tagging|retry_tagging_worker"
    "Initial Vectorization Worker|celery -A celery_worker.celery_setup.celery_app worker -l info -Q initial_vectorization_queue -c 2 -n worker_initial_vectorization@%h|worker_initial_vectorization|initial_vectorization_worker"
    "Retry Vectorization Worker|celery -A celery_worker.celery_setup.celery_app worker -l info -Q retry_vectorization_queue -c 2 -n worker_retry_vectorization@%h|worker_retry_vectorization|retry_vectorization_worker"
    "Uvicorn Server|uvicorn app.main:app --host 0.0.0.0 --port 8000|uvicorn|uvicorn"
)

# --- Function to start all services ---
start_services() {
    echo "Attempting to start all services..."
    echo ""

    local all_already_running=true
    for service_def in "${SERVICES[@]}"; do
        IFS='|' read -r name cmd log_stem pid_stem <<< "$service_def"
        local pid_file="$PID_DIR/$pid_stem.pid"
        if [ -f "$pid_file" ] && ps -p "$(cat "$pid_file")" > /dev/null; then
            echo "$name (PID: $(cat "$pid_file")) is already running. Skipping."
        else
            all_already_running=false
            echo "Starting $name..."
            # Using nohup to ensure processes continue after script exits and are immune to SIGHUP
            # Redirecting stdout and stderr to log files
            nohup $cmd > "$LOG_DIR/$log_stem.log" 2>&1 &
            local pid=$!
            echo "$pid" > "$pid_file"
            echo "$name started with PID: $pid. Log: $LOG_DIR/$log_stem.log"
        fi
        echo ""
    done

    if $all_already_running; then
        echo "All services appear to be already running."
    else
        echo "Service startup process initiated."
    fi
    echo "You can monitor logs in the '$LOG_DIR' directory."
}

# --- Function to stop all services ---
stop_services() {
    echo "Attempting to stop all services..."
    echo ""
    local all_stopped_successfully=true

    # Iterate in reverse order for shutdown (optional, but sometimes Uvicorn is last to start, first to stop)
    for (( i=${#SERVICES[@]}-1 ; i>=0 ; i-- )); do
        IFS='|' read -r name cmd log_stem pid_stem <<< "${SERVICES[$i]}"
        local pid_file="$PID_DIR/$pid_stem.pid"

        if [ -f "$pid_file" ]; then
            local pid_to_kill=$(cat "$pid_file")
            if [ -n "$pid_to_kill" ] && ps -p "$pid_to_kill" > /dev/null; then
                echo "Stopping $name (PID: $pid_to_kill)..."
                kill "$pid_to_kill" # Try graceful shutdown first
                sleep 2 # Wait for graceful shutdown

                if ps -p "$pid_to_kill" > /dev/null; then
                    echo "$name (PID: $pid_to_kill) did not stop gracefully, trying kill -9 (force)..."
                    kill -9 "$pid_to_kill"
                    sleep 1 # Wait a moment after force kill
                fi

                if ps -p "$pid_to_kill" > /dev/null; then
                    echo "ERROR: Failed to stop $name (PID: $pid_to_kill) even with kill -9."
                    all_stopped_successfully=false
                else
                    echo "$name stopped."
                    rm "$pid_file" # Remove PID file on successful stop
                fi
            else
                echo "$name (PID: $pid_to_kill from $pid_file) is not running or PID is invalid. Removing stale PID file."
                rm "$pid_file"
            fi
        else
            echo "PID file for $name not found ($pid_file). Assuming service is not running or PID is unknown."
        fi
        echo ""
    done

    if $all_stopped_successfully; then
        echo "All specified services have been processed for shutdown."
    else
        echo "Some services may not have stopped correctly. Please check manually."
    fi
}

# --- Main script logic based on argument ---
COMMAND=$1

case "$COMMAND" in
    start)
        start_services
        ;;
    stop)
        stop_services
        ;;
    restart)
        echo "Restarting services..."
        stop_services
        echo ""
        echo "Waiting a few seconds before starting again..."
        sleep 3
        start_services
        ;;
    status)
        echo "Checking status of services..."
        echo ""
        for service_def in "${SERVICES[@]}"; do
            IFS='|' read -r name cmd log_stem pid_stem <<< "$service_def"
            local pid_file="$PID_DIR/$pid_stem.pid"
            if [ -f "$pid_file" ]; then
                local pid=$(cat "$pid_file")
                if [ -n "$pid" ] && ps -p "$pid" > /dev/null; then
                    echo "$name is RUNNING (PID: $pid). Log: $LOG_DIR/$log_stem.log"
                else
                    echo "$name is STOPPED (Stale PID file found: $pid_file containing $pid)."
                fi
            else
                echo "$name is STOPPED (No PID file found: $pid_file)."
            fi
        done
        ;;
    start_and_keep_alive) # New command
        echo "Starting services and keeping script alive..."
        start_services
        echo "All services initiated. Script will now stay alive."
        echo "Monitor logs in '$LOG_DIR' directory inside the container."
        # Keep the script running in the foreground
        # This prevents the container from exiting if this script is PID 1
        trap "echo 'Received signal, exiting...'; exit 0" SIGINT SIGTERM
        tail -f /dev/null & wait ${!}
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|start_and_keep_alive}"
        exit 1
        ;;
esac

exit 0
