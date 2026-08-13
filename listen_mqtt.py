#!/usr/bin/env python3
"""
Script để lắng nghe MQTT messages từ FreeCAD workers
Sử dụng: python listen_mqtt.py [user_id]
- Nếu không có user_id: lắng nghe tất cả users
- Nếu có user_id: chỉ lắng nghe user đó
"""

import json
import sys
import time
import paho.mqtt.client as mqtt
from datetime import datetime
import os
import config

# Màu sắc cho output
class Colors:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

def format_timestamp(timestamp_str):
    """Format timestamp để dễ đọc"""
    try:
        dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        return dt.strftime('%Y-%m-%d %H:%M:%S')
    except:
        return timestamp_str

def on_connect(client, userdata, flags, rc):
    """Callback khi kết nối MQTT"""
    if rc == 0:
        print(f"{Colors.GREEN}✓ MQTT Connected successfully!{Colors.RESET}")
        if userdata.get('user_id'):
            # Subscribe cho user cụ thể
            topic = f"freecad/progress/{userdata['user_id']}"
            client.subscribe(topic)
            print(f"{Colors.CYAN}Listening to: {topic}{Colors.RESET}")
        else:
            # Subscribe tất cả users
            client.subscribe("freecad/progress/+")
            client.subscribe("freecad/status/+")
            print(f"{Colors.CYAN}Listening to all users: freecad/progress/+ and freecad/status/+{Colors.RESET}")
    else:
        print(f"{Colors.RED}✗ MQTT Connection failed with code {rc}{Colors.RESET}")

def on_message(client, userdata, msg):
    """Callback khi nhận được message"""
    try:
        topic_parts = msg.topic.split("/")
        user_id = topic_parts[-1] if len(topic_parts) >= 3 else "unknown"
        
        # Parse JSON message
        message_data = json.loads(msg.payload.decode())
        
        # Hiển thị thông tin
        print(f"\n{Colors.BOLD}{'='*80}{Colors.RESET}")
        print(f"{Colors.BLUE}Topic:{Colors.RESET} {msg.topic}")
        print(f"{Colors.BLUE}User ID:{Colors.RESET} {Colors.BOLD}{user_id}{Colors.RESET}")
        print(f"{Colors.BLUE}Timestamp:{Colors.RESET} {format_timestamp(message_data.get('timestamp', ''))}")
        print(f"{Colors.BLUE}Status:{Colors.RESET} {Colors.BOLD}{message_data.get('status', 'N/A')}{Colors.RESET}")
        print(f"{Colors.BLUE}Progress:{Colors.RESET} {Colors.BOLD}{message_data.get('progress', 0)}%{Colors.RESET}")
        
        if message_data.get('message'):
            print(f"{Colors.BLUE}Message:{Colors.RESET} {message_data['message']}")
        
        if message_data.get('error'):
            print(f"{Colors.RED}Error:{Colors.RESET} {message_data['error']}")
        
        # Hiển thị progress bar
        progress = message_data.get('progress', 0)
        bar_length = 50
        filled = int(bar_length * progress / 100)
        bar = '█' * filled + '░' * (bar_length - filled)
        print(f"{Colors.CYAN}Progress Bar:{Colors.RESET} [{bar}] {progress}%")
        
        # Hiển thị raw JSON (có thể comment nếu không cần)
        print(f"\n{Colors.YELLOW}Raw JSON:{Colors.RESET}")
        print(json.dumps(message_data, indent=2, ensure_ascii=False))
        
    except json.JSONDecodeError as e:
        print(f"{Colors.RED}Error parsing JSON: {e}{Colors.RESET}")
        print(f"Raw message: {msg.payload.decode()}")
    except Exception as e:
        print(f"{Colors.RED}Error processing message: {e}{Colors.RESET}")

def main():
    """Main function"""
    # Parse arguments
    user_id = None
    if len(sys.argv) > 1:
        user_id = sys.argv[1]
        print(f"{Colors.CYAN}Listening for user_id: {user_id}{Colors.RESET}")
    else:
        print(f"{Colors.CYAN}Listening for all users...{Colors.RESET}")
    
    # Get MQTT broker config
    broker_url = os.getenv("MQTT_BROKER", config.MQTT_BROKER)
    broker_host = broker_url.replace("mqtt://", "").split(":")[0]
    broker_port = int(broker_url.split(":")[-1]) if ":" in broker_url else 1883
    
    print(f"{Colors.YELLOW}Connecting to MQTT broker: {broker_host}:{broker_port}{Colors.RESET}")
    
    # Create MQTT client
    client = mqtt.Client(userdata={'user_id': user_id})
    client.on_connect = on_connect
    client.on_message = on_message
    
    try:
        # Connect to broker
        client.connect(broker_host, broker_port, 60)
        
        # Start loop
        print(f"{Colors.GREEN}Starting MQTT listener... (Press Ctrl+C to stop){Colors.RESET}\n")
        client.loop_forever()
        
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Stopping listener...{Colors.RESET}")
        client.disconnect()
    except Exception as e:
        print(f"{Colors.RED}Error: {e}{Colors.RESET}")
        sys.exit(1)

if __name__ == "__main__":
    main()

