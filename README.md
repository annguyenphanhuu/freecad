# FreeCAD Model Generator API (Flask + RQ + Redis + Swagger)

API receives requests to create FreeCAD models, queues jobs into Redis Queue. Workers process in parallel, generating STEP and OBJ files stored in `storage/`.

## 🚀 Installation and Running

### Docker Compose (Recommended)

#### Method 1: Using automatic script (Recommended)
```bash
# Run with 3 workers (default)
./start.sh

# Run with custom number of workers
./start.sh 5

# Run with 1 worker
./start.sh 1
```

#### Method 2: Using environment variables
```bash
# Set number of workers and API URL
export WORKER_REPLICAS=3
export API_BASE_URL=http://localhost:8000
docker-compose up -d --build

# Or run directly
WORKER_REPLICAS=5 API_BASE_URL=http://localhost:8000 docker-compose up -d --build

# For production server
export API_BASE_URL=https://your-domain.com
export WORKER_REPLICAS=5
./start.sh
```

#### Method 3: Manual execution
```bash
# Build and run all services
docker-compose up -d --build

# Scale workers manually
docker-compose up -d --scale worker=5

# View logs
docker-compose logs -f

# Stop services
docker-compose down
```

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run Redis
redis-server

# Run API server
python app.py

# Run worker (in another terminal)
rq worker --url redis://localhost:6379 freecad_jobs
```

## 📚 Swagger Documentation

After running the API, access Swagger UI at:
- **http://localhost:8000/swagger/**

Swagger provides:
- Interactive API documentation
- Try-it-out functionality
- Request/response schemas
- Parameter validation

## 🔗 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/swagger/` | Swagger UI documentation |
| POST | `/freecad/generate` | Create new FreeCAD model (upload file + user_id) |
| GET | `/freecad/status/{user_id}` | Check job status by user_id |
| GET | `/freecad/result/{user_id}` | Get job result by user_id |
| GET | `/freecad/download/{user_id}/{filename}` | Download file by user_id and filename |
| GET | `/freecad/template/oblong` | Get template parameters |
| GET | `/freecad/monitor` | FreeCAD runtime monitor dashboard |
| GET | `/freecad/monitor/overview` | Monitor snapshot: workers, queue, resources, history |
| GET | `/freecad/monitor/resources` | Recent RAM/CPU samples for charts |
| GET | `/freecad/monitor/jobs` | Running jobs, queued jobs, and saved history |
| GET | `/freecad/monitor/export` | Export monitor JSON |

## 🔧 Configuration

### Job Priority

`POST /freecad/generate` accepts an optional form field named `priority`.

- Range: `0` to `100`
- Default: `0`
- Higher values run earlier when jobs are waiting in the queue
- Priority affects queued jobs only; running jobs are not interrupted

Example:

```bash
curl -X POST http://localhost:8080/freecad/generate \
  -F "file=@script.py" \
  -F "user_id=user123" \
  -F "priority=90"
```

The current storage and MQTT model treats `user_id` as the job identifier. Avoid submitting multiple active jobs with the same `user_id`, because input, output, and status are keyed by `user_id`.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection URL |
| `QUEUE_NAME` | `freecad_jobs` | Queue name for jobs |
| `STORAGE_PATH` | `/app/storage` | Path to store generated files |
| `API_HOST` | `0.0.0.0` | API server host |
| `API_PORT` | `8000` | API server port |
| `API_BASE_URL` | `http://localhost:8000` | Base URL for download links |
| `WORKER_REPLICAS` | `3` | Number of worker replicas |

### Production Deployment

```bash
# Set production environment variables
export API_BASE_URL=https://your-domain.com
export WORKER_REPLICAS=5

# Start services
./start.sh

# Client will automatically use the correct API URL
python3 client_user_upload.py oblong.py user123 --auto-download
```

## 🧪 Testing

### API Client Tests
```bash
# Test file upload with auto-download (recommended)
python3 client_user_upload.py oblong.py user123 --auto-download

# Test without auto-download
python3 client_user_upload.py oblong.py user123

# Test full workflow (equivalent to freecadcmd oblong.py)
python client_oblong.py

# Test with custom parameters
python client_oblong.py --custom

# Test using wrapper (similar to freecadcmd)
python freecadcmd_api.py oblong
python freecadcmd_api.py oblong --custom
```

### Manual API Testing

#### 1. Health Check
```bash
curl -s http://localhost:8000/health
```

#### 2. Get Template
```bash
curl -s http://localhost:8000/freecad/template/oblong
```

#### 3. Generate Model (File Upload)
```bash
# Upload script file
curl -X POST -F "file=@oblong_wrapper.py" -F "user_id=user123" http://localhost:8000/freecad/generate

# Or with original script
curl -X POST -F "file=@oblong.py" -F "user_id=user456" http://localhost:8000/freecad/generate
```

#### 4. Check Status
```bash
curl -s http://localhost:8000/freecad/status/{user_id}
```

#### 5. Get Result
```bash
curl -s http://localhost:8000/freecad/result/{user_id}
```

#### 6. Download File
```bash
# Download specific file
curl -O http://localhost:8000/freecad/download/{user_id}/{filename}

# Example
curl -O http://localhost:8000/freecad/download/alice/Platine_150x75x6_R15_1Oblong_D10p5x30_Center_alice.obj
```

## 👤 User Management

API uses `user_id` to manage jobs instead of auto-generating job_id:

### ✅ **Benefits:**
- **Easy management**: Track jobs by specific user
- **No duplicates**: Each user has separate namespace
- **Scalable**: Supports multiple users simultaneously
- **Predictable**: Users know the ID in advance for tracking

### 📝 **Usage:**
```bash
# Upload with user_id
curl -X POST -F "file=@script.py" -F "user_id=alice" http://localhost:8000/freecad/generate

# Check status by user_id
curl http://localhost:8000/freecad/status/alice

# Get result by user_id  
curl http://localhost:8000/freecad/result/alice
```

### 🔄 **Multiple Users:**
```bash
# User Alice
python3 client_user_upload.py oblong.py alice

# User Bob (in parallel)
python3 client_user_upload.py oblong.py bob

# User Charlie
python3 client_user_upload.py oblong.py charlie
```

## 📥 Auto-Download Feature

Client automatically downloads files to local machine when job is completed:

### ✅ **Features:**
- **Auto-download**: Files are automatically downloaded to current directory
- **Security**: Only file owner can download
- **Multiple formats**: Supports STEP and OBJ files
- **Progress tracking**: Displays download progress

### 📝 **How it works:**
```bash
# Run client - files will be automatically downloaded
python3 client_user_upload.py oblong.py alice

# Output:
# 🎉 Job completed successfully!
# 📁 Files generated:
#    📄 OBJ: Platine_150x75x6_R15_1Oblong_D10p5x30_Center_alice.obj
#    📄 STEP: Platine_150x75x6_R15_1Oblong_D10p5x30_Center_alice.step
# 📥 Downloaded files:
#    ✅ Platine_150x75x6_R15_1Oblong_D10p5x30_Center_alice.obj
#    ✅ Platine_150x75x6_R15_1Oblong_D10p5x30_Center_alice.step
# 💡 Files are now available in current directory
```

### 🔒 **Security:**
- Files can only be downloaded by their owner
- Filename must contain `user_id` for authentication
- API endpoint: `/freecad/download/{user_id}/{filename}`

## 📁 File Output

Each job generates 2 files:
- **STEP file**: Standard CAD 3D model
- **OBJ file**: Mesh model for visualization

Files are saved in the `storage/` directory with filenames containing parameter information and user_id.

**Examples:**
- `Platine_150x75x6_R15_1Oblong_D10p5x30_Center_alice.obj`
- `Platine_150x75x6_R15_1Oblong_D10p5x30_Center_alice.step`

## ⚙️ Configuration

Modify environment variables:
- `STORAGE_PATH`: File storage path (default: `/app/storage`)
- `REDIS_URL`: Redis URL (default: `redis://redis:6379/0`)
- `API_PORT`: API port (default: `8000`)
- `WORKER_REPLICAS`: Number of workers (default: `3`)

**Note**: Redis runs on port `6380` to avoid conflicts with local Redis.

## 📝 Notes

- If FreeCAD is not available in the worker environment, placeholder STEP/OBJ files will be created
- Worker integrates logic from `oblong.py` to create accurate models
- API has complete Swagger documentation with validation

## 🎯 Equivalent Commands

Instead of running `freecadcmd oblong.py`, you can use:

### **Method 1: Upload script file (NEW - Recommended)**
```bash
# Equivalent to: freecadcmd oblong.py
python client_file_upload.py oblong.py

# Or with wrapper script
python client_file_upload.py oblong_wrapper.py
```

### **Method 2: API with parameters (Legacy)**
```bash
# Equivalent to: freecadcmd oblong.py
python client_oblong.py

# Or using wrapper
python freecadcmd_api.py oblong

# With custom parameters
python client_oblong.py --custom
python freecadcmd_api.py oblong --custom
```

### **Method 3: Swagger UI**
1. Open http://localhost:8000/swagger/
2. Select endpoint `/freecad/generate`
3. Upload your `.py` file
4. Click "Execute"

**Benefits of API approach:**
- ✅ **Upload script files directly** - just like `freecadcmd`
- ✅ No need to install FreeCAD locally
- ✅ Parallel processing with multiple workers
- ✅ Queue management with Redis
- ✅ RESTful API with Swagger docs
- ✅ Easy to scale and monitor
#   f r e e c a d  
 