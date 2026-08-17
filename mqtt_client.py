"""
MQTT client for FreeCAD API progress updates
Provides real-time progress tracking via MQTT messages
"""

from typing import Dict, Any, Optional
import paho.mqtt.client as mqtt
from datetime import datetime, timezone
import os
import socket
import subprocess
import json
import threading
import time
import config
from job_contract import TERMINAL_STATUSES


def _get_host_ip():
    """Get host machine IP from container (gateway IP)"""
    try:
        # Try to get gateway IP from default route
        result = subprocess.run(
            ['ip', 'route', 'show', 'default'],
            capture_output=True,
            text=True,
            timeout=2
        )
        if result.returncode == 0:
            gateway = result.stdout.strip().split()[2]
            return gateway
    except Exception:
        pass
    
    try:
        # Fallback: try to connect to host.docker.internal (Docker Desktop)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        host_ip = s.getsockname()[0]
        s.close()
        # If we're in a container, get gateway
        result = subprocess.run(
            ['ip', 'route', 'get', '8.8.8.8'],
            capture_output=True,
            text=True,
            timeout=2
        )
        if result.returncode == 0:
            # Extract gateway from route output
            parts = result.stdout.strip().split()
            for i, part in enumerate(parts):
                if part == 'via':
                    return parts[i + 1]
    except Exception:
        pass
    
    # Last resort: try host.docker.internal
    try:
        socket.gethostbyname('host.docker.internal')
        return 'host.docker.internal'
    except Exception:
        pass
    
    return None


class MQTTProgressManager:
    """
    MQTT client to manage progress updates from workers
    """
    
    def __init__(self, broker_url: str = None):
        self.broker_url = broker_url or config.MQTT_BROKER
        broker_host_raw = self.broker_url.replace("mqtt://", "").split(":")[0]
        self.broker_port = int(self.broker_url.split(":")[-1]) if ":" in self.broker_url else 1883
        
        # Auto-detect host IP if localhost is used (for Docker containers)
        if broker_host_raw in ['localhost', '127.0.0.1']:
            host_ip = _get_host_ip()
            if host_ip:
                self.broker_host = host_ip
                print(f"Auto-detected host IP for MQTT: {host_ip}")
            else:
                self.broker_host = broker_host_raw
                print(f"Warning: Could not auto-detect host IP, using {broker_host_raw}")
        else:
            self.broker_host = broker_host_raw
        
        self.client = mqtt.Client()
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        
        # Store progress data
        self.progress_data: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()
        
        # Connection status
        self.connected = False
        self.connection_thread = None
        
    def _on_connect(self, client, userdata, flags, rc):
        """Callback when MQTT connection is established"""
        if rc == 0:
            print(f"MQTT connected to {self.broker_host}:{self.broker_port}")
            self.connected = True
            # Subscribe to progress topics
            client.subscribe("freecad/progress/+")
            client.subscribe("freecad/status/+")
        else:
            print(f"MQTT connection failed with code {rc}")
            self.connected = False
    
    def _on_disconnect(self, client, userdata, rc):
        """Callback when MQTT disconnection occurs"""
        print(f"MQTT disconnected with code {rc}")
        self.connected = False
    
    def _on_message(self, client, userdata, msg):
        """Callback when message is received from MQTT"""
        try:
            topic_parts = msg.topic.split("/")
            if len(topic_parts) >= 3:
                user_id = topic_parts[-1]  # Get user_id from topic

                # Parse message
                message_data = json.loads(msg.payload.decode())

                with self.lock:
                    if user_id not in self.progress_data:
                        self.progress_data[user_id] = {
                            "status": "unknown",
                            "progress": 0,
                            "message": "",
                            "updated_at": None,
                            "code": None,
                            "error": None,
                            "details": None,
                            "final": False,
                        }

                    # Copy every field of the envelope through verbatim. A
                    # running update carries no code/error, and must not erase
                    # the code of a terminal one that arrived first — final
                    # messages go to BOTH topics, so the same user can get a
                    # 'running' straggler afterwards.
                    if message_data.get("final") or not self.progress_data[user_id].get("final"):
                        for key in (
                            "progress", "status", "message", "code", "error",
                            "details", "worker_id", "timestamp", "final",
                        ):
                            if key in message_data:
                                self.progress_data[user_id][key] = message_data[key]

                    self.progress_data[user_id]["updated_at"] = datetime.now(timezone.utc).isoformat()

                print(f"Updated progress for user {user_id}: {message_data}")

        except Exception as e:
            print(f"Error processing MQTT message: {e}")
    
    def start(self):
        """Start MQTT connection"""
        try:
            self.connection_thread = threading.Thread(target=self._connect_loop, daemon=True)
            self.connection_thread.start()
        except Exception as e:
            print(f"Error starting MQTT client: {e}")
    
    def _connect_loop(self):
        """MQTT connection loop"""
        while True:
            try:
                if not self.connected:
                    print(f"Connecting to MQTT broker at {self.broker_host}:{self.broker_port}")
                    self.client.connect(self.broker_host, self.broker_port, 60)
                    self.client.loop_start()
                time.sleep(5)
            except Exception as e:
                print(f"MQTT connection error: {e}")
                time.sleep(5)
    
    def get_progress(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get progress for a specific user"""
        with self.lock:
            return self.progress_data.get(user_id)
    
    def get_all_progress(self) -> Dict[str, Dict[str, Any]]:
        """Get progress for all users"""
        with self.lock:
            return self.progress_data.copy()

    def reset_progress(self, user_id: str) -> None:
        """Forget everything known about a user's previous job.

        user_id doubles as the job key, so a user who submits a second time
        would otherwise inherit the first job's terminal state — and since a
        terminal message is sticky (see _on_message), the new job's updates
        would be ignored and the client would read the old verdict as if it
        belonged to the new job. Called when a job is queued.
        """
        with self.lock:
            self.progress_data.pop(user_id, None)
    
    def publish_progress(
        self,
        user_id: str,
        progress: int,
        status: str,
        message: str = "",
        worker_id: str = None,
    ) -> None:
        """Publish a progress update. Carries no code and no error by design:
        progress says how far the job got, never whether it succeeded."""
        if not self.connected:
            print("MQTT not connected, cannot publish progress")
            return

        try:
            topic = f"freecad/progress/{user_id}"
            data = {
                "user_id": user_id,
                "progress": progress,
                "status": status,
                "message": message,
                "final": False,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

            if worker_id:
                data["worker_id"] = worker_id
            
            self.client.publish(topic, json.dumps(data))
            worker_info = f" [Worker: {worker_id}]" if worker_id else ""
            print(f"Published progress for user {user_id}{worker_info}: {progress}% - {status}")
            
        except Exception as e:
            print(f"Error publishing progress: {e}")
    
    def publish_status(
        self,
        user_id: str,
        status: str,
        message: str = "",
        worker_id: str = None,
    ) -> None:
        """
        Publish a NON-terminal status ("queued", "running").

        A terminal status must go through publish_outcome() instead — it is the
        only thing that also carries a code and gets written to disk. Publishing
        "failed"/"complete"/"partial_success" here would tell the client the job
        is over while leaving it without any way to know why, so it is refused.
        """
        if status in TERMINAL_STATUSES:
            print(
                f"Refusing to publish terminal status '{status}' for user {user_id} "
                f"through publish_status(); use publish_outcome()"
            )
            return

        if not self.connected:
            print("MQTT not connected, cannot publish status")
            return

        try:
            topic = f"freecad/status/{user_id}"
            data = {
                "user_id": user_id,
                "status": status,
                "message": message,
                "final": False,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

            if worker_id:
                data["worker_id"] = worker_id

            self.client.publish(topic, json.dumps(data))
            worker_info = f" [Worker: {worker_id}]" if worker_id else ""
            print(f"Published status for user {user_id}{worker_info}: {status}")

        except Exception as e:
            print(f"Error publishing status: {e}")

    def publish_outcome(self, outcome: Dict[str, Any]) -> bool:
        """
        Publish a terminal outcome envelope. The ONLY way a job announces that
        it has finished, whatever the result.

        The envelope is built by job_contract.build_outcome() and is published
        **verbatim**: `code`, `error` and `details` stay real JSON values. They
        used to be re-encoded with json.dumps() into a string field, which forced
        the reader to parse a string out of a parsed message and lost every
        detail the moment anyone forgot to.

        Returns True if the message went out. A False here is not a lost job:
        the worker also writes the same envelope to disk, and /freecad/status
        and /freecad/result read it from there.
        """
        user_id = outcome.get("user_id")
        if not user_id:
            print("Refusing to publish an outcome with no user_id")
            return False

        if not self.connected:
            # Loud, because from here on the client only learns the outcome
            # through the slower HTTP fallback.
            print(
                f"MQTT NOT CONNECTED - outcome for user {user_id} "
                f"({outcome.get('status')}) not published; it is on disk only"
            )
            return False

        try:
            payload = json.dumps(outcome)
            # Both topics: subscribers listen to progress for the bar and to
            # status for the verdict, and a terminal message is both.
            self.client.publish(f"freecad/status/{user_id}", payload)
            self.client.publish(f"freecad/progress/{user_id}", payload)

            worker_info = f" [Worker: {outcome.get('worker_id')}]" if outcome.get("worker_id") else ""
            code_info = f" code={outcome.get('code')}" if outcome.get("code") else ""
            print(
                f"Published FINAL outcome for user {user_id}{worker_info}: "
                f"{outcome.get('status')} ({outcome.get('progress')}%){code_info}"
            )
            return True

        except Exception as e:
            print(f"Error publishing final outcome: {e}")
            return False


# Global MQTT manager instance
_mqtt_manager = None
_mqtt_lock = threading.Lock()

def get_mqtt_manager() -> MQTTProgressManager:
    """Get or create MQTT manager instance (singleton)"""
    global _mqtt_manager
    
    with _mqtt_lock:
        if _mqtt_manager is None:
            _mqtt_manager = MQTTProgressManager()
            _mqtt_manager.start()
        
        return _mqtt_manager
