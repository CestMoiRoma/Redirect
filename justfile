# Redirect task runner

# Run the app natively with auto-reload, data in ./data, on $PORT (8080)
run:
    DATA_DIR="${DATA_DIR:-./data}" uv run uvicorn redirect.main:app --reload --host 127.0.0.1 --port "${PORT:-8080}"
