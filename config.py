import os
from datetime import timedelta
from dotenv import load_dotenv

# Load environment variables from .env file if it exists
load_dotenv()

# Redis Configuration
# Default to localhost for standalone deployment
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
QUEUE_NAME = os.getenv("QUEUE_NAME", "freecad_jobs")
STORAGE_PATH = os.getenv("STORAGE_PATH", "/app/storage")
MIN_JOB_PRIORITY = int(os.getenv("MIN_JOB_PRIORITY", "0"))
MAX_JOB_PRIORITY = int(os.getenv("MAX_JOB_PRIORITY", "100"))
DEFAULT_JOB_PRIORITY = int(os.getenv("DEFAULT_JOB_PRIORITY", "0"))

# API Configuration
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8080"))  # Changed default to 8020
MQTT_BROKER = os.getenv("MQTT_BROKER", "mqtt://localhost:1883")  # Default to localhost

JOB_TIMEOUT = int(os.getenv("JOB_TIMEOUT", str(int(timedelta(hours=1).total_seconds()))))  # Default 1 hour for heavy files
RESULT_TTL = int(os.getenv("RESULT_TTL", str(int(timedelta(hours=12).total_seconds()))))
FAILURE_TTL = int(os.getenv("FAILURE_TTL", str(int(timedelta(hours=12).total_seconds()))))

os.makedirs(STORAGE_PATH, exist_ok=True)
