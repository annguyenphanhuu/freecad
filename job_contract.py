"""
The job-outcome contract between this FreeCAD server and tolery-api-ai.

A job ends in exactly one of three states, and the server — not the client —
decides which, and why. The server is the only side that saw the failure
happen, so it names the failure with a code; the client reads that code and
turns it into a message for the user. The client never guesses from prose.

Outcome envelope (identical in the final MQTT message, in GET /freecad/status
and in GET /freecad/result):

    {
      "user_id":   "session_abc",
      "status":    "complete" | "partial_success" | "failed",
      "progress":  100 | 90 | 0,
      "code":      "104.1" | null,          # null only when status == complete
      "message":   "one human-readable line",
      "error":     {                        # present iff code is set
          "code": "104.1",
          "message": "...",
          "specific_exception": "...",      # optional
          "error_hint": "...",              # optional
          "stdout_tail": [...],             # optional
          "stderr_tail": [...],             # optional
          "returncode": -1                  # optional
      },
      "details":   { ... },                 # optional, success/diagnostic info
      "worker_id": "worker-1",
      "final":     true,
      "timestamp": "2026-08-17T10:00:00+00:00"
    }

Two rules keep this honest:

1. `code` and `error` are real JSON values, never a JSON string stuffed into a
   field. Anything that has to be re-parsed by the reader gets lost the first
   time someone wraps it.
2. The envelope is also written to disk as OUTCOME_FILENAME in the job's output
   directory. MQTT can be down, the broker can drop a retained message and the
   API process can restart — the file is what lets /status and /result still
   tell the truth afterwards.

The code table lives in tolery-api-ai/src/core/error_codes.py, which holds the
user-facing message for each code. Only the codes below are ever produced here;
the 102.x family is about reaching this server, which only the client can
observe, and 105.x is the client's own side.
"""

import json
import os
from typing import Any, Dict, List, Optional

# ── Statuses ───────────────────────────────────────────────────────────────
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_PARTIAL_SUCCESS = "partial_success"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = (STATUS_COMPLETE, STATUS_PARTIAL_SUCCESS, STATUS_FAILED)

#: Progress percentage implied by each terminal status. A failed job reports 0
#: rather than its last running value: the number answers "how much of this job
#: is usable", not "how far did it get".
PROGRESS_BY_STATUS = {
    STATUS_COMPLETE: 100,
    STATUS_PARTIAL_SUCCESS: 90,
    STATUS_FAILED: 0,
}

# ── Codes this server can emit ─────────────────────────────────────────────
#: freecadcmd was killed for exceeding EXEC_TIMEOUT_SECONDS.
CODE_JOB_TIMEOUT = "101.1"
#: freecadcmd exited non-zero: the generated script raised.
CODE_SCRIPT_FAILED = "104.1"
#: The script failed on an encoding error (charmap / codec / unicode).
CODE_SCRIPT_ENCODING = "104.2"
#: The script finished but a required CAD output is missing.
CODE_MISSING_OUTPUT = "104.3"
#: The worker itself failed (bad input, crash) — not the user's model.
CODE_SERVER_INTERNAL = "104.5"
#: The model was built but an optional output (PDF) is missing.
CODE_PARTIAL_EXPORT = "104.6"

#: One line per code, for logs and for anyone reading a raw envelope. The
#: user-facing wording lives in the client's table, not here.
CODE_MEANINGS = {
    CODE_JOB_TIMEOUT: "FreeCAD execution exceeded the time limit and was stopped",
    CODE_SCRIPT_FAILED: "The generated script raised while running in FreeCAD",
    CODE_SCRIPT_ENCODING: "The generated script hit an encoding error",
    CODE_MISSING_OUTPUT: "Execution finished without producing the required CAD output",
    CODE_SERVER_INTERNAL: "The FreeCAD server failed while handling the job",
    CODE_PARTIAL_EXPORT: "The model was produced but an optional export is missing",
}

#: Hard ceiling for one freecadcmd run. Sized from measurement, not guesswork: a
#: 4700-hole perforated sheet genuinely takes ~470-500s, so this is a safety
#: ceiling well above real work rather than a target. Anything that hits it is
#: reported as CODE_JOB_TIMEOUT, and this constant is the single place the value
#: is defined so the number in the message can never drift from the real one.
EXEC_TIMEOUT_SECONDS = 900

#: Written into the job's output directory. Not in the download whitelist, so it
#: never leaks to end users.
OUTCOME_FILENAME = "_job_outcome.json"

#: stdout/stderr lines kept for diagnosis of a failure.
TAIL_LINES = 50

#: Substrings that identify an encoding failure in FreeCAD's output.
_ENCODING_SIGNATURES = ("charmap", "unicodeencodeerror", "unicodedecodeerror", "codec can't")


def classify_script_failure(stdout: str = "", stderr: str = "", message: str = "") -> str:
    """Return the code for a script that ran and failed.

    Only one distinction is worth making from the output itself: an encoding
    failure is a defect in what was generated and tells the user something
    actionable (drop the accents), while everything else is "the script raised".
    Timeouts and missing outputs are NOT detected here — the worker knows those
    from its own control flow and passes the code directly.
    """
    haystack = "{} {} {}".format(stdout or "", stderr or "", message or "").lower()
    if any(signature in haystack for signature in _ENCODING_SIGNATURES):
        return CODE_SCRIPT_ENCODING
    return CODE_SCRIPT_FAILED


def build_error(
    code: str,
    message: str,
    specific_exception: Optional[str] = None,
    error_hint: Optional[str] = None,
    stdout_tail: Optional[List[str]] = None,
    stderr_tail: Optional[List[str]] = None,
    **extra: Any
) -> Dict[str, Any]:
    """Build the `error` object of the envelope.

    Optional fields are omitted rather than sent as null, so a reader can tell
    "the server had no hint" from "the server sent a hint that was empty".
    """
    error = {"code": code, "message": message, "meaning": CODE_MEANINGS.get(code, "")}
    if specific_exception:
        error["specific_exception"] = specific_exception
    if error_hint:
        error["error_hint"] = error_hint
    if stdout_tail:
        error["stdout_tail"] = stdout_tail
    if stderr_tail:
        error["stderr_tail"] = stderr_tail
    for key, value in extra.items():
        if value is not None:
            error[key] = value
    return error


def build_outcome(
    user_id: str,
    status: str,
    message: str,
    code: Optional[str] = None,
    error: Optional[Dict[str, Any]] = None,
    details: Optional[Dict[str, Any]] = None,
    worker_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a terminal outcome envelope.

    `code` is derived from `error` when only the error object is given, so the
    two can never disagree.
    """
    if code is None and error:
        code = error.get("code")

    if status == STATUS_COMPLETE and code:
        raise ValueError("a complete job cannot carry an error code ({})".format(code))
    if status == STATUS_FAILED and not code:
        raise ValueError("a failed job must carry an error code")

    from datetime import datetime, timezone

    outcome = {
        "user_id": user_id,
        "status": status,
        "progress": PROGRESS_BY_STATUS.get(status, 0),
        "code": code,
        "message": message,
        "final": True,
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
    }
    if error:
        outcome["error"] = error
    if details:
        outcome["details"] = details
    if worker_id:
        outcome["worker_id"] = worker_id
    return outcome


def outcome_path(output_dir: str) -> str:
    return os.path.join(output_dir, OUTCOME_FILENAME)


def write_outcome(output_dir: str, outcome: Dict[str, Any]) -> bool:
    """Persist the outcome next to the job's files. Never raises.

    Writing this must not be able to turn a reportable failure into a crash, so
    every error here is swallowed after being logged — the MQTT message is still
    on its way and remains the fast path.
    """
    if not output_dir:
        return False
    try:
        os.makedirs(output_dir, exist_ok=True)
        with open(outcome_path(output_dir), "w", encoding="utf-8") as handle:
            json.dump(outcome, handle, indent=2)
        return True
    except Exception as exc:  # pragma: no cover - diagnostics only
        print("[CONTRACT] Could not write {}: {}".format(OUTCOME_FILENAME, exc))
        return False


def read_outcome(output_dir: str) -> Optional[Dict[str, Any]]:
    """Read a persisted outcome, or None when there is none / it is unreadable."""
    if not output_dir:
        return None
    path = outcome_path(output_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            outcome = json.load(handle)
        return outcome if isinstance(outcome, dict) else None
    except Exception as exc:  # pragma: no cover - diagnostics only
        print("[CONTRACT] Could not read {}: {}".format(OUTCOME_FILENAME, exc))
        return None
