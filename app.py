import uuid
import os
import tempfile
import shutil
from datetime import datetime, timezone
import time
from typing import Any, Dict
from flask import Flask, request, jsonify, send_file, render_template, redirect
from flask_restx import Api, Resource, fields, Namespace
from redis import Redis
from rq import Queue
from rq.job import Job
from rq.registry import StartedJobRegistry, FinishedJobRegistry
from dotenv import load_dotenv
from werkzeug.datastructures import FileStorage
from werkzeug.exceptions import HTTPException
import threading

import config
from mqtt_client import get_mqtt_manager
from script_preprocessor import save_preprocessed_script
from freecad_monitor import (
    RESOURCE_LIMIT as RESOURCE_LIMIT_FOR_PEAKS,
    current_process_snapshot,
    get_history,
    get_live_job_snapshots,
    get_resource_series,
    record_resource_sample,
    system_snapshot,
)

load_dotenv()

app = Flask(__name__)
api = Api(app,
          title='FreeCAD Model Generator API',
          version='1.0',
          description='API to create FreeCAD models through worker queue with scalability',
          doc='/swagger/')

redis_conn = Redis.from_url(config.REDIS_URL)
queue = Queue(
    config.QUEUE_NAME,
    connection=redis_conn,
    default_timeout=config.JOB_TIMEOUT,
    result_ttl=config.RESULT_TTL,
    failure_ttl=config.FAILURE_TTL,
)


def normalize_priority(raw_value) -> int:
    """Parse and validate a job priority value."""
    if raw_value is None:
        return config.DEFAULT_JOB_PRIORITY

    value_text = str(raw_value).strip()
    if "." in value_text:
        raise ValueError(f"Priority must be an integer, got: {raw_value!r}")

    try:
        value = int(value_text)
    except (TypeError, ValueError):
        raise ValueError(f"Priority must be an integer, got: {raw_value!r}")

    if value < config.MIN_JOB_PRIORITY or value > config.MAX_JOB_PRIORITY:
        raise ValueError(
            f"Priority must be between {config.MIN_JOB_PRIORITY} and "
            f"{config.MAX_JOB_PRIORITY}, got: {value}"
        )

    return value


def get_priority_queue_name(priority: int) -> str:
    return f"{config.QUEUE_NAME}_p{priority}"


def infer_priority_from_queue_name(queue_name: str, fallback: int = None) -> int:
    """Infer priority from a priority queue name such as freecad_jobs_p90."""
    if queue_name:
        prefix = f"{config.QUEUE_NAME}_p"
        if queue_name.startswith(prefix):
            try:
                return int(queue_name[len(prefix):])
            except ValueError:
                pass

    return config.DEFAULT_JOB_PRIORITY if fallback is None else fallback


def get_priority_queue(priority: int) -> Queue:
    return Queue(
        get_priority_queue_name(priority),
        connection=redis_conn,
        default_timeout=config.JOB_TIMEOUT,
        result_ttl=config.RESULT_TTL,
        failure_ttl=config.FAILURE_TTL,
    )


def iter_priority_queue_names():
    """Yield priority queue names from highest priority to lowest."""
    for priority in range(config.MAX_JOB_PRIORITY, config.MIN_JOB_PRIORITY - 1, -1):
        yield priority, get_priority_queue_name(priority)


def iter_all_queue_names():
    """Yield priority queues plus the legacy queue for migration compatibility."""
    seen = set()
    for _, queue_name in iter_priority_queue_names():
        seen.add(queue_name)
        yield queue_name

    if config.QUEUE_NAME not in seen:
        yield config.QUEUE_NAME


def find_job_for_user(user_id: str):
    """
    Find a user's job across priority queues and the legacy queue.
    Search order is started, queued, then finished; each pass scans high to low priority.
    """
    registry_getters = (
        lambda q: q.started_job_registry.get_job_ids(),
        lambda q: q.get_job_ids(),
        lambda q: q.finished_job_registry.get_job_ids(),
    )

    for get_job_ids in registry_getters:
        for queue_name in iter_all_queue_names():
            q = Queue(queue_name, connection=redis_conn)
            try:
                job_ids = get_job_ids(q)
            except Exception:
                continue

            for job_id in job_ids:
                try:
                    job = Job.fetch(job_id, connection=redis_conn)
                    if job.meta and job.meta.get("user_id") == user_id:
                        return job
                except Exception:
                    continue

    return None


# Initialize MQTT manager
mqtt_manager = get_mqtt_manager()

# Swagger models
freecad_ns = Namespace('freecad', description='FreeCAD model generation operations')
api.add_namespace(freecad_ns)

# API Models
upload_parser = api.parser()
upload_parser.add_argument('file', location='files', type=FileStorage, required=True, help='FreeCAD Python script file (.py)')
upload_parser.add_argument('user_id', location='form', type=str, required=True, help='User ID for job management')
upload_parser.add_argument('auto_download', location='form', type=bool, required=False, help='If true, API will wait and return files immediately')
upload_parser.add_argument('metadata_file', location='files', type=FileStorage, required=False, help='Optional metadata JSON file for threaded holes information')
upload_parser.add_argument(
    'priority',
    location='form',
    type=int,
    required=False,
    default=config.DEFAULT_JOB_PRIORITY,
    help=(
        f'Job priority {config.MIN_JOB_PRIORITY}-{config.MAX_JOB_PRIORITY}. '
        f'Higher runs first. Default: {config.DEFAULT_JOB_PRIORITY}.'
    )
)

# Result parser with auto_download parameter
result_parser = api.parser()
result_parser.add_argument('auto_download', location='query', type=bool, required=False, help='[DEPRECATED] This parameter is no longer used. Files are stored in storage/{user_id}/output/')

generate_request = api.model('GenerateRequest', {
    'user_id': fields.String(required=True, description='User ID', example='user123'),
    'script_name': fields.String(description='FreeCAD script name', example='oblong.py')
})

job_response = api.model('JobResponse', {
    'user_id': fields.String(description='User ID'),
    'status': fields.String(description='Job status', enum=['queued', 'started', 'finished', 'failed']),
    'message': fields.String(description='Message'),
    'created_at': fields.String(description='Creation time'),
    'priority': fields.Integer(description='Job priority (0-100)'),
    'queue_name': fields.String(description='Redis queue name')
})

status_response = api.model('StatusResponse', {
    'user_id': fields.String(description='User ID'),
    'status': fields.String(description='Job status'),
    'progress': fields.Integer(description='Completion percentage (0-100)'),
    'message': fields.String(description='Detailed message'),
    'error': fields.String(description='Error message (if any)'),
    'updated_at': fields.String(description='Update time'),
    'data_source': fields.String(description='Data source: "mqtt" or "redis"', required=False),
    'mqtt_connected': fields.Boolean(description='MQTT connection status', required=False)
})

file_info = api.model('FileInfo', {
    'type': fields.String(description='File type', enum=['step', 'obj', 'pdf']),
    'path': fields.String(description='File path'),
    'filename': fields.String(description='File name'),
    'download_url': fields.String(description='Download URL (if auto_download is enabled)', required=False),
    'local_path': fields.String(description='Local path (if auto_download is enabled)', required=False)
})

result_response = api.model('ResultResponse', {
    'user_id': fields.String(description='User ID'),
    'status': fields.String(description='Job status'),
    'message': fields.String(description='Message', required=False),
    'files': fields.List(fields.Nested(file_info), description='List of generated files'),
    'output_directory': fields.String(description='Output directory (if auto_download is enabled)', required=False),
    'completed_at': fields.String(description='Completion time')
})

# New models for improved worker status
worker_info = api.model('WorkerInfo', {
    'worker_id': fields.String(description='Worker ID/name'),
    'status': fields.String(description='Worker status', enum=['idle', 'busy', 'offline']),
    'current_user': fields.String(description='User ID being processed (if busy)', required=False),
    'current_priority': fields.Integer(description='Current job priority (if busy)', required=False),
    'priority': fields.Integer(description='Alias of current_priority for easier debugging', required=False),
    'queue_name': fields.String(description='Queue that the current job came from', required=False),
    'job_id': fields.String(description='Current RQ job ID (if busy)', required=False),
    'job_status': fields.String(description='Current RQ job status (if busy)', required=False),
    'progress': fields.Integer(description='Job progress 0-100 (if busy)', required=False),
    'message': fields.String(description='Current status message (if busy)', required=False),
    'started_at': fields.String(description='Job start time (if busy)', required=False),
    'last_heartbeat': fields.String(description='Last heartbeat from worker', required=False)
})

workers_status_response = api.model('WorkersStatusResponse', {
    'workers': fields.List(fields.Nested(worker_info), description='List of workers'),
    'total_workers': fields.Integer(description='Total number of workers'),
    'busy_workers': fields.Integer(description='Number of busy workers'),
    'idle_workers': fields.Integer(description='Number of idle workers'),
    'offline_workers': fields.Integer(description='Number of offline workers'),
    'updated_at': fields.String(description='Update time')
})

# Queue models
queue_item = api.model('QueueItem', {
    'position': fields.Integer(description='Position in queue (1-based)'),
    'job_id': fields.String(description='RQ job ID'),
    'user_id': fields.String(description='User ID'),
    'script_name': fields.String(description='Script name', required=False),
    'status': fields.String(description='RQ job status'),
    'created_at': fields.String(description='Job creation time'),
    'priority': fields.Integer(description='Job priority (0-100)'),
    'queue_name': fields.String(description='Redis queue name'),
    'estimated_wait_time': fields.String(description='Estimated wait time', required=False)
})

queue_response = api.model('QueueResponse', {
    'queue': fields.List(fields.Nested(queue_item), description='List of queued jobs'),
    'total_queued': fields.Integer(description='Total jobs in queue'),
    'updated_at': fields.String(description='Update time')
})


def iso_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()




def _parse_bool(value) -> bool:
    """Robust boolean parser accepting many truthy/falsey forms.
    Accepts: 1,true,yes,on,t,y (case-insensitive). Also treats empty presence (?flag) as True.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s == "":
        # Presence without value (e.g., ?auto_download) counts as True
        return True
    return s in ["1", "true", "yes", "on", "t", "y"]

def _prepare_download_files(files, user_id):
    """Copy files to outputs directory and create download links"""
    if not files:
        return []

    # Always use default output directory (absolute path in container)
    output_dir_absolute = os.path.join("/app", "outputs", "code", "cad_outputs_generated")
    os.makedirs(output_dir_absolute, exist_ok=True)

    # Relative path from source (workspace root)
    output_dir_relative = "outputs/code/cad_outputs_generated"

    # Copy files to outputs directory and create download links
    download_links = []
    for f in files:
        src_path = f.get('path')
        filename = f.get('filename')
        if src_path and os.path.exists(src_path):
            dst_path_absolute = os.path.join(output_dir_absolute, filename)
            dst_path_relative = os.path.join(output_dir_relative, filename)
            shutil.copy2(src_path, dst_path_absolute)

            # Create download link
            base_url = os.getenv('API_BASE_URL', f'http://{config.API_HOST}:{config.API_PORT}')
            download_url = f"{base_url}/freecad/download/{user_id}/{filename}"
            download_links.append({
                "type": f.get('type'),
                "filename": filename,
                "download_url": download_url,
                "local_path": dst_path_relative,
                "path": dst_path_relative
            })

    return download_links


def _get_all_workers_info():
    """Get comprehensive information about all workers"""
    workers = []

    # Get all RQ workers
    from rq.worker import Worker
    rq_workers = Worker.all(connection=redis_conn)
    rq_workers_by_name = {worker.name: worker for worker in rq_workers}

    # Get started jobs registries from priority queues and legacy queue
    started_job_ids = []
    for queue_name in iter_all_queue_names():
        try:
            started_registry = StartedJobRegistry(queue_name, connection=redis_conn)
            started_job_ids.extend(started_registry.get_job_ids())
        except Exception:
            continue

    # Map jobs to workers
    job_to_worker = {}
    for job_id in started_job_ids:
        try:
            job = Job.fetch(job_id, connection=redis_conn)
            if job.worker_name:
                job_to_worker[job.worker_name] = job
        except:
            continue

    # Some RQ versions do not always persist job.worker_name immediately.
    # Ask live workers directly as a second source so /workers/status stays useful.
    for worker in rq_workers:
        if worker.name in job_to_worker:
            continue

        try:
            current_job = worker.get_current_job()
        except Exception:
            current_job = None

        if current_job and not hasattr(current_job, 'meta'):
            try:
                current_job = Job.fetch(current_job, connection=redis_conn)
            except Exception:
                current_job = None

        if current_job:
            job_to_worker[worker.name] = current_job

    # Get MQTT progress data
    mqtt_progress_data = mqtt_manager.get_all_progress()

    # Build worker info
    worker_names = set()

    # Add RQ workers
    for worker in rq_workers:
        worker_names.add(worker.name)

    # Add workers from MQTT data
    for user_id, progress in mqtt_progress_data.items():
        worker_id = progress.get('worker_id')
        if worker_id:
            worker_names.add(worker_id)

    # Build detailed worker info
    for worker_name in worker_names:
        worker_info = {
            'worker_id': worker_name,
            'status': 'idle',
            'current_user': None,
            'current_priority': None,
            'priority': None,
            'queue_name': None,
            'job_id': None,
            'job_status': None,
            'progress': None,
            'message': None,
            'started_at': None,
            'last_heartbeat': None
        }

        # Check if worker has a running job
        if worker_name in job_to_worker:
            job = job_to_worker[worker_name]
            job_meta = job.meta or {}
            user_id = job_meta.get('user_id')
            queue_name = job_meta.get('queue_name') or getattr(job, 'origin', None)
            job_priority = job_meta.get(
                'priority',
                infer_priority_from_queue_name(queue_name)
            )

            worker_info['status'] = 'busy'
            worker_info['current_user'] = user_id
            worker_info['current_priority'] = job_priority
            worker_info['priority'] = job_priority
            worker_info['queue_name'] = queue_name
            worker_info['job_id'] = job.id
            worker_info['job_status'] = job.get_status()
            worker_info['started_at'] = job.started_at.replace(tzinfo=timezone.utc).isoformat() if job.started_at else None

            # Try to get progress from MQTT
            if user_id:
                mqtt_progress = mqtt_manager.get_progress(user_id)
                if mqtt_progress:
                    worker_info['progress'] = mqtt_progress.get('progress', 0)
                    worker_info['message'] = mqtt_progress.get('message', '')
                    worker_info['last_heartbeat'] = mqtt_progress.get('updated_at')

        # Check if worker is in RQ workers list (is alive)
        is_alive = worker_name in rq_workers_by_name
        if not is_alive and worker_info['status'] == 'idle':
            worker_info['status'] = 'offline'

        workers.append(worker_info)

    # Sort workers: busy first, then idle, then offline
    status_priority = {'busy': 0, 'idle': 1, 'offline': 2}
    workers.sort(key=lambda w: (status_priority.get(w['status'], 3), w['worker_id']))

    return workers


def _get_queue_items():
    """Return queued jobs across priority queues, highest priority first."""
    queue_items = []
    position = 1
    queue_specs = list(iter_priority_queue_names()) + [(config.DEFAULT_JOB_PRIORITY, config.QUEUE_NAME)]
    seen_queue_names = set()

    for priority, queue_name in queue_specs:
        if queue_name in seen_queue_names:
            continue
        seen_queue_names.add(queue_name)

        q = Queue(queue_name, connection=redis_conn)
        for job_id in q.get_job_ids():
            try:
                job = Job.fetch(job_id, connection=redis_conn)
                job_meta = job.meta or {}
                user_id = job_meta.get('user_id', 'unknown')
                script_name = job_meta.get('script_name')
                created_at = job_meta.get('created_at')
                job_priority = (
                    job_meta.get('priority')
                    if 'priority' in job_meta
                    else infer_priority_from_queue_name(queue_name, priority)
                )

                avg_job_time = 300
                estimated_seconds = position * avg_job_time
                if estimated_seconds < 60:
                    estimated_wait = f"{estimated_seconds} seconds"
                elif estimated_seconds < 3600:
                    estimated_wait = f"{estimated_seconds // 60} minutes"
                else:
                    hours = estimated_seconds // 3600
                    minutes = (estimated_seconds % 3600) // 60
                    estimated_wait = f"{hours}h {minutes}m"

                queue_items.append({
                    "position": position,
                    "job_id": job.id,
                    "user_id": user_id,
                    "script_name": script_name,
                    "status": job.get_status(),
                    "created_at": created_at,
                    "priority": job_priority,
                    "queue_name": queue_name,
                    "estimated_wait_time": estimated_wait,
                })
                position += 1
            except Exception as e:
                print(f"Error fetching job {job_id}: {e}")
                continue

    return queue_items


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _duration_ms(started_at, finished_at=None):
    started = _parse_iso(started_at)
    finished = _parse_iso(finished_at) or datetime.now(timezone.utc)
    if not started:
        return None
    return int((finished - started).total_seconds() * 1000)


def _percentiles(values):
    values = sorted([float(v) for v in values if v is not None])
    if not values:
        return {"avg": None, "p50": None, "p90": None, "p95": None, "p99": None, "max": None}

    def pct(percent):
        idx = min(len(values) - 1, max(0, int(round((percent / 100) * (len(values) - 1)))))
        return round(values[idx], 2)

    return {
        "avg": round(sum(values) / len(values), 2),
        "p50": pct(50),
        "p90": pct(90),
        "p95": pct(95),
        "p99": pct(99),
        "max": round(values[-1], 2),
    }


def _resource_peaks(current_system: Dict[str, Any], current_api_process: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate whole-server peaks from monitor resource samples."""
    samples = get_resource_series(redis_conn, limit=RESOURCE_LIMIT_FOR_PEAKS)
    if current_system:
        samples.append({
            "timestamp": iso_now(),
            "system": current_system,
            "api_process": current_api_process,
        })

    peaks = {
        "system_ram_used_mb": 0.0,
        "system_ram_percent": 0.0,
        "system_cpu_percent": 0.0,
        "api_process_rss_mb": 0.0,
        "api_process_cpu_percent": 0.0,
        "freecad_process_rss_mb": 0.0,
        "freecad_process_cpu_percent": 0.0,
        "timestamp": None,
    }

    for sample in samples:
        system = sample.get("system") or {}
        api_process = sample.get("api_process") or sample.get("worker_process") or {}
        freecad_process = sample.get("freecad_process") or {}
        system_ram = float(
            system.get("server_ram_used_mb")
            or system.get("system_ram_used_mb")
            or system.get("process_rss_mb")
            or system.get("ram_used_mb")
            or 0.0
        )
        system_cpu = float(
            system.get("server_cpu_percent")
            or system.get("system_cpu_percent")
            or system.get("process_cpu_percent")
            or system.get("cpu_percent")
            or 0.0
        )

        if system_ram >= peaks["system_ram_used_mb"]:
            peaks["system_ram_used_mb"] = round(system_ram, 2)
            peaks["system_ram_percent"] = round(
                float(system.get("system_ram_percent") or system.get("ram_percent") or 0.0),
                1,
            )
            peaks["timestamp"] = sample.get("timestamp")

        peaks["system_cpu_percent"] = max(
            peaks["system_cpu_percent"],
            round(system_cpu, 1),
        )
        peaks["api_process_rss_mb"] = max(
            peaks["api_process_rss_mb"],
            round(float(api_process.get("rss_mb") or 0.0), 2),
        )
        peaks["api_process_cpu_percent"] = max(
            peaks["api_process_cpu_percent"],
            round(float(api_process.get("cpu_percent") or 0.0), 1),
        )
        peaks["freecad_process_rss_mb"] = max(
            peaks["freecad_process_rss_mb"],
            round(float(freecad_process.get("rss_mb") or 0.0), 2),
        )
        peaks["freecad_process_cpu_percent"] = max(
            peaks["freecad_process_cpu_percent"],
            round(float(freecad_process.get("cpu_percent") or 0.0), 1),
        )

    return peaks


def _monitor_snapshot():
    workers = _get_all_workers_info()
    queue_items = _get_queue_items()
    all_job_snapshots = get_live_job_snapshots(redis_conn)
    live_jobs = [
        job for job in all_job_snapshots
        if job.get("status") not in {"complete", "partial_success", "failed"}
    ]
    history = get_history(redis_conn, limit=100)
    system = system_snapshot()
    api_process = current_process_snapshot()
    mqtt_connected = mqtt_manager.connected if hasattr(mqtt_manager, "connected") else False

    record_resource_sample(redis_conn, {
        "timestamp": iso_now(),
        "system": system,
        "api_process": api_process,
        "workers": workers,
        "queue_depth": len(queue_items),
        "busy_workers": sum(1 for worker in workers if worker.get("status") == "busy"),
    })

    completed_durations = [
        item.get("duration_ms") or _duration_ms(item.get("started_at"), item.get("finished_at"))
        for item in history
        if item.get("status") in {"complete", "partial_success", "failed"}
    ]
    priorities = {}
    for item in queue_items:
        key = str(item.get("priority", "unknown"))
        priorities[key] = priorities.get(key, 0) + 1

    return {
        "updated_at": iso_now(),
        "system": system,
        "api_process": api_process,
        "mqtt_connected": mqtt_connected,
        "workers": workers,
        "queue": queue_items,
        "queue_by_priority": priorities,
        "live_jobs": live_jobs,
        "history": history,
        "resource_peaks": _resource_peaks(system, api_process),
        "summary": {
            "total_workers": len(workers),
            "busy_workers": sum(1 for worker in workers if worker.get("status") == "busy"),
            "idle_workers": sum(1 for worker in workers if worker.get("status") == "idle"),
            "offline_workers": sum(1 for worker in workers if worker.get("status") == "offline"),
            "running_jobs": len([job for job in live_jobs if job.get("status") == "running"]),
            "queued_jobs": len(queue_items),
            "completed_jobs": len([job for job in history if job.get("status") in {"complete", "partial_success"}]),
            "failed_jobs": len([job for job in history if job.get("status") == "failed"]),
        },
        "latency_ms": _percentiles(completed_durations),
    }


@api.route('/health')
class Health(Resource):
    @api.doc('health_check')
    def get(self):
        """Health check endpoint"""
        return {"status": "ok", "time": iso_now()}


@app.route('/')
@app.route('/monitor')
def freecad_monitor_redirect():
    """Convenience redirect for local development."""
    return redirect('/freecad/monitor')




@app.route('/freecad/monitor')
def freecad_monitor_page():
    """FreeCAD runtime monitoring dashboard."""
    return render_template("freecad_monitor.html")


@freecad_ns.route('/monitor/overview')
class FreeCADMonitorOverview(Resource):
    @api.doc('freecad_monitor_overview')
    def get(self):
        """FreeCAD monitor overview for workers, queue, resources, and history."""
        try:
            return _monitor_snapshot()
        except Exception as e:
            api.abort(500, f"Failed to build monitor overview: {str(e)}")


@freecad_ns.route('/monitor/resources')
class FreeCADMonitorResources(Resource):
    @api.doc('freecad_monitor_resources')
    def get(self):
        """Return recent resource samples for charts."""
        try:
            limit = request.args.get("limit", default=300, type=int)
            return {
                "samples": get_resource_series(redis_conn, limit=limit),
                "updated_at": iso_now(),
            }
        except Exception as e:
            api.abort(500, f"Failed to get resource series: {str(e)}")


@freecad_ns.route('/monitor/jobs')
class FreeCADMonitorJobs(Resource):
    @api.doc('freecad_monitor_jobs')
    def get(self):
        """Return live jobs, queued jobs, and recent history."""
        try:
            limit = request.args.get("limit", default=100, type=int)
            live_jobs = [
                job for job in get_live_job_snapshots(redis_conn)
                if job.get("status") not in {"complete", "partial_success", "failed"}
            ]
            return {
                "live_jobs": live_jobs,
                "queue": _get_queue_items(),
                "history": get_history(redis_conn, limit=limit),
                "updated_at": iso_now(),
            }
        except Exception as e:
            api.abort(500, f"Failed to get monitor jobs: {str(e)}")


@freecad_ns.route('/monitor/export')
class FreeCADMonitorExport(Resource):
    @api.doc('freecad_monitor_export')
    def get(self):
        """Export FreeCAD monitor data as JSON."""
        try:
            return _monitor_snapshot()
        except Exception as e:
            api.abort(500, f"Failed to export monitor data: {str(e)}")


@freecad_ns.route('/generate')
class GenerateModel(Resource):
    @api.expect(upload_parser)
    @api.doc('generate_model')
    def post(self):
        """Create FreeCAD model from script file"""
        try:
            # Check file upload
            if 'file' not in request.files:
                api.abort(400, "No file uploaded")

            file = request.files['file']
            if file.filename == '':
                api.abort(400, "No file selected")

            if not file.filename.endswith('.py'):
                api.abort(400, "File must be a Python script (.py)")

            # Get user_id from form data
            user_id = request.form.get('user_id')
            if not user_id:
                api.abort(400, "user_id is required")
            try:
                priority = normalize_priority(request.form.get('priority'))
            except ValueError as e:
                api.abort(400, str(e))
            priority_queue_name = get_priority_queue_name(priority)
            priority_queue = get_priority_queue(priority)
            print(
                f"[API] /freecad/generate received user_id={user_id} "
                f"priority={priority} queue={priority_queue_name} "
                f"form_keys={list(request.form.keys())}"
            )

            auto_download_raw = request.form.get('auto_download', '')
            auto_download = str(auto_download_raw).lower() in ['1', 'true', 'yes', 'on']

            # ===== PHASE 2: New folder structure =====
            # Create folder structure: /app/storage/{user_id}/input/ and /app/storage/{user_id}/output/
            user_storage_dir = os.path.join(config.STORAGE_PATH, user_id)
            input_dir = os.path.join(user_storage_dir, "input")
            output_dir = os.path.join(user_storage_dir, "output")
            
            # Create directories if they don't exist
            os.makedirs(input_dir, exist_ok=True)
            os.makedirs(output_dir, exist_ok=True)
            
            # Save script to input/script.py (fixed name)
            script_path = os.path.join(input_dir, "script.py")
            file.save(script_path)
            
            # Check and save metadata file if provided
            metadata_saved = False
            if 'metadata_file' in request.files:
                metadata_file = request.files['metadata_file']
                if metadata_file and metadata_file.filename != '':
                    if not metadata_file.filename.endswith('.json'):
                        api.abort(400, "Metadata file must be a JSON file (.json)")
                    
                    metadata_path = os.path.join(input_dir, "metadata.json")
                    metadata_file.save(metadata_path)
                    
                    # Validate JSON format
                    try:
                        import json
                        with open(metadata_path, 'r', encoding='utf-8') as f:
                            metadata_content = json.load(f)
                        
                        # Basic validation: check for threaded_holes array
                        if 'threaded_holes' in metadata_content:
                            metadata_saved = True
                            print(f"[API] ✓ Metadata file saved with {len(metadata_content.get('threaded_holes', []))} threaded hole(s)")
                        else:
                            print(f"[API] ⚠️ Warning: Metadata file missing 'threaded_holes' array")
                            metadata_saved = True  # Still save it, converter will handle
                    except json.JSONDecodeError as e:
                        print(f"[API] ✗ Invalid JSON in metadata file: {e}")
                        api.abort(400, f"Invalid JSON in metadata file: {str(e)}")
                    except Exception as e:
                        print(f"[API] ⚠️ Warning: Could not validate metadata file: {e}")
                        metadata_saved = True  # Still save it
            
            # Pre-process script: replace sanitized_title and output_dir_abs
            # Use structured result handling similar to script_preprocessor.py
            try:
                preprocessed_path, preprocess_result = save_preprocessed_script(script_path, user_id, output_dir)
                
                # Log detailed results
                if preprocess_result.success:
                    print(f"[API] ✓ Successfully preprocessed script for user {user_id}")
                    if preprocess_result.changes:
                        print(f"[API] Changes made: {len(preprocess_result.changes)}")
                        for change in preprocess_result.changes:
                            print(f"[API]   - {change}")
                else:
                    print(f"[API] ⚠️ Preprocessing completed with issues for user {user_id}")
                    if preprocess_result.errors:
                        print(f"[API] Errors: {len(preprocess_result.errors)}")
                        for error in preprocess_result.errors:
                            print(f"[API]   ✗ {error}")
                    if preprocess_result.warnings:
                        print(f"[API] Warnings: {len(preprocess_result.warnings)}")
                        for warning in preprocess_result.warnings:
                            print(f"[API]   ⚠ {warning}")
                
                # If preprocessing failed critically, we should still continue
                # as the worker might handle edge cases, but log it clearly
                if not preprocess_result.success and preprocess_result.errors:
                    print(f"[API] ⚠️ Continuing despite preprocessing errors - worker will attempt execution")
                    
            except Exception as e:
                print(f"[API] ✗ [CRITICAL] Failed to preprocess script: {e}")
                import traceback
                traceback.print_exc()
                # Continue anyway - worker might handle it, but this is less ideal
                print(f"[API] ⚠️ Continuing without preprocessing - script may fail if variables are not set correctly")

            # Put job into the priority queue with user_id and output_dir
            job = priority_queue.enqueue(
                "worker.execute_freecad_script",
                script_path,
                user_id=user_id,
                output_dir=output_dir,  # Pass output_dir to worker
                meta={
                    "created_at": iso_now(), 
                    "script_name": file.filename, 
                    "user_id": user_id,
                    "output_dir": output_dir,
                    "priority": priority,
                    "queue_name": priority_queue_name
                },
                job_timeout=config.JOB_TIMEOUT
            )

            # Publish initial status to MQTT when job is queued
            mqtt_published = False
            try:
                if mqtt_manager.connected:
                    mqtt_manager.publish_status(
                        user_id,
                        "queued",
                        f"Job queued for script {file.filename}"
                    )
                    mqtt_manager.publish_progress(
                        user_id,
                        0,
                        "queued",
                        f"Job queued. Waiting for worker to start processing..."
                    )
                    mqtt_published = True
                    print(f"✓ Published initial MQTT status for user {user_id}")
                else:
                    print(f"⚠️ MQTT not connected, cannot publish for user {user_id}")
            except Exception as e:
                print(f"⚠️ Warning: Failed to publish initial MQTT status: {e}")
                import traceback
                traceback.print_exc()

            # Expected output files (standardized naming)
            expected_files = [
                f"{user_id}.step",
                f"{user_id}.obj",
                f"{user_id}.pdf"
            ]
            
            # Get file sizes for received files
            script_size = os.path.getsize(script_path) if os.path.exists(script_path) else 0
            metadata_size = 0
            metadata_info = None
            
            if metadata_saved:
                metadata_path = os.path.join(input_dir, "metadata.json")
                if os.path.exists(metadata_path):
                    metadata_size = os.path.getsize(metadata_path)
                    try:
                        import json
                        with open(metadata_path, 'r', encoding='utf-8') as f:
                            metadata_content = json.load(f)
                        threaded_holes_count = len(metadata_content.get('threaded_holes', []))
                        metadata_info = {
                            "filename": "metadata.json",
                            "size_bytes": metadata_size,
                            "threaded_holes_count": threaded_holes_count,
                            "path": f"storage/{user_id}/input/metadata.json"
                        }
                    except:
                        metadata_info = {
                            "filename": "metadata.json",
                            "size_bytes": metadata_size,
                            "path": f"storage/{user_id}/input/metadata.json"
                        }
            
            return {
                "user_id": user_id,
                "status": "queued",
                "storage_path": f"storage/{user_id}/",
                "priority": priority,
                "queue_name": priority_queue_name,
                "files_received": {
                    "python_script": {
                        "filename": file.filename,
                        "saved_as": "script.py",
                        "size_bytes": script_size,
                        "path": f"storage/{user_id}/input/script.py"
                    },
                    "metadata": metadata_info,
                    "user_id": user_id
                },
                "expected_output_files": expected_files,
                "message": f"✓ Received: Python script '{file.filename}' ({script_size} bytes)" + 
                          (f" + metadata.json ({metadata_size} bytes, {metadata_info.get('threaded_holes_count', 0)} threaded holes)" if metadata_saved else "") + 
                          f" for user '{user_id}'. Job queued for processing.",
                "created_at": job.meta.get("created_at"),
                "check_status_url": f"/freecad/status/{user_id}",
                "check_result_url": f"/freecad/result/{user_id}",
                "mqtt_published": mqtt_published
            }

        except HTTPException:
            raise
        except Exception as exc:
            api.abort(500, f"Failed to process file: {exc}")


@freecad_ns.route('/status/<string:user_id>')
class JobStatus(Resource):
    @api.marshal_with(status_response)
    @api.doc('get_job_status')
    def get(self, user_id):
        """Check job status by user_id with progress from MQTT"""
        try:
            # Check MQTT progress first
            mqtt_progress = mqtt_manager.get_progress(user_id)
            mqtt_connected = mqtt_manager.connected if hasattr(mqtt_manager, 'connected') else False

            if mqtt_progress:
                return {
                    "user_id": user_id,
                    "status": mqtt_progress.get("status", "unknown"),
                    "progress": mqtt_progress.get("progress", 0),
                    "message": mqtt_progress.get("message", ""),
                    "error": mqtt_progress.get("error"),
                    "updated_at": mqtt_progress.get("updated_at", iso_now()),
                    "data_source": "mqtt",
                    "mqtt_connected": mqtt_connected
                }

            # Fallback to Redis if no MQTT data
            job = find_job_for_user(user_id)

            if job:
                status = job.get_status()
                updated_at = iso_now()
                if job.ended_at:
                    updated_at = job.ended_at.replace(tzinfo=timezone.utc).isoformat()
                progress = 0
                if status == "started":
                    progress = 50
                elif status == "finished":
                    progress = 100
                return {
                    "user_id": user_id,
                    "status": status,
                    "progress": progress,
                    "message": f"Job {status} (from Redis queue)",
                    "error": None,
                    "updated_at": updated_at,
                    "data_source": "redis",
                    "mqtt_connected": mqtt_connected
                }

            api.abort(
                404,
                {
                    "message": "Job not found. It may still be initializing or has already expired.",
                    "user_id": user_id,
                },
            )
        except Exception as e:
            api.abort(
                404,
                {
                    "message": f"Job not found: {str(e)}",
                    "user_id": user_id,
                },
            )


@freecad_ns.route('/result/<string:user_id>')
class JobResult(Resource):
    @api.expect(result_parser)
    @api.marshal_with(result_response)
    @api.doc('get_job_result')
    def get(self, user_id):
        """Get job result by user_id. Returns files from storage/{user_id}/output/ directory."""
        try:
            # ===== PHASE 4: Direct folder access (no Redis loop) =====
            # Access output directory directly
            output_dir = os.path.join(config.STORAGE_PATH, user_id, "output")
            
            if not os.path.exists(output_dir):
                # Check if job is still running or queued across priority queues.
                job = find_job_for_user(user_id)
                if job:
                    status = job.get_status()
                    if status in ("started", "queued"):
                        response_status = "running" if status == "started" else "queued"
                        message = (
                            "Job is still running. Files will be available when complete."
                            if status == "started"
                            else "Job is still in queue."
                        )
                        return {
                            "user_id": user_id,
                            "status": response_status,
                            "message": message,
                            "files": [],
                            "total_files": 0,
                            "storage_path": f"storage/{user_id}/",
                            "completed_at": None,
                        }, 202
                
                api.abort(404, {
                    "message": "Result not found. The job may not have started yet or output directory does not exist.",
                    "user_id": user_id,
                })

            # List expected files with standardized naming
            expected_files = [
                {"type": "step", "filename": f"{user_id}.step"},
                {"type": "obj", "filename": f"{user_id}.obj"},
                {"type": "pdf", "filename": f"{user_id}.pdf"}
            ]
            
            # Check each file and create download URLs
            base_url = os.getenv('API_BASE_URL', f'http://{config.API_HOST}:{config.API_PORT}')
            files = []
            
            for file_info in expected_files:
                file_path = os.path.join(output_dir, file_info["filename"])
                if os.path.exists(file_path):
                    file_size = os.path.getsize(file_path)
                    download_url = f"{base_url}/freecad/download/{user_id}/{file_info['filename']}"
                    
                    files.append({
                        "type": file_info["type"],
                        "filename": file_info["filename"],
                        "path": f"storage/{user_id}/output/{file_info['filename']}",
                        "download_url": download_url,
                        "size": file_size
                    })
            
            return {
                "user_id": user_id,
                "status": "success",
                "files": files,
                "total_files": len(files),
                "storage_path": f"storage/{user_id}/",
                "completed_at": iso_now(),
            }
            
        except Exception as e:
            api.abort(
                500,
                {
                    "message": f"Error retrieving result: {str(e)}",
                    "user_id": user_id,
                },
            )


@freecad_ns.route('/download/<string:user_id>/<string:filename>')
class DownloadFile(Resource):
    @api.doc('download_file')
    def get(self, user_id, filename):
        """Download file by user_id and filename. Only allows downloading files belonging to the user."""
        try:
            # ===== PHASE 5: Security check with whitelist =====
            # Whitelist of allowed files for this user
            allowed_files = [
                f"{user_id}.step",
                f"{user_id}.obj",
                f"{user_id}.pdf",
                "script.py"  # Allow downloading the script
            ]
            
            # Security: Only allow downloading files that belong to this user
            if filename not in allowed_files:
                api.abort(403, f"Access denied: File '{filename}' is not allowed for user {user_id}")
            
            # Determine file path based on filename
            if filename == "script.py":
                # Script is in input directory
                file_path = os.path.join(config.STORAGE_PATH, user_id, "input", filename)
            else:
                # Output files are in output directory
                file_path = os.path.join(config.STORAGE_PATH, user_id, "output", filename)
            
            # Check if file exists
            if not os.path.exists(file_path):
                api.abort(404, f"File not found: {filename} for user {user_id}")
            
            # Additional security: Prevent path traversal attacks
            # Ensure the resolved path is within the user's storage directory
            resolved_path = os.path.abspath(file_path)
            user_storage_dir = os.path.abspath(os.path.join(config.STORAGE_PATH, user_id))
            if not resolved_path.startswith(user_storage_dir):
                api.abort(403, f"Access denied: Invalid file path")

            return send_file(
                file_path,
                as_attachment=True,
                download_name=filename,
                mimetype='application/octet-stream'
            )

        except Exception as e:
            api.abort(500, f"Download failed: {str(e)}")


@freecad_ns.route('/template/oblong')
class OblongTemplate(Resource):
    @api.doc('get_oblong_template')
    def get(self):
        """Get oblong.py template script"""
        return {
            "script_name": "oblong.py",
            "description": "Oblong plate generator script",
            "usage": "Upload this script file to /freecad/generate endpoint"
        }


@freecad_ns.route('/workers/status')
class AllWorkersStatus(Resource):
    @api.marshal_with(workers_status_response)
    @api.doc('get_all_workers_status')
    def get(self):
        """Get detailed status of all workers - shows which worker is processing which user"""
        try:
            workers = _get_all_workers_info()

            # Count workers by status
            busy_count = sum(1 for w in workers if w['status'] == 'busy')
            idle_count = sum(1 for w in workers if w['status'] == 'idle')
            offline_count = sum(1 for w in workers if w['status'] == 'offline')

            return {
                "workers": workers,
                "total_workers": len(workers),
                "busy_workers": busy_count,
                "idle_workers": idle_count,
                "offline_workers": offline_count,
                "updated_at": iso_now()
            }
        except Exception as e:
            api.abort(500, f"Failed to get workers status: {str(e)}")


@freecad_ns.route('/queue')
class QueueStatus(Resource):
    @api.marshal_with(queue_response)
    @api.doc('get_queue_status')
    def get(self):
        """Get list of all jobs in queue waiting to be processed"""
        try:
            queue_items = _get_queue_items()
            return {
                "queue": queue_items,
                "total_queued": len(queue_items),
                "updated_at": iso_now()
            }
        except Exception as e:
            api.abort(500, f"Failed to get queue status: {str(e)}")




@freecad_ns.route('/download-script/<string:user_id>')
class DownloadLatestScript(Resource):
    @api.doc('download_latest_script')
    def get(self, user_id):
        """Download the script for a given user_id from storage/{user_id}/input/script.py"""
        try:
            # ===== PHASE 7: Direct path access =====
            script_path = os.path.join(config.STORAGE_PATH, user_id, "input", "script.py")
            
            if not os.path.exists(script_path):
                api.abort(404, f"Script not found for user {user_id}")
            
            return send_file(
                script_path,
                as_attachment=True,
                download_name="script.py",
                mimetype='text/x-python'
            )

        except Exception as e:
            api.abort(500, f"Download script failed: {str(e)}")


@freecad_ns.route('/python/<string:user_id>')
class GetPythonScript(Resource):
    @api.doc('get_python_script')
    def get(self, user_id):
        """Get Python script content by user_id from storage/{user_id}/input/script.py"""
        try:
            # Get script path from input directory
            script_path = os.path.join(config.STORAGE_PATH, user_id, "input", "script.py")
            
            if not os.path.exists(script_path):
                api.abort(404, {
                    "message": f"Python script not found for user {user_id}",
                    "user_id": user_id,
                    "expected_path": f"storage/{user_id}/input/script.py"
                })
            
            # Read file content
            try:
                with open(script_path, 'r', encoding='utf-8') as f:
                    script_content = f.read()
            except UnicodeDecodeError:
                # Try with different encoding if UTF-8 fails
                with open(script_path, 'r', encoding='latin-1') as f:
                    script_content = f.read()
            
            # Get file metadata
            file_size = os.path.getsize(script_path)
            modified_time = os.path.getmtime(script_path)
            
            return {
                "user_id": user_id,
                "filename": "script.py",
                "content": script_content,
                "size": file_size,
                "modified_at": datetime.fromtimestamp(modified_time, tz=timezone.utc).isoformat(),
                "path": f"storage/{user_id}/input/script.py"
            }
            
        except Exception as e:
            api.abort(500, {
                "message": f"Error reading Python script: {str(e)}",
                "user_id": user_id
            })


if __name__ == "__main__":
    app.run(host=config.API_HOST, port=config.API_PORT, debug=True)
