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
                            "error": None,
                            "details": None
                        }

                    # Update progress data
                    if "progress" in message_data:
                        self.progress_data[user_id]["progress"] = message_data["progress"]
                    if "status" in message_data:
                        self.progress_data[user_id]["status"] = message_data["status"]
                    if "message" in message_data:
                        self.progress_data[user_id]["message"] = message_data["message"]
                    if "error" in message_data:
                        self.progress_data[user_id]["error"] = message_data["error"]
                    if "details" in message_data:
                        self.progress_data[user_id]["details"] = message_data["details"]
                    if "worker_id" in message_data:
                        self.progress_data[user_id]["worker_id"] = message_data["worker_id"]
                    if "timestamp" in message_data:
                        self.progress_data[user_id]["timestamp"] = message_data["timestamp"]

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
    
    def publish_progress(
        self,
        user_id: str,
        progress: int,
        status: str,
        message: str = "",
        error: str = None,
        worker_id: str = None,
    ) -> None:
        """Publish progress update to MQTT"""
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
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
            
            if error:
                data["error"] = error
            
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
        error: str = None,
        details: str = None,
        worker_id: str = None,
    ) -> None:
        """
        Publish status update to MQTT

        Args:
            user_id: User identifier
            status: Status string (e.g., "complete", "failed", "partial_success")
            message: Human-readable status message
            error: Error information (only for failed status)
            details: Success details (only for complete/partial_success status)
            worker_id: Worker identifier
        """
        if not self.connected:
            print("MQTT not connected, cannot publish status")
            return

        try:
            topic = f"freecad/status/{user_id}"
            data = {
                "user_id": user_id,
                "status": status,
                "message": message,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

            if error:
                data["error"] = error

            if details:
                data["details"] = details

            if worker_id:
                data["worker_id"] = worker_id

            self.client.publish(topic, json.dumps(data))
            worker_info = f" [Worker: {worker_id}]" if worker_id else ""
            print(f"Published status for user {user_id}{worker_info}: {status}")

        except Exception as e:
            print(f"Error publishing status: {e}")

    def publish_final_status(
        self,
        user_id: str,
        status: str,
        message: str,
        details: Dict[str, Any] = None,
        worker_id: str = None
    ) -> None:
        """
        Publish final job status with both progress and status in ONE message.
        This is the ONLY method that should be called when a job finishes.

        Status mapping:
        - "complete": progress=100, all files generated successfully
        - "partial_success": progress=90, some files failed but job completed
        - "failed": progress=0, job failed completely

        Args:
            user_id: User identifier
            status: Final status ("complete", "partial_success", "failed")
            message: Human-readable status message
            details: Additional details (files, errors, etc.)
            worker_id: Worker identifier
        """
        if not self.connected:
            print("MQTT not connected, cannot publish final status")
            return

        try:
            # Determine progress based on status
            progress_map = {
                "complete": 100,
                "partial_success": 90,
                "failed": 0
            }
            progress = progress_map.get(status, 0)

            # Create consistent data structure
            timestamp = datetime.now(timezone.utc).isoformat()

            data = {
                "user_id": user_id,
                "status": status,
                "progress": progress,
                "message": message,
                "timestamp": timestamp,
                "final": True  # Indicates this is the final message
            }

            if details:
                data["details"] = json.dumps(details) if isinstance(details, dict) else details

            if worker_id:
                data["worker_id"] = worker_id

            # Publish to both topics with consistent data
            self.client.publish(f"freecad/status/{user_id}", json.dumps(data))
            self.client.publish(f"freecad/progress/{user_id}", json.dumps(data))

            worker_info = f" [Worker: {worker_id}]" if worker_id else ""
            print(f"Published FINAL status for user {user_id}{worker_info}: {status} ({progress}%)")

        except Exception as e:
            print(f"Error publishing final status: {e}")


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
