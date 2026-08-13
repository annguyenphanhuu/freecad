import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import psutil
except Exception:  # pragma: no cover - monitor must not break the API if psutil is absent.
    psutil = None


JOB_KEY_PREFIX = "freecad_monitor:job:"
HISTORY_KEY = "freecad_monitor:history"
RESOURCE_SERIES_KEY = "freecad_monitor:resource_series"
DEFAULT_TTL_SECONDS = int(os.getenv("MONITOR_JOB_TTL_SECONDS", str(24 * 60 * 60)))
HISTORY_LIMIT = int(os.getenv("MONITOR_HISTORY_LIMIT", "300"))
RESOURCE_LIMIT = int(os.getenv("MONITOR_RESOURCE_LIMIT", "1200"))


def iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def mb(value: float) -> float:
    return round(float(value) / (1024 * 1024), 2)


def _safe_cpu_percent(process) -> float:
    try:
        return round(float(process.cpu_percent(interval=None)), 1)
    except Exception:
        return 0.0


def system_snapshot() -> Dict[str, Any]:
    if psutil is None:
        return {
            "timestamp": iso_now(),
            "available": False,
            "error": "psutil is not installed",
        }

    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    ram_total_mb = mb(memory.total)
    ram_used_mb = mb(memory.used)
    ram_available_mb = mb(memory.available)
    cpu_percent = round(float(psutil.cpu_percent(interval=None)), 1)
    per_core_list = psutil.cpu_percent(interval=None, percpu=True)
    cpu_per_core = {
        f"core_{i}": round(float(value), 1)
        for i, value in enumerate(per_core_list)
    }
    cpu_count_physical = psutil.cpu_count(logical=False) or 0
    container_ram_limit_mb = None
    try:
        for cgroup_path in [
            "/sys/fs/cgroup/memory.max",
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
        ]:
            if os.path.exists(cgroup_path):
                with open(cgroup_path) as cgroup_file:
                    raw_limit = cgroup_file.read().strip()
                if raw_limit not in ("max", ""):
                    limit_bytes = int(raw_limit)
                    if limit_bytes < (1 << 40):
                        container_ram_limit_mb = mb(limit_bytes)
                break
    except Exception:
        pass

    return {
        "timestamp": iso_now(),
        "available": True,
        "monitor_scope": "system",
        "cpu_percent": cpu_percent,
        "server_cpu_percent": cpu_percent,
        "cpu_count": psutil.cpu_count() or 0,
        "cpu_count_physical": cpu_count_physical,
        "cpu_per_core": cpu_per_core,
        "ram_total_mb": ram_total_mb,
        "ram_used_mb": ram_used_mb,
        "ram_available_mb": ram_available_mb,
        "ram_percent": round(float(memory.percent), 1),
        "server_ram_used_mb": ram_used_mb,
        "system_ram_total_mb": ram_total_mb,
        "system_ram_used_mb": ram_used_mb,
        "system_ram_available_mb": ram_available_mb,
        "system_ram_percent": round(float(memory.percent), 1),
        "system_cpu_percent": cpu_percent,
        "container_ram_limit_mb": container_ram_limit_mb,
        "swap_used_mb": mb(swap.used),
        "swap_total_mb": mb(swap.total),
        "swap_percent": round(float(swap.percent), 1),
        "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
    }


def process_snapshot(pid: Optional[int] = None, include_children: bool = True) -> Dict[str, Any]:
    if psutil is None:
        return {"available": False, "error": "psutil is not installed"}

    pid = pid or os.getpid()
    try:
        process = psutil.Process(pid)
        children = process.children(recursive=True) if include_children else []
        processes = [process] + children
        rss_mb = 0.0
        cpu_percent = 0.0
        child_rows = []

        for proc in processes:
            try:
                with proc.oneshot():
                    mem = proc.memory_info()
                    cpu = _safe_cpu_percent(proc)
                    rss_mb += mb(mem.rss)
                    cpu_percent += cpu
                    if proc.pid != pid:
                        child_rows.append({
                            "pid": proc.pid,
                            "name": proc.name(),
                            "status": proc.status(),
                            "rss_mb": mb(mem.rss),
                            "cpu_percent": cpu,
                        })
            except Exception:
                continue

        return {
            "available": True,
            "pid": pid,
            "name": process.name(),
            "status": process.status(),
            "rss_mb": round(rss_mb, 2),
            "cpu_percent": round(cpu_percent, 1),
            "child_count": len(children),
            "children": child_rows[:20],
        }
    except Exception as exc:
        return {"available": False, "pid": pid, "error": str(exc)}


def current_process_snapshot() -> Dict[str, Any]:
    return process_snapshot(os.getpid(), include_children=True)


def freecad_process_snapshot(pid: Optional[int]) -> Dict[str, Any]:
    if not pid:
        return {"available": False, "pid": None}
    return process_snapshot(pid, include_children=True)


def record_resource_sample(redis_conn, sample: Dict[str, Any]) -> None:
    try:
        redis_conn.lpush(RESOURCE_SERIES_KEY, json.dumps(sample))
        redis_conn.ltrim(RESOURCE_SERIES_KEY, 0, RESOURCE_LIMIT - 1)
    except Exception:
        pass


def get_resource_series(redis_conn, limit: int = 300) -> List[Dict[str, Any]]:
    try:
        rows = redis_conn.lrange(RESOURCE_SERIES_KEY, 0, max(0, min(limit, RESOURCE_LIMIT) - 1))
    except Exception:
        return []
    samples = []
    for row in rows:
        try:
            samples.append(json.loads(row.decode() if isinstance(row, bytes) else row))
        except Exception:
            continue
    samples.reverse()
    return samples


def record_job_snapshot(redis_conn, user_id: str, data: Dict[str, Any], ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
    if not user_id:
        return

    key = f"{JOB_KEY_PREFIX}{user_id}"
    data = dict(data)
    data["user_id"] = user_id
    data["updated_at"] = iso_now()

    try:
        redis_conn.setex(key, ttl_seconds, json.dumps(data))
    except Exception:
        pass


def append_history(redis_conn, data: Dict[str, Any]) -> None:
    try:
        payload = dict(data)
        payload.setdefault("updated_at", iso_now())
        redis_conn.lpush(HISTORY_KEY, json.dumps(payload))
        redis_conn.ltrim(HISTORY_KEY, 0, HISTORY_LIMIT - 1)
    except Exception:
        pass


def get_live_job_snapshots(redis_conn) -> List[Dict[str, Any]]:
    jobs = []
    try:
        keys = list(redis_conn.scan_iter(f"{JOB_KEY_PREFIX}*"))
    except Exception:
        return jobs

    for key in keys:
        try:
            raw = redis_conn.get(key)
            if raw:
                jobs.append(json.loads(raw.decode() if isinstance(raw, bytes) else raw))
        except Exception:
            continue
    jobs.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return jobs


def get_history(redis_conn, limit: int = 100) -> List[Dict[str, Any]]:
    try:
        rows = redis_conn.lrange(HISTORY_KEY, 0, max(0, min(limit, HISTORY_LIMIT) - 1))
    except Exception:
        return []
    history = []
    for row in rows:
        try:
            history.append(json.loads(row.decode() if isinstance(row, bytes) else row))
        except Exception:
            continue
    return history


class JobResourceTracker:
    def __init__(self, redis_conn, user_id: str, worker_id: str, job_id: str = None, priority: int = None, queue_name: str = None):
        self.redis_conn = redis_conn
        self.user_id = user_id
        self.worker_id = worker_id
        self.job_id = job_id
        self.priority = priority
        self.queue_name = queue_name
        self.started_at = iso_now()
        self.peak = {
            "system_ram_mb": 0.0,
            "system_cpu_percent": 0.0,
            "worker_rss_mb": 0.0,
            "worker_cpu_percent": 0.0,
            "freecad_rss_mb": 0.0,
            "freecad_cpu_percent": 0.0,
        }

    def sample(self, stage: str, status: str = "running", progress: int = None, freecad_pid: int = None, message: str = "") -> Dict[str, Any]:
        system = system_snapshot()
        worker = current_process_snapshot()
        freecad = freecad_process_snapshot(freecad_pid)

        self.peak["system_ram_mb"] = max(self.peak["system_ram_mb"], float(system.get("ram_used_mb") or 0.0))
        self.peak["system_cpu_percent"] = max(self.peak["system_cpu_percent"], float(system.get("cpu_percent") or 0.0))
        self.peak["worker_rss_mb"] = max(self.peak["worker_rss_mb"], float(worker.get("rss_mb") or 0.0))
        self.peak["worker_cpu_percent"] = max(self.peak["worker_cpu_percent"], float(worker.get("cpu_percent") or 0.0))
        self.peak["freecad_rss_mb"] = max(self.peak["freecad_rss_mb"], float(freecad.get("rss_mb") or 0.0))
        self.peak["freecad_cpu_percent"] = max(self.peak["freecad_cpu_percent"], float(freecad.get("cpu_percent") or 0.0))

        snapshot = {
            "user_id": self.user_id,
            "job_id": self.job_id,
            "worker_id": self.worker_id,
            "priority": self.priority,
            "queue_name": self.queue_name,
            "status": status,
            "stage": stage,
            "progress": progress,
            "message": message,
            "started_at": self.started_at,
            "system": system,
            "worker_process": worker,
            "freecad_process": freecad,
            "peaks": dict(self.peak),
            "updated_at": iso_now(),
        }
        record_job_snapshot(self.redis_conn, self.user_id, snapshot)
        record_resource_sample(self.redis_conn, {
            "timestamp": snapshot["updated_at"],
            "user_id": self.user_id,
            "status": status,
            "stage": stage,
            "system": system,
            "worker_process": worker,
            "freecad_process": freecad,
            "peaks": dict(self.peak),
        })
        return snapshot

    def finish(self, status: str, message: str = "", progress: int = 100, freecad_pid: int = None, details: Dict[str, Any] = None) -> Dict[str, Any]:
        snapshot = self.sample("finished", status=status, progress=progress, freecad_pid=freecad_pid, message=message)
        snapshot["finished_at"] = iso_now()
        snapshot["details"] = details or {}
        try:
            started = datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
            finished = datetime.fromisoformat(snapshot["finished_at"].replace("Z", "+00:00"))
            snapshot["duration_ms"] = int((finished - started).total_seconds() * 1000)
        except Exception:
            snapshot["duration_ms"] = None
        record_job_snapshot(self.redis_conn, self.user_id, snapshot)
        append_history(self.redis_conn, snapshot)
        return snapshot
