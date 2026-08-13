import os
import sys
import tempfile
import shutil
import subprocess
import uuid
import threading
import time
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List
from pathlib import Path
from mqtt_client import get_mqtt_manager
from redis import Redis
from freecad_monitor import JobResourceTracker

STORAGE_PATH = os.getenv("STORAGE_PATH", "/app/storage")
os.makedirs(STORAGE_PATH, exist_ok=True)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Add src/utils and src/core to Python path for technical drawing generator and step converter
sys.path.append('/app/src/utils')
sys.path.append('/app/src/core')


def get_worker_id() -> str:
    """Get current RQ worker name or generate a fallback ID"""
    try:
        from rq import get_current_job
        job = get_current_job()
        if job and job.worker_name:
            return job.worker_name
    except:
        pass

    # Fallback: generate unique ID
    return f"worker-{uuid.uuid4().hex[:8]}"


def get_current_job_context() -> Dict[str, Any]:
    """Return RQ job context for monitor metadata when this function runs in a worker."""
    try:
        from rq import get_current_job
        job = get_current_job()
        if not job:
            return {}
        meta = job.meta or {}
        return {
            "job_id": job.id,
            "priority": meta.get("priority"),
            "queue_name": meta.get("queue_name") or getattr(job, "origin", None),
        }
    except Exception:
        return {}


def generate_pdf_from_step(step_file_path: str, user_id: str) -> Dict[str, Any]:
    """
    Generate PDF from STEP file using technical_drawing_generator.
    Saves PDF to the same directory as the STEP file.
    """
    try:
        from technical_drawing_generator import generate_technical_drawing_from_step

        print(f"Generating PDF from STEP file: {step_file_path} for user: {user_id}")

        # Use same directory as STEP file for output
        step_path = Path(step_file_path)
        output_dir = step_path.parent
        pdf_output_dir = Path(output_dir) / f"pdf_temp_{user_id}"
        pdf_output_dir.mkdir(parents=True, exist_ok=True)

        # Use standardized naming: {user_id}.pdf
        base_filename = user_id

        # Call technical drawing generator
        result = generate_technical_drawing_from_step(
            step_path,
            pdf_output_dir,
            base_filename
        )

        if result["success"] and result["pdf_path"]:
            # Copy PDF to same directory as STEP file with standardized name
            pdf_source = Path(result["pdf_path"])
            pdf_dest = output_dir / f"{user_id}.pdf"
            shutil.copy2(pdf_source, pdf_dest)

            # Cleanup temp directory
            shutil.rmtree(pdf_output_dir, ignore_errors=True)

            return {
                "status": "success",
                "pdf_path": str(pdf_dest),
                "filename": pdf_dest.name,
                "message": result["message"]
            }
        else:
            return {
                "status": "failed",
                "error": result["message"]
            }

    except ImportError as e:
        print(f"Warning: Technical drawing generator not available: {e}")
        return {
            "status": "failed",
            "error": "PDF generation not available - technical_drawing_generator not found"
        }
    except Exception as e:
        print(f"Error generating PDF: {e}")
        return {
            "status": "failed",
            "error": f"PDF generation failed: {str(e)}"
        }


def generate_json_from_step(step_file_path: str, user_id: str, metadata_path: str = None) -> Dict[str, Any]:
    """
    Generate JSON from STEP file using step_converter.
    Saves JSON to the same directory as the STEP file.
    
    Args:
        step_file_path: Path to STEP file
        user_id: User ID for naming
        metadata_path: Optional path to metadata JSON file with threaded holes info
    """
    try:
        print(f"Generating JSON from STEP file: {step_file_path} for user: {user_id}")
        if metadata_path:
            print(f"Using metadata file: {metadata_path}")

        # Use same directory as STEP file for output
        step_path = Path(step_file_path)
        output_dir = step_path.parent
        # Use standardized naming: {user_id}.json
        json_dest = output_dir / f"{user_id}.json"

        # Create script to run step_converter with optional metadata
        # If metadata_path is provided, pass it as 3rd argument
        if metadata_path and os.path.exists(metadata_path):
            converter_script = f"""
import sys
sys.path.append('/app/src/core')
from step_converter import OnShapeJSONConverter

converter = OnShapeJSONConverter()
success = converter.convert('{step_file_path}', '{json_dest}', '{metadata_path}')
if success:
    print("JSON conversion completed successfully!")
else:
    print("JSON conversion failed!")
    sys.exit(1)
"""
            print(f"[Worker] Using metadata file for threaded holes enrichment")
        else:
            converter_script = f"""
import sys
sys.path.append('/app/src/core')
from step_converter import OnShapeJSONConverter

converter = OnShapeJSONConverter()
success = converter.convert('{step_file_path}', '{json_dest}')
if success:
    print("JSON conversion completed successfully!")
else:
    print("JSON conversion failed!")
    sys.exit(1)
"""

        # Save temporary script in output directory
        script_path = output_dir / f"converter_{user_id}.py"
        with open(script_path, 'w') as f:
            f.write(converter_script)

        # Run script with freecadcmd (increased timeout for heavy files)
        print(f"Running JSON converter script: {script_path}")
        result = subprocess.run(
            ["freecadcmd", str(script_path)],
            capture_output=True,
            text=True,
            timeout=300  # 5 minutes timeout
        )

        # Cleanup script
        try:
            os.remove(script_path)
        except:
            pass

        if result.returncode != 0:
            # Extract specific FreeCAD exception from output
            specific_exception = _extract_freecad_exception(result.stdout or "", result.stderr or "")

            if specific_exception:
                error_msg = f"Failed to generate JSON file: {specific_exception}"
            else:
                error_msg = f"Failed to generate JSON file: FreeCAD command failed with return code {result.returncode}"
                if result.stderr:
                    error_msg += f" - {result.stderr[:500]}"  # Limit error message length

            print(f"JSON generation failed: {error_msg}")
            print(f"FreeCAD stdout: {result.stdout[:500] if result.stdout else 'No output'}")
            return {
                "status": "failed",
                "error": error_msg,
                "specific_exception": specific_exception,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }

        if json_dest.exists():
            print(f"JSON file created successfully: {json_dest}")
            return {
                "status": "success",
                "json_path": str(json_dest),
                "filename": json_dest.name,
                "message": "JSON conversion completed successfully"
            }
        else:
            # Extract specific FreeCAD exception from output even if returncode was 0
            specific_exception = _extract_freecad_exception(result.stdout or "", result.stderr or "")

            if specific_exception:
                error_msg = f"Failed to generate JSON file: {specific_exception}"
            else:
                error_msg = f"Failed to generate JSON file: JSON file was not created at {json_dest}"

            print(f"JSON generation failed: {error_msg}")
            if result.stdout:
                print(f"FreeCAD stdout: {result.stdout[:500]}")
            if result.stderr:
                print(f"FreeCAD stderr: {result.stderr[:500]}")
            return {
                "status": "failed",
                "error": error_msg,
                "specific_exception": specific_exception,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }

    except ImportError as e:
        print(f"Warning: Step converter not available: {e}")
        return {
            "status": "failed",
            "error": "JSON generation not available - step_converter not found"
        }
    except subprocess.TimeoutExpired as e:
        error_msg = "JSON generation timed out after 10 minutes. This may happen with very large/complex STEP files."
        print(f"Error generating JSON: {error_msg}")
        print(f"Exception: {e}")
        # Cleanup script on timeout
        try:
            step_path = Path(step_file_path)
            output_dir = step_path.parent
            script_path = output_dir / f"converter_{user_id}.py"
            if script_path.exists():
                os.remove(script_path)
        except:
            pass
        return {
            "status": "failed",
            "error": error_msg,
            "exception": str(e),
        }
    except Exception as e:
        error_msg = f"JSON generation failed: {str(e)}"
        print(f"Error generating JSON: {error_msg}")
        import traceback
        tb = traceback.format_exc()
        print(f"Traceback: {tb}")
        return {
            "status": "failed",
            "error": error_msg,
            "traceback": tb,
        }



def _validate_generated_files(file_paths: List[str]) -> Dict[str, Any]:
    """
    Validate that provided files exist, are readable, and have valid content.
    - Requires size > 0 and basic readability
    - STEP: first line should start with 'ISO-10303'
    - JSON: must be valid JSON

    Returns a dict with:
      {
        "valid": bool,
        "files": List[Dict[str, Any]],  # valid files with metadata
        "errors": List[str]
      }
    """
    valid_files: List[Dict[str, Any]] = []
    errors: List[str] = []

    for file_path in file_paths:
        try:
            p = Path(file_path)
            if not p.exists():
                errors.append(f"File not found: {file_path}")
                continue

            try:
                size = p.stat().st_size
            except Exception as e:
                errors.append(f"Cannot stat file: {file_path} - {e}")
                continue

            if size <= 0:
                errors.append(f"File is empty (0 bytes): {file_path}")
                continue

            # Basic readability
            try:
                with open(p, 'rb') as fh:
                    fh.read(1)
            except Exception as e:
                errors.append(f"File not readable: {file_path} - {e}")
                continue

            fext = p.suffix.lower()

            # STEP validation
            if fext == '.step':
                try:
                    with open(p, 'r', errors='ignore') as fh:
                        first_line = fh.readline()
                        if not first_line.startswith('ISO-10303'):
                            errors.append(f"Invalid STEP format (missing ISO-10303 header): {file_path}")
                            continue
                except Exception as e:
                    errors.append(f"Cannot validate STEP file: {file_path} - {e}")
                    continue

            # JSON validation
            if fext == '.json':
                try:
                    with open(p, 'r') as fh:
                        json.load(fh)
                except Exception as e:
                    errors.append(f"Invalid JSON format: {file_path} - {e}")
                    continue

            valid_files.append({
                "path": str(p),
                "filename": p.name,
                "size": size,
                "type": fext[1:] if len(fext) > 1 else ""
            })

        except Exception as ex:
            errors.append(f"Unexpected validation error for {file_path}: {ex}")

    return {
        "valid": len(errors) == 0,
        "files": valid_files,
        "errors": errors,
    }


def _build_fast_wrapper_script(script_path: str) -> str:
    """
    Wrap the generated FreeCAD script with two speed measures, entirely on
    the tolery-freecad side -- no change to the script-generation template
    in the other repo, and no change to the generated script's own content.

    1. .brep sibling export via Import.export() interception: instead of
       guessing which output directory the script wrote to and which
       document object is "the" shape (both heuristics, and both break the
       moment a different shape/hole-type deviates from what was tested),
       monkeypatch Import.export itself. Every shape category the codegen
       template supports funnels through the SAME single call --
       `Import.export([main_object], step_path)` (src/core/templates.py:1566
       in the other repo, confirmed the one and only STEP-export call
       mandated for all shapes, not perforated-specific). Intercepting it
       gives us the EXACT objects and EXACT path the script itself used, no
       matter the hole shape (R/C/LR/LC) or pitch (U/T/Z) or shape category
       entirely -- so a .brep sibling gets written for anything the template
       can produce, not just the one case this was tested against.
       step_converter.py's import_step_file() already has a fast path that
       uses a sibling .brep instead of re-parsing .step (measured 120-430x
       faster: ~0.65s vs ~75s on a 4428-hole sheet, verified). This does NOT
       cache anything across two DIFFERENT requests/shapes -- it only avoids
       parsing the STEP geometry TWICE (once implicitly when exported, once
       again when step_converter.py reads it back) within this one
       request's own STEP->JSON round trip.

    2. Coarser OBJ mesh: MeshPart.meshFromShape is monkeypatched to floor
       LinearDeflection/AngularDeflection at a coarser value, regardless of
       what the generated script's OBJ-export loop requests. The .obj file
       is not read by json_viewer.js (confirmed: it builds geometry only
       from the JSON) so trading its visual fidelity for wall-clock time is
       safe as far as the 3D JSON viewer is concerned; on a perforated sheet
       the OBJ loop calls meshFromShape once per face (thousands of tiny
       hole side-walls), so this should compound. This patch is likewise
       generic (same canonical footer template for every shape category),
       not perforated-specific.

    Falls back to running script_path unchanged (return script_path itself)
    if the wrapper can't be written for any reason.
    """
    try:
        wrapper_path = script_path + ".fastwrap.py"
        with open(script_path, "r", encoding="utf-8") as f:
            original_source = f.read()

        wrapper_code = f'''# Auto-generated by tolery-freecad/worker.py -- does not modify the
# original generated script or the script-generation template in the other
# repo. See _build_fast_wrapper_script() docstring for what/why.
import os

_ORIG_SCRIPT = {script_path!r}
_src = {original_source!r}
import time as _time
_wrap_t_start = _time.perf_counter()
_wrap_timing = {{"mesh_from_shape": 0.0, "mesh_from_shape_calls": 0, "cut": 0.0, "cut_calls": 0}}

# --- (2) coarsen OBJ mesh deflection, regardless of what the script asks for ---
try:
    import MeshPart as _MeshPart
    _orig_meshFromShape = _MeshPart.meshFromShape
    _MIN_LINEAR_DEFLECTION = 0.8   # mm  (template default is 0.1mm)
    _MIN_ANGULAR_DEFLECTION = 1.0  # rad (template default is 0.523599 rad)

    def _coarse_meshFromShape(*args, **kwargs):
        if kwargs.get("LinearDeflection") is not None:
            kwargs["LinearDeflection"] = max(kwargs["LinearDeflection"], _MIN_LINEAR_DEFLECTION)
        if kwargs.get("AngularDeflection") is not None:
            kwargs["AngularDeflection"] = max(kwargs["AngularDeflection"], _MIN_ANGULAR_DEFLECTION)
        _t0 = _time.perf_counter()
        _result = _orig_meshFromShape(*args, **kwargs)
        _wrap_timing["mesh_from_shape"] += _time.perf_counter() - _t0
        _wrap_timing["mesh_from_shape_calls"] += 1
        return _result

    _MeshPart.meshFromShape = _coarse_meshFromShape
except Exception as _e:
    print(f"[FASTWRAP] Could not patch MeshPart.meshFromShape: {{_e}}")

# --- timing only: wrap Part.Shape.cut (Boolean cut is the other suspected
# hot spot -- a single compound-tool cut on a shape with thousands of hole
# cylinders). Best-effort: some FreeCAD builds expose Shape as an immutable
# C extension type that refuses attribute assignment: if so this silently
# no-ops and we just won't get a cut-specific number this run.
try:
    import Part as _Part
    _orig_cut = _Part.Shape.cut

    def _timed_cut(self, *args, **kwargs):
        _t0 = _time.perf_counter()
        _result = _orig_cut(self, *args, **kwargs)
        _wrap_timing["cut"] += _time.perf_counter() - _t0
        _wrap_timing["cut_calls"] += 1
        return _result

    _Part.Shape.cut = _timed_cut
except Exception as _e:
    print(f"[FASTWRAP] Could not time Part.Shape.cut (non-fatal): {{_e}}")

# --- (1) intercept Import.export() to write a .brep sibling using the EXACT
# objects + EXACT path the script itself passed -- no directory guessing,
# no "assume the first shape object" heuristic. Works for any shape/hole
# type since every generated script funnels STEP export through this one
# call.
try:
    import Import as _Import
    import Part as _Part
    _orig_export = _Import.export

    def _capturing_export(objs, filename, *args, **kwargs):
        result = _orig_export(objs, filename, *args, **kwargs)
        try:
            if isinstance(filename, str) and filename.lower().endswith((".step", ".stp")):
                shapes = [o.Shape for o in objs if hasattr(o, "Shape") and not o.Shape.isNull()]
                if shapes:
                    combined = shapes[0] if len(shapes) == 1 else _Part.makeCompound(shapes)
                    brep_path = os.path.splitext(filename)[0] + ".brep"
                    combined.exportBrep(brep_path)
                    print(f"[FASTWRAP] Wrote sibling .brep: {{brep_path}}")
        except Exception as _e:
            print(f"[FASTWRAP] .brep export failed for {{filename}}: {{_e}}")
        return result

    _Import.export = _capturing_export
except Exception as _e:
    print(f"[FASTWRAP] Could not patch Import.export: {{_e}}")

# --- run the original script exactly as generated (same __file__, so its
# own path-setup logic resolves identically to running it directly) ---
_exec_globals = {{"__file__": _ORIG_SCRIPT, "__name__": "__main__"}}
exec(compile(_src, _ORIG_SCRIPT, "exec"), _exec_globals)

_wrap_timing["total"] = _time.perf_counter() - _wrap_t_start
print("[FASTWRAP_TIMING] " + " | ".join(f"{{k}}={{v:.2f}}" if isinstance(v, float) else f"{{k}}={{v}}" for k, v in _wrap_timing.items()))
'''
        with open(wrapper_path, "w", encoding="utf-8") as f:
            f.write(wrapper_code)
        return wrapper_path
    except Exception as e:
        print(f"[WARNING] Failed to build fast wrapper script, running original unmodified: {e}")
        return script_path


def execute_freecad_script(script_path: str, user_id: str = None, output_dir: str = None) -> Dict[str, Any]:
    """
    Execute FreeCAD script using freecadcmd with comprehensive error handling and validation.
    
    Args:
        script_path: Path to the FreeCAD Python script
        user_id: User ID for job tracking and file naming
        output_dir: Target output directory (if None, uses temp directory)
    
    Returns:
        Dict with status, files, errors, and detailed information
    """
    _wall_t0 = time.perf_counter()
    print(f"[WALLCLOCK] execute_freecad_script entered at t=0.00s (real time {time.strftime('%H:%M:%S')})")
    _wc_events = {"entered": 0.0}

    def _wc_mark(label):
        _wc_events[label] = time.perf_counter() - _wall_t0

    def _wc_dump():
        # Persisted to output_dir (not stdout) because `conda run -n base rq
        # worker ...` buffers/drops stdout from long-lived child processes,
        # so print(f"[WALLCLOCK] ...") never reaches `docker logs`. This file
        # is the only reliable way to see per-stage timing for a real job.
        try:
            _dump_dir = output_dir if output_dir else "/tmp"
            os.makedirs(_dump_dir, exist_ok=True)
            with open(os.path.join(_dump_dir, f"_wallclock_{user_id}.json"), "w", encoding="utf-8") as _f:
                json.dump(_wc_events, _f, indent=2)
        except Exception:
            pass
    # Get worker ID from RQ context
    worker_id = get_worker_id()
    job_context = get_current_job_context()
    monitor_tracker = JobResourceTracker(
        Redis.from_url(REDIS_URL),
        user_id=user_id,
        worker_id=worker_id,
        job_id=job_context.get("job_id"),
        priority=job_context.get("priority"),
        queue_name=job_context.get("queue_name"),
    )

    mqtt_manager = get_mqtt_manager()
    
    # Structured result tracking (similar to PreprocessorResult pattern)
    execution_result = {
        "success": False,
        "warnings": [],
        "errors": [],
        "changes": [],
        "method_used": "freecadcmd",
        "files_generated": [],
        "validation_passed": False
    }

    try:
        monitor_tracker.sample(
            "initializing",
            status="running",
            progress=5,
            message="Worker picked up job and is validating inputs",
        )

        # Validate script path exists
        if not os.path.exists(script_path):
            error_msg = f"Script file not found: {script_path}"
            execution_result["errors"].append(error_msg)
            mqtt_manager.publish_final_status(
                user_id=user_id,
                status="failed",
                message=error_msg,
                details={"script_path": script_path},
                worker_id=worker_id
            )
            monitor_tracker.finish(
                status="failed",
                message=error_msg,
                progress=0,
                details={"script_path": script_path},
            )
            return {
                "status": "failed",
                "error": error_msg,
                "execution_result": execution_result
            }
        
        # Use provided output_dir or create temp directory
        if output_dir:
            user_output_dir = output_dir
            os.makedirs(user_output_dir, exist_ok=True)
            execution_result["changes"].append(f"Using provided output directory: {output_dir}")
        else:
            user_output_dir = tempfile.mkdtemp(prefix=f"freecad_job_{user_id}_")
            execution_result["changes"].append(f"Created temporary output directory: {user_output_dir}")

        # Update progress
        mqtt_manager.publish_progress(
            user_id, 10, "running",
            "Setting up output directory...",
            worker_id=worker_id
        )
        monitor_tracker.sample(
            "setup_output",
            status="running",
            progress=10,
            message="Setting up output directory",
        )
        
        # Log execution start with structured information
        print("=" * 70)
        print(f"[{worker_id}] Starting FreeCAD script execution")
        print(f"[{worker_id}] User ID: {user_id}")
        print(f"[{worker_id}] Script: {script_path}")
        print(f"[{worker_id}] Output Directory: {user_output_dir}")
        print("=" * 70)

        # Run script using Python with FreeCAD modules.
        # Wrapped with a .brep-export + coarser-OBJ-mesh fast path (see
        # _build_fast_wrapper_script) that lives entirely in this repo --
        # the generated script itself is executed unmodified inside it.
        run_script_path = _build_fast_wrapper_script(script_path)
        cmd = ["freecadcmd", run_script_path]
        print(f"[{worker_id}] Executing command: {' '.join(cmd)}")
        execution_result["changes"].append(f"Command: {' '.join(cmd)}")

        # Update progress
        mqtt_manager.publish_progress(
            user_id, 20, "running",
            "Executing FreeCAD script...",
            worker_id=worker_id
        )
        monitor_tracker.sample(
            "freecad_starting",
            status="running",
            progress=20,
            message="Starting freecadcmd subprocess",
        )

        # Start heartbeat thread to publish progress updates while FreeCAD is running
        heartbeat_stop = threading.Event()
        heartbeat_progress = {"value": 25}  # Start at 25%
        freecad_pid_holder = {"pid": None}

        def heartbeat_worker():
            """Publish progress updates every 5 seconds while FreeCAD is running"""
            while not heartbeat_stop.is_set():
                heartbeat_progress["value"] = min(heartbeat_progress["value"] + 2, 35)  # Gradually increase to 35%
                mqtt_manager.publish_progress(
                    user_id,
                    heartbeat_progress["value"],
                    "running",
                    f"FreeCAD script is running... (progress: {heartbeat_progress['value']}%)",
                    worker_id=worker_id
                )
                monitor_tracker.sample(
                    "freecad_running",
                    status="running",
                    progress=heartbeat_progress["value"],
                    freecad_pid=freecad_pid_holder["pid"],
                    message="FreeCAD script is running",
                )
                if heartbeat_stop.wait(timeout=5):  # Wait 5 seconds or until stop event
                    break

        heartbeat_thread = threading.Thread(target=heartbeat_worker, daemon=True)
        heartbeat_thread.start()

        try:
            print(f"[WALLCLOCK] Popen(freecadcmd) starting at t={time.perf_counter()-_wall_t0:.2f}s")
            _wc_mark("popen_freecadcmd_starting")
            # Redirect to temp FILES instead of PIPE. With PIPE, the parent
            # must actively drain stdout/stderr while process.communicate()
            # waits -- and freecadcmd is extremely verbose (thousands of
            # tessellation/export progress lines). The heartbeat thread below
            # publishes MQTT progress from a SECOND thread in this same
            # process every 5s; if it holds the GIL at the wrong moment,
            # draining can lag, the OS pipe buffer (~64KB) fills, and
            # freecadcmd blocks on its own stdout write -- directly slowing
            # the CHILD process's real wall-clock execution. Writing to a
            # file removes any possibility of that backpressure entirely.
            _stdout_path = os.path.join(user_output_dir, f"_freecadcmd_stdout_{user_id}.log")
            _stderr_path = os.path.join(user_output_dir, f"_freecadcmd_stderr_{user_id}.log")
            _stdout_f = open(_stdout_path, "w", encoding="utf-8", errors="replace")
            _stderr_f = open(_stderr_path, "w", encoding="utf-8", errors="replace")

            def _read_and_cleanup_captured_output():
                _stdout_f.close()
                _stderr_f.close()
                try:
                    with open(_stdout_path, "r", encoding="utf-8", errors="replace") as f:
                        _out = f.read()
                except Exception:
                    _out = ""
                try:
                    with open(_stderr_path, "r", encoding="utf-8", errors="replace") as f:
                        _err = f.read()
                except Exception:
                    _err = ""
                for _p in (_stdout_path, _stderr_path):
                    try:
                        os.remove(_p)
                    except Exception:
                        pass
                return _out, _err

            # Use Popen to allow monitoring, but still wait for completion
            process = subprocess.Popen(
                cmd,
                stdout=_stdout_f,
                stderr=_stderr_f,
                text=True,
                cwd=user_output_dir,
                env={**os.environ, "PYTHONPATH": "/app:/usr/lib/freecad/lib:/usr/lib/python3/dist-packages", "PYTHONUNBUFFERED": "1"}
            )
            freecad_pid_holder["pid"] = process.pid
            monitor_tracker.sample(
                "freecad_running",
                status="running",
                progress=25,
                freecad_pid=process.pid,
                message="freecadcmd subprocess started",
            )

            # Wait for process with timeout (increased for heavy files).
            # Diagnostic run at 600s showed real (non-isolated) execution
            # can genuinely take ~470-500s on a 4700-hole perforated sheet --
            # not just marginally over the old 300s edge -- so 300s was
            # cutting off real jobs, not just catching hangs. Set to 900s
            # (15 min) as a safety ceiling well above observed real-world
            # time, not as a target.
            try:
                process.wait(timeout=900)
                returncode = process.returncode
                stdout, stderr = _read_and_cleanup_captured_output()
                print(f"[WALLCLOCK] freecadcmd process.wait() returned at t={time.perf_counter()-_wall_t0:.2f}s")
                _wc_mark("freecadcmd_wait_returned")
                _wc_events["freecadcmd_stdout_tail"] = stdout[-2000:] if stdout else ""
                _wc_dump()
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                stdout, stderr = _read_and_cleanup_captured_output()
                returncode = -1
                error_msg = "FreeCAD command timed out after 15 minutes"
                heartbeat_stop.set()
                mqtt_manager.publish_final_status(
                    user_id=user_id,
                    status="failed",
                    message=error_msg,
                    details=None,
                    worker_id=worker_id
                )
                monitor_tracker.finish(
                    status="failed",
                    message=error_msg,
                    progress=0,
                    freecad_pid=freecad_pid_holder["pid"],
                    details={"timeout": True, "timeout_seconds": 300},
                )
                return {
                    "status": "failed",
                    "error": error_msg
                }

            result = type('obj', (object,), {
                'returncode': returncode,
                'stdout': stdout,
                'stderr': stderr
            })()

        finally:
            # Stop heartbeat
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)

        print(f"[{worker_id}] FreeCAD command exit code: {result.returncode}")
        print(f"[{worker_id}] FreeCAD stdout: {result.stdout}")
        if result.stderr:
            print(f"[{worker_id}] FreeCAD stderr: {result.stderr}")

        if result.returncode != 0:
            # Extract specific FreeCAD exception from output
            specific_exception = _extract_freecad_exception(result.stdout or "", result.stderr or "")

            if specific_exception:
                error_msg = f"FreeCADCmd execution failed: {specific_exception}"
            else:
                error_msg = f"FreeCAD command failed with exit code {result.returncode}"

            # Include helpful diagnostics
            stdout_tail = (result.stdout or "").splitlines()[-50:]
            stderr_tail = (result.stderr or "").splitlines()[-50:]
            error_hint = _extract_error_hint("\n".join(stdout_tail), "\n".join(stderr_tail))
            details = {
                "error_hint": error_hint,
                "specific_exception": specific_exception,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "returncode": result.returncode,
            }
            
            # Record in execution result
            execution_result["errors"].append(error_msg)
            if specific_exception:
                execution_result["errors"].append(f"Specific exception: {specific_exception}")
            if error_hint:
                execution_result["warnings"].append(f"Error hint: {error_hint}")

            print("=" * 70)
            print(f"[{worker_id}] ❌ EXECUTION FAILED")
            print(f"[{worker_id}] Error: {error_msg}")
            if specific_exception:
                print(f"[{worker_id}] Exception: {specific_exception}")
            if error_hint:
                print(f"[{worker_id}] Hint: {error_hint}")
            print("=" * 70)

            mqtt_manager.publish_final_status(
                user_id=user_id,
                status="failed",
                message=error_msg,
                details=details,
                worker_id=worker_id
            )
            monitor_tracker.finish(
                status="failed",
                message=error_msg,
                progress=0,
                freecad_pid=freecad_pid_holder["pid"],
                details=details,
            )
            return {
                "status": "failed",
                "error": error_msg,
                "details": details,
                "execution_result": execution_result
            }

        # Validation gate: ensure both STEP and OBJ were generated and valid before proceeding
        mqtt_manager.publish_progress(
            user_id, 40, "running",
            "Validating generated CAD outputs (STEP and OBJ)...",
            worker_id=worker_id
        )
        
        print(f"[{worker_id}] Validating generated files...")
        execution_result["changes"].append("Starting file validation")

        # Discover candidate STEP/OBJ files first in the job output directory (most reliable)
        candidate_paths: List[str] = []
        for root, dirs, files in os.walk(user_output_dir):
            for file in files:
                if file.endswith(('.step', '.obj')):
                    candidate_paths.append(os.path.join(root, file))

        # Also look in legacy output directory as a fallback (some scripts may write there directly)
        legacy_dir = "/app/cad_outputs_generated"
        if os.path.exists(legacy_dir):
            try:
                for root, dirs, files in os.walk(legacy_dir):
                    for file in files:
                        if file.endswith(('.step', '.obj')):
                            candidate_paths.append(os.path.join(root, file))
            except Exception as e:
                print(f"[{worker_id}] Warning: error scanning legacy dir {legacy_dir}: {e}")

        # Validate candidates
        validation = _validate_generated_files(candidate_paths)
        valid_step = [f for f in validation["files"] if f["type"] == "step"]
        valid_obj = [f for f in validation["files"] if f["type"] == "obj"]
        
        # Record validation results
        execution_result["validation_passed"] = validation["valid"]
        if validation["errors"]:
            execution_result["warnings"].extend(validation["errors"])
        execution_result["changes"].append(f"Found {len(valid_step)} STEP files, {len(valid_obj)} OBJ files")
        
        print(f"[{worker_id}] Validation results:")
        print(f"[{worker_id}]   - STEP files: {len(valid_step)}")
        print(f"[{worker_id}]   - OBJ files: {len(valid_obj)}")
        if validation["errors"]:
            print(f"[{worker_id}]   - Validation errors: {len(validation['errors'])}")
            for error in validation["errors"]:
                print(f"[{worker_id}]     ⚠ {error}")

        if len(valid_step) == 0:
            # Build diagnostics
            # OBJ is intentionally NOT required here: nothing downstream (PDF gen,
            # JSON gen/enrichment, the active json_viewer.js 3D viewer) reads the
            # .obj file -- it only fed a now-dormant legacy viewer. STEP is the
            # only hard requirement for the rest of the pipeline to proceed.
            stdout_tail = (result.stdout or "").splitlines()[-50:]
            stderr_tail = (result.stderr or "").splitlines()[-50:]
            error_hint = _extract_error_hint("\n".join(stdout_tail), "\n".join(stderr_tail))

            missing_types = ["STEP"]
            error_msg = f"CAD generation failed: missing required outputs ({' and '.join(missing_types)})."
            if error_hint:
                error_msg += f" FreeCAD error: {error_hint}"

            details = {
                "error_hint": error_hint,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "validation_errors": validation.get("errors", []),
                "validated_files": [f["filename"] for f in validation.get("files", [])],
            }

            print("=" * 70)
            print(f"[{worker_id}] ❌ VALIDATION FAILED")
            print(f"[{worker_id}] Error: {error_msg}")
            print(f"[{worker_id}] Missing types: {', '.join(missing_types)}")
            print("=" * 70)
            
            execution_result["errors"].append(error_msg)
            execution_result["validation_passed"] = False
            
            mqtt_manager.publish_final_status(
                user_id=user_id,
                status="failed",
                message=error_msg,
                details=details,
                worker_id=worker_id
            )
            monitor_tracker.finish(
                status="failed",
                message=error_msg,
                progress=0,
                details=details,
            )
            return {
                "status": "failed",
                "error": error_msg,
                "details": details,
                "execution_result": execution_result
            }

        # Find generated files
        generated_files = []

        # Update progress
        mqtt_manager.publish_progress(
            user_id, 50, "running",
            "Searching for generated files...",
            worker_id=worker_id
        )

        # Determine final output directory (use provided output_dir or STORAGE_PATH)
        final_output_dir = output_dir if output_dir else STORAGE_PATH
        os.makedirs(final_output_dir, exist_ok=True)
        
        # Find files in user output directory
        for root, dirs, files in os.walk(user_output_dir):
            for file in files:
                if file.endswith(('.step', '.obj')):
                    file_path = os.path.join(root, file)
                    # Use standardized naming: {user_id}.step, {user_id}.obj
                    file_ext = os.path.splitext(file)[1]
                    new_filename = f"{user_id}{file_ext}"
                    new_path = os.path.join(final_output_dir, new_filename)
                    
                    # Normalize paths for comparison (resolve symlinks and absolute paths)
                    file_path_abs = os.path.abspath(os.path.realpath(file_path))
                    new_path_abs = os.path.abspath(os.path.realpath(new_path))
                    
                    # Check if file is already in the correct location with correct name
                    if file_path_abs == new_path_abs:
                        # File is already in the right place with the right name - no copy needed
                        execution_result["changes"].append(f"File already in place: {new_filename} (skipped copy)")
                        print(f"[{worker_id}] File already correctly placed: {new_filename}")
                    elif file == new_filename and os.path.dirname(file_path_abs) == final_output_dir:
                        # File has correct name and is in correct directory - no copy needed
                        execution_result["changes"].append(f"File already correctly named: {new_filename} (skipped copy)")
                        print(f"[{worker_id}] File already correctly named: {new_filename}")
                    else:
                        # File needs to be copied/moved
                        try:
                            shutil.copy2(file_path, new_path)
                            execution_result["changes"].append(f"Copied {file} → {new_filename}")
                            print(f"[{worker_id}] Copied: {file} → {new_filename}")
                        except shutil.SameFileError:
                            # Handle edge case where paths are same but comparison missed it
                            execution_result["warnings"].append(f"SameFileError avoided for {new_filename} - file already in place")
                            print(f"[{worker_id}] Warning: SameFileError avoided for {new_filename} - file already in place")

                    file_type = "step" if file.endswith('.step') else "obj"
                    generated_files.append({
                        "type": file_type,
                        "path": new_path,
                        "filename": new_filename
                    })

        # If no files found in job directory, search in cad_outputs_generated
        if not generated_files:
            mqtt_manager.publish_progress(
                user_id, 60, "running",
                "Searching in cad_outputs_generated directory...",
                worker_id=worker_id
            )
            cad_output_dir = "/app/cad_outputs_generated"
            if os.path.exists(cad_output_dir):
                for root, dirs, files in os.walk(cad_output_dir):
                    for file in files:
                        if file.endswith(('.step', '.obj')):
                            file_path = os.path.join(root, file)
                            # Use standardized naming: {user_id}.step, {user_id}.obj
                            file_ext = os.path.splitext(file)[1]
                            new_filename = f"{user_id}{file_ext}"
                            new_path = os.path.join(final_output_dir, new_filename)
                            
                            # Normalize paths for comparison
                            file_path_abs = os.path.abspath(os.path.realpath(file_path))
                            new_path_abs = os.path.abspath(os.path.realpath(new_path))
                            
                            # Check if copy is needed (legacy dir files always need to be copied)
                            if file_path_abs != new_path_abs:
                                try:
                                    shutil.copy2(file_path, new_path)
                                    execution_result["changes"].append(f"Copied from legacy dir: {file} → {new_filename}")
                                    print(f"[{worker_id}] Copied from legacy dir: {file} → {new_filename}")
                                    
                                    # Cleanup original file after successful copy
                                    try:
                                        os.remove(file_path)
                                    except:
                                        pass
                                except shutil.SameFileError:
                                    execution_result["warnings"].append(f"SameFileError avoided for {new_filename} from legacy dir")
                                    print(f"[{worker_id}] Warning: SameFileError avoided for {new_filename} from legacy dir")
                            else:
                                execution_result["warnings"].append(f"File from legacy dir already in place: {new_filename}")
                                print(f"[{worker_id}] File from legacy dir already in place: {new_filename}")

                            file_type = "step" if file.endswith('.step') else "obj"
                            generated_files.append({
                                "type": file_type,
                                "path": new_path,
                                "filename": new_filename
                            })

        # Cleanup temp directory only if it was created by us (not the provided output_dir)
        if not output_dir:
            try:
                shutil.rmtree(user_output_dir, ignore_errors=True)
                execution_result["changes"].append(f"Cleaned up temporary directory: {user_output_dir}")
            except:
                pass

        if not generated_files:
            # Extract diagnostics from the FreeCAD run output FIRST
            stdout_tail = (result.stdout or "").splitlines()[-50:]
            stderr_tail = (result.stderr or "").splitlines()[-50:]
            error_hint = _extract_error_hint("\n".join(stdout_tail), "\n".join(stderr_tail))
            
            error_msg = "No STEP or OBJ files were generated"
            if error_hint:
                error_msg += f". FreeCAD error: {error_hint}"
            details = {
                "error_hint": error_hint,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "searched_directories": [user_output_dir, "/app/cad_outputs_generated"]
            }
            
            execution_result["errors"].append(error_msg)
            if error_hint:
                execution_result["warnings"].append(f"Error hint: {error_hint}")
            
            print("=" * 70)
            print(f"[{worker_id}] ❌ NO FILES GENERATED")
            print(f"[{worker_id}] Error: {error_msg}")
            print(f"[{worker_id}] Searched directories:")
            print(f"[{worker_id}]   - {user_output_dir}")
            print(f"[{worker_id}]   - /app/cad_outputs_generated")
            if error_hint:
                print(f"[{worker_id}] Hint: {error_hint}")
            print("=" * 70)
            
            mqtt_manager.publish_final_status(
                user_id=user_id,
                status="failed",
                message=error_msg,
                details=details,
                worker_id=worker_id
            )
            monitor_tracker.finish(
                status="failed",
                message=error_msg,
                progress=0,
                details=details,
            )
            return {
                "status": "failed",
                "error": error_msg,
                "details": details,
                "execution_result": execution_result
            }

        print(f"[WALLCLOCK] File discovery/validation done at t={time.perf_counter()-_wall_t0:.2f}s")
        _wc_mark("file_discovery_done")
        _wc_dump()
        # Update progress
        mqtt_manager.publish_progress(
            user_id, 70, "running",
            f"Found {len(generated_files)} files, generating PDF and JSON...",
            worker_id=worker_id
        )
        monitor_tracker.sample(
            "post_processing",
            status="running",
            progress=70,
            message=f"Found {len(generated_files)} CAD files, generating PDF and JSON",
        )

        # Track file generation status for comprehensive reporting
        file_generation_status = {
            "script": {"expected": 1, "generated": 1, "files": [script_path]},  # Script already exists
            "cad": {"expected": 0, "generated": 0, "files": [], "failures": []},
            "json": {"expected": 0, "generated": 0, "files": [], "failures": []},
            "pdf": {"expected": 0, "generated": 0, "files": [], "failures": []}
        }

        # Count expected files based on STEP files found
        step_file_count = sum(1 for f in generated_files if f["type"] == "step")
        file_generation_status["cad"]["expected"] = len(generated_files)
        file_generation_status["cad"]["generated"] = len(generated_files)
        file_generation_status["cad"]["files"] = [f["filename"] for f in generated_files]
        file_generation_status["json"]["expected"] = step_file_count
        file_generation_status["pdf"]["expected"] = step_file_count

        # Generate PDF and JSON from STEP files (if any)
        pdf_files = []
        json_files = []
        for i, file_info in enumerate(generated_files):
            if file_info["type"] == "step":
                # Update progress for each file
                progress = 70 + int((i + 1) * 20 / len(generated_files))
                mqtt_manager.publish_progress(
                    user_id, progress, "running",
                    f"Processing file {i+1}/{len(generated_files)}: {file_info['filename']}",
                    worker_id=worker_id
                )
                monitor_tracker.sample(
                    "generating_pdf_json",
                    status="running",
                    progress=progress,
                    message=f"Processing file {i+1}/{len(generated_files)}: {file_info['filename']}",
                )

                # Check for metadata file in input directory (needed before
                # launching the JSON job below)
                metadata_path = None
                if user_id and output_dir:
                    # Metadata should be in storage/{user_id}/input/metadata.json
                    user_storage_dir = os.path.dirname(output_dir)  # Get parent of output dir
                    potential_metadata = os.path.join(user_storage_dir, "input", "metadata.json")
                    if os.path.exists(potential_metadata):
                        metadata_path = potential_metadata
                        print(f"[{worker_id}] Found metadata file: {metadata_path}")

                # PDF and JSON generation are independent of each other --
                # both only need the STEP file on disk. Run them concurrently
                # instead of sequentially so wall time is roughly
                # max(pdf_time, json_time) instead of their sum.
                print(f"[{worker_id}] Generating PDF and JSON concurrently from STEP file: {file_info['path']}")
                print(f"[WALLCLOCK] PDF+JSON block starting at t={time.perf_counter()-_wall_t0:.2f}s")
                _wc_mark("pdf_json_block_starting")
                _wc_dump()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    pdf_future = pool.submit(generate_pdf_from_step, file_info["path"], user_id)
                    json_future = pool.submit(generate_json_from_step, file_info["path"], user_id, metadata_path)
                    pdf_result = pdf_future.result()
                    json_result = json_future.result()
                print(f"[WALLCLOCK] PDF+JSON block done at t={time.perf_counter()-_wall_t0:.2f}s")
                _wc_mark("pdf_json_block_done")
                _wc_events["pdf_result_status"] = pdf_result.get("status")
                _wc_events["json_result_status"] = json_result.get("status")
                _wc_dump()

                if pdf_result["status"] == "success":
                    pdf_files.append({
                        "type": "pdf",
                        "path": pdf_result["pdf_path"],
                        "filename": pdf_result["filename"]
                    })
                    file_generation_status["pdf"]["generated"] += 1
                    file_generation_status["pdf"]["files"].append(pdf_result["filename"])
                    print(f"[{worker_id}] ✅ PDF generated successfully: {pdf_result['filename']}")
                else:
                    error_msg = pdf_result.get("error", "Unknown error during PDF generation")
                    failure_info = {
                        "step_file": file_info["filename"],
                        "error": error_msg
                    }
                    file_generation_status["pdf"]["failures"].append(failure_info)
                    print(f"[{worker_id}] ⚠️ PDF generation failed for {file_info['filename']}: {error_msg}")

                    # Send MQTT message about PDF failure but continue processing
                    mqtt_manager.publish_status(
                        user_id, "warning",
                        f"Failed to generate PDF file: {error_msg}",
                        json.dumps(failure_info),
                        worker_id=worker_id
                    )

                if json_result["status"] == "success":
                    json_files.append({
                        "type": "json",
                        "path": json_result["json_path"],
                        "filename": json_result["filename"]
                    })
                    file_generation_status["json"]["generated"] += 1
                    file_generation_status["json"]["files"].append(json_result["filename"])
                    print(f"[{worker_id}] ✅ JSON generated successfully: {json_result['filename']}")
                else:
                    error_msg = json_result.get("error", "Unknown error during JSON generation")
                    failure_info = {
                        "step_file": file_info["filename"],
                        "error": error_msg,
                        "specific_exception": json_result.get("specific_exception")
                    }
                    file_generation_status["json"]["failures"].append(failure_info)
                    print(f"[{worker_id}] ⚠️ JSON generation failed for {file_info['filename']}: {error_msg}")

                    # Send MQTT message about JSON failure but continue processing
                    mqtt_manager.publish_status(
                        user_id, "warning",
                        f"Failed to generate JSON file: {error_msg}",
                        json.dumps(failure_info),
                        worker_id=worker_id
                    )

        # Combine all files (STEP, OBJ, PDF, JSON)
        all_files = generated_files + pdf_files + json_files

        # Calculate total expected vs generated files
        total_expected = (file_generation_status["script"]["expected"] +
                         file_generation_status["cad"]["expected"] +
                         file_generation_status["json"]["expected"] +
                         file_generation_status["pdf"]["expected"])
        total_generated = (file_generation_status["script"]["generated"] +
                          file_generation_status["cad"]["generated"] +
                          file_generation_status["json"]["generated"] +
                          file_generation_status["pdf"]["generated"])

        # Check if all expected files were generated
        all_files_generated = (total_generated == total_expected)

        # Build detailed status message
        status_details = {
            "total_expected": total_expected,
            "total_generated": total_generated,
            "file_status": file_generation_status,
            "all_files": [f["filename"] for f in all_files]
        }

        print(f"[WALLCLOCK] execute_freecad_script about to return at t={time.perf_counter()-_wall_t0:.2f}s")
        _wc_mark("about_to_return")
        _wc_dump()
        if all_files_generated:
            # All files generated successfully
            success_msg = f"Job completed successfully! All {total_expected} expected files were generated."
            
            execution_result["success"] = True
            execution_result["validation_passed"] = True
            execution_result["files_generated"] = [f["filename"] for f in all_files]
            execution_result["changes"].append(f"All {total_expected} files generated successfully")
            
            print("=" * 70)
            print(f"[{worker_id}] ✅ EXECUTION SUCCESSFUL")
            print(f"[{worker_id}] Message: {success_msg}")
            print(f"[{worker_id}] Generated files ({len(all_files)}):")
            for f in all_files:
                print(f"[{worker_id}]   ✓ {f['filename']}")
            print("=" * 70)

            mqtt_manager.publish_final_status(
                user_id=user_id,
                status="complete",
                message=success_msg,
                details=status_details,
                worker_id=worker_id
            )
            monitor_tracker.finish(
                status="complete",
                message=success_msg,
                progress=100,
                details=status_details,
            )

            return {
                "status": "success",
                "files": all_files,
                "file_status": file_generation_status,
                "execution_result": execution_result
            }
        else:
            # Partial success - some files failed
            failure_summary = []

            if file_generation_status["json"]["failures"]:
                failure_summary.append(
                    f"JSON files: {file_generation_status['json']['generated']}/{file_generation_status['json']['expected']} generated"
                )
                for failure in file_generation_status["json"]["failures"]:
                    failure_summary.append(f"  - {failure['step_file']}: {failure['error']}")

            if file_generation_status["pdf"]["failures"]:
                failure_summary.append(
                    f"PDF files: {file_generation_status['pdf']['generated']}/{file_generation_status['pdf']['expected']} generated"
                )
                for failure in file_generation_status["pdf"]["failures"]:
                    failure_summary.append(f"  - {failure['step_file']}: {failure['error']}")

            partial_msg = (f"Job completed with partial success. Generated {total_generated}/{total_expected} files. "
                          f"Failures: {', '.join(failure_summary)}")

            execution_result["success"] = False  # Partial success is not full success
            execution_result["files_generated"] = [f["filename"] for f in all_files]
            execution_result["warnings"].append(partial_msg)
            execution_result["changes"].append(f"Generated {total_generated}/{total_expected} files")
            
            print("=" * 70)
            print(f"[{worker_id}] ⚠️ PARTIAL SUCCESS")
            print(f"[{worker_id}] Message: {partial_msg}")
            print(f"[{worker_id}] Successfully generated ({len(all_files)}):")
            for f in all_files:
                print(f"[{worker_id}]   ✓ {f['filename']}")
            if failure_summary:
                print(f"[{worker_id}] Failures:")
                for failure in failure_summary:
                    print(f"[{worker_id}]   ✗ {failure}")
            print("=" * 70)

            mqtt_manager.publish_final_status(
                user_id=user_id,
                status="partial_success",
                message=partial_msg,
                details=status_details,
                worker_id=worker_id
            )
            monitor_tracker.finish(
                status="partial_success",
                message=partial_msg,
                progress=90,
                details=status_details,
            )

            return {
                "status": "partial_success",
                "files": all_files,
                "file_status": file_generation_status,
                "message": partial_msg,
                "execution_result": execution_result
            }

    except subprocess.TimeoutExpired:
        error_msg = "FreeCAD command timed out after 5 minutes"
        execution_result["errors"].append(error_msg)
        execution_result["warnings"].append("Process exceeded maximum execution time")
        
        print("=" * 70)
        print(f"[{worker_id}] ❌ TIMEOUT ERROR")
        print(f"[{worker_id}] Error: {error_msg}")
        print("=" * 70)
        
        mqtt_manager.publish_final_status(
            user_id=user_id,
            status="failed",
            message=error_msg,
            details={"timeout": True, "timeout_seconds": 300},
            worker_id=worker_id
        )
        monitor_tracker.finish(
            status="failed",
            message=error_msg,
            progress=0,
            details={"timeout": True, "timeout_seconds": 300},
        )
        return {
            "status": "failed",
            "error": error_msg,
            "execution_result": execution_result
        }
    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        execution_result["errors"].append(error_msg)
        
        import traceback
        tb = traceback.format_exc()
        execution_result["warnings"].append(f"Traceback: {tb[:500]}")  # Limit traceback length
        
        print("=" * 70)
        print(f"[{worker_id}] ❌ UNEXPECTED ERROR")
        print(f"[{worker_id}] Error: {error_msg}")
        print(f"[{worker_id}] Traceback:")
        print(tb)
        print("=" * 70)
        
        mqtt_manager.publish_final_status(
            user_id=user_id,
            status="failed",
            message=error_msg,
            details={"exception_type": type(e).__name__, "traceback": tb[:1000]},
            worker_id=worker_id
        )
        monitor_tracker.finish(
            status="failed",
            message=error_msg,
            progress=0,
            details={"exception_type": type(e).__name__, "traceback": tb[:1000]},
        )
        return {
            "status": "failed",
            "error": error_msg,
            "execution_result": execution_result
        }
    finally:
        # Keep uploaded script file for diagnostics and re-download if needed
        # Do not delete script_path here so that admins can fetch the .py by user_id later
        try:
            if os.path.exists(script_path):
                print(f"[{worker_id}] Keeping script file for diagnostics: {script_path}")
        except Exception as e:
            print(f"[{worker_id}] Warning: Could not access script file during finalization: {e}")


# Legacy function for backward compatibility (if needed)
def generate_freecad_job(model_type: str, parameters: dict, job_id: str) -> Dict[str, Any]:
    """
    Legacy function - redirects to script execution
    """
    return {
        "status": "failed",
        "error": "This endpoint is deprecated. Please use file upload instead."
    }


def _extract_freecad_exception(stdout: str, stderr: str) -> str:
    """
    Extract specific FreeCAD exception message from output.
    Looks for pattern: "Exception while processing file: ... [error details]"
    """
    import re

    # Combine stdout and stderr for searching
    combined_output = f"{stdout}\n{stderr}"

    # Pattern 1: "Exception while processing file: ... [error message]"
    exception_pattern = r"Exception while processing file:.*?\[(.*?)\]"
    match = re.search(exception_pattern, combined_output, re.IGNORECASE | re.DOTALL)
    if match:
        error_msg = match.group(1).strip()
        # Clean up the error message
        error_msg = error_msg.replace("'", "").replace('"', '')
        return error_msg

    # Pattern 2: Look for Python traceback with exception type and message
    traceback_pattern = r"(\w+Error|Exception):\s*(.+?)(?:\n|$)"
    matches = re.findall(traceback_pattern, combined_output)
    if matches:
        # Return the last exception found (usually most relevant)
        exception_type, exception_msg = matches[-1]
        return f"{exception_type}: {exception_msg.strip()}"

    # Pattern 3: Look for "Error:" or "ERROR:" messages
    error_pattern = r"(?:Error|ERROR):\s*(.+?)(?:\n|$)"
    match = re.search(error_pattern, combined_output)
    if match:
        return match.group(1).strip()

    return None


def _extract_error_hint(stdout_text: str, stderr_text: str) -> str:
    """Try to infer a helpful error hint from FreeCAD outputs."""
    text = f"{stdout_text}\n{stderr_text}".lower()

    # First try to extract specific FreeCAD exception
    specific_exception = _extract_freecad_exception(stdout_text, stderr_text)
    if specific_exception:
        return f"FreeCAD Exception: {specific_exception}"

    if "modulenotfounderror" in text or "no module named" in text:
        return "ImportError: missing module. Verify FreeCadUtil and workbench imports, or sys.path."
    if "attributeerror" in text and "maketub" in text:
        return "AttributeError: Part.makeTub missing. Ensure FreeCadUtil monkey patch is loaded."
    if "permission denied" in text:
        return "Permission issue writing outputs. Verify container paths and permissions."
    if "traceback" in text and "export" in text and ".step" in text:
        return "STEP export failed. Ensure shapes are valid solids and export path exists."
    if "wkhtmltopdf" in text and "not found" in text:
        return "wkhtmltopdf missing. PDF generation may fail; check worker image deps."
    if "precision" in text and "approximation" in text:
        return "FreeCAD Precision API mismatch. Ensure FreeCAD 0.21+/1.0 compatibility fixes are applied."
    return "See stdout_tail/stderr_tail for details."
