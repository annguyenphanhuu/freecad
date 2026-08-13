"""
File Utilities for Robust JSON I/O Operations
==============================================
This module provides utility functions for safe file operations with:
- Retry logic for handling file locks and race conditions
- Atomic writes to prevent data corruption
- File size monitoring

Author: AI Assistant
Date: 2026-01-27
"""

import json
import os
import time
import tempfile
import shutil
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
import logging

try:
    import orjson
    HAS_ORJSON = True
except ImportError:
    HAS_ORJSON = False

logger = logging.getLogger(__name__)


def get_file_size_mb(file_path: str) -> float:
    """
    Get file size in megabytes.
    
    Args:
        file_path: Path to the file
        
    Returns:
        File size in MB, or 0 if file doesn't exist
    """
    try:
        if os.path.exists(file_path):
            size_bytes = os.path.getsize(file_path)
            return size_bytes / (1024 * 1024)
        return 0.0
    except OSError as e:
        logger.warning(f"Could not get file size for {file_path}: {e}")
        return 0.0


def safe_read_json_with_retry(
    file_path: str,
    max_retries: int = 5,
    retry_delay: float = 1.0,
    validate_structure: bool = True
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Safely read a JSON file with retry logic for handling file locks and race conditions.
    
    Args:
        file_path: Path to the JSON file
        max_retries: Maximum number of retry attempts
        retry_delay: Delay between retries in seconds
        validate_structure: If True, validate that the JSON is a dict
        
    Returns:
        Tuple of (data, error): 
            - (dict, None) on success
            - (None, error_message) on failure
    """
    last_error = None
    
    for attempt in range(max_retries):
        try:
            # Check if file exists
            if not os.path.exists(file_path):
                return None, f"File not found: {file_path}"
            
            # Check if file is empty
            if os.path.getsize(file_path) == 0:
                if attempt < max_retries - 1:
                    logger.debug(f"File is empty, retrying in {retry_delay}s... (attempt {attempt + 1}/{max_retries})")
                    time.sleep(retry_delay)
                    continue
                return None, f"File is empty after {max_retries} attempts: {file_path}"
            
            # Read and parse JSON
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Validate structure if required
            if validate_structure:
                if not isinstance(data, dict):
                    return None, f"Invalid JSON structure: expected dict, got {type(data).__name__}"
            
            # Success
            return data, None
            
        except json.JSONDecodeError as e:
            last_error = f"JSON decode error: {e}"
            if attempt < max_retries - 1:
                logger.debug(f"JSON decode error, retrying in {retry_delay}s... (attempt {attempt + 1}/{max_retries})")
                time.sleep(retry_delay)
                continue
                
        except PermissionError as e:
            last_error = f"Permission denied: {e}"
            if attempt < max_retries - 1:
                logger.debug(f"Permission denied, retrying in {retry_delay}s... (attempt {attempt + 1}/{max_retries})")
                time.sleep(retry_delay)
                continue
                
        except IOError as e:
            last_error = f"I/O error: {e}"
            if attempt < max_retries - 1:
                logger.debug(f"I/O error, retrying in {retry_delay}s... (attempt {attempt + 1}/{max_retries})")
                time.sleep(retry_delay)
                continue
                
        except Exception as e:
            last_error = f"Unexpected error: {type(e).__name__}: {e}"
            if attempt < max_retries - 1:
                logger.debug(f"Unexpected error, retrying in {retry_delay}s... (attempt {attempt + 1}/{max_retries})")
                time.sleep(retry_delay)
                continue
    
    return None, f"Failed after {max_retries} attempts. Last error: {last_error}"


def safe_write_json_atomic(
    file_path: str,
    data: Dict[str, Any],
    indent: int = 2,
    ensure_ascii: bool = False
) -> Tuple[bool, Optional[str]]:
    """
    Safely write JSON to a file using atomic write pattern.
    
    This prevents data corruption by:
    1. Writing to a temporary file first
    2. Flushing and syncing to disk
    3. Atomically renaming temp file to target file
    
    Args:
        file_path: Target file path
        data: Data to write as JSON
        indent: JSON indentation level
        ensure_ascii: If False, allow non-ASCII characters
        
    Returns:
        Tuple of (success, error):
            - (True, None) on success
            - (False, error_message) on failure
    """
    try:
        # Ensure parent directory exists
        parent_dir = os.path.dirname(file_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        
        # Create temporary file in same directory for atomic rename
        fd, temp_path = tempfile.mkstemp(
            suffix='.tmp',
            prefix='json_',
            dir=parent_dir or '.'
        )
        
        try:
            # Write to temp file (orjson is much faster for large numeric payloads;
            # falls back to stdlib json if orjson isn't installed)
            if HAS_ORJSON:
                option = orjson.OPT_INDENT_2 if indent else 0
                with os.fdopen(fd, 'wb') as f:
                    f.write(orjson.dumps(data, option=option))
                    f.flush()
                    os.fsync(f.fileno())
            else:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)
                    f.flush()
                    os.fsync(f.fileno())  # Ensure data is written to disk
            
            # Atomic rename (on Windows, need to remove target first)
            if os.name == 'nt' and os.path.exists(file_path):
                # Windows: remove target before rename
                try:
                    os.remove(file_path)
                except OSError:
                    pass  # Target may not exist
            
            shutil.move(temp_path, file_path)
            return True, None
            
        except Exception as e:
            # Clean up temp file on error
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            raise e
            
    except PermissionError as e:
        return False, f"Permission denied: {e}"
        
    except IOError as e:
        return False, f"I/O error: {e}"
        
    except Exception as e:
        return False, f"Unexpected error: {type(e).__name__}: {e}"


def safe_read_file_with_retry(
    file_path: str,
    max_retries: int = 5,
    retry_delay: float = 1.0
) -> Tuple[Optional[str], Optional[str]]:
    """
    Safely read a text file with retry logic.
    
    Args:
        file_path: Path to the file
        max_retries: Maximum number of retry attempts
        retry_delay: Delay between retries in seconds
        
    Returns:
        Tuple of (content, error):
            - (str, None) on success
            - (None, error_message) on failure
    """
    last_error = None
    
    for attempt in range(max_retries):
        try:
            if not os.path.exists(file_path):
                return None, f"File not found: {file_path}"
            
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            return content, None
            
        except PermissionError as e:
            last_error = f"Permission denied: {e}"
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
                
        except IOError as e:
            last_error = f"I/O error: {e}"
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
                
        except Exception as e:
            last_error = f"Unexpected error: {type(e).__name__}: {e}"
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                continue
    
    return None, f"Failed after {max_retries} attempts. Last error: {last_error}"


def safe_write_file_atomic(
    file_path: str,
    content: str,
    encoding: str = 'utf-8'
) -> Tuple[bool, Optional[str]]:
    """
    Safely write text content to a file using atomic write pattern.
    
    Args:
        file_path: Target file path
        content: Text content to write
        encoding: File encoding
        
    Returns:
        Tuple of (success, error):
            - (True, None) on success
            - (False, error_message) on failure
    """
    try:
        parent_dir = os.path.dirname(file_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        
        fd, temp_path = tempfile.mkstemp(
            suffix='.tmp',
            prefix='file_',
            dir=parent_dir or '.'
        )
        
        try:
            with os.fdopen(fd, 'w', encoding=encoding) as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            
            if os.name == 'nt' and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass
            
            shutil.move(temp_path, file_path)
            return True, None
            
        except Exception as e:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            raise e
            
    except Exception as e:
        return False, f"Error writing file: {type(e).__name__}: {e}"
