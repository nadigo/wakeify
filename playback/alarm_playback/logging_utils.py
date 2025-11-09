"""
Logging utilities for structured logging
"""

import logging
import json
import sys
from datetime import datetime
from typing import Any, Dict, Optional


class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging"""
    
    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON"""
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno
        }
        
        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # Add extra fields from record
        for key, value in record.__dict__.items():
            if key not in ('name', 'msg', 'args', 'levelname', 'levelno', 'pathname',
                          'filename', 'module', 'lineno', 'funcName', 'created',
                          'msecs', 'relativeCreated', 'thread', 'threadName',
                          'processName', 'process', 'getMessage', 'exc_info',
                          'exc_text', 'stack_info'):
                log_data[key] = value
        
        return json.dumps(log_data, ensure_ascii=False)


class AlarmPlaybackFilter(logging.Filter):
    """Filter for alarm playback specific logging"""
    
    def filter(self, record: logging.LogRecord) -> bool:
        """Filter log records"""
        # Suppress zeroconf cleanup exceptions (non-fatal)
        if record.levelno == logging.ERROR:
            msg = record.getMessage()
            # Check logger name or message content for zeroconf issues
            if ("zeroconf" in record.name.lower() or "asyncio" in record.name.lower()) and "ServiceBrowser" in msg:
                if "KeyError" in msg or "_ServiceBrowserBase._async_cancel" in msg or "_async_cancel" in msg:
                    return False  # Suppress these logs
        
        # Add device context if available
        if hasattr(record, 'device_name'):
            record.device_context = {
                "device_name": record.device_name
            }
        
        # Add phase context if available
        if hasattr(record, 'phase'):
            record.phase_context = {
                "phase": record.phase
            }
        
        return True


def setup_logging(log_level: str = "INFO", log_format: str = "json", 
                 log_file: Optional[str] = None) -> None:
    """
    Setup structured logging for alarm playback system.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_format: Log format ("json" or "text")
        log_file: Optional log file path
    """
    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))
    
    # Clear existing handlers
    root_logger.handlers.clear()
    
    # Create formatter
    if log_format.lower() == "json":
        formatter = JSONFormatter()
    elif log_format.lower() == "simple":
        formatter = logging.Formatter(
            '%(asctime)s INFO %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    else:
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(AlarmPlaybackFilter())
    root_logger.addHandler(console_handler)
    
    # File handler if specified
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(AlarmPlaybackFilter())
        root_logger.addHandler(file_handler)
    
    # Set specific logger levels
    logging.getLogger('zeroconf').setLevel(logging.WARNING)
    logging.getLogger('spotipy').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    
    # Disable uvicorn logging
    logging.getLogger('uvicorn.access').disabled = True
    logging.getLogger('uvicorn').disabled = True
    logging.getLogger('uvicorn.error').disabled = True


def get_logger(name: str) -> logging.Logger:
    """
    Get logger with alarm playback context.
    
    Args:
        name: Logger name
        
    Returns:
        Logger instance
    """
    return logging.getLogger(name)


def log_phase_start(logger: logging.Logger, phase: str, device_name: str, 
                   **kwargs) -> None:
    """
    Log the start of a phase.
    
    Args:
        logger: Logger instance
        phase: Phase name
        device_name: Device name
        **kwargs: Additional context
    """
    logger.info(
        f"Starting phase: {phase}",
        extra={
            "phase": phase,
            "device_name": device_name,
            "phase_action": "start",
            **kwargs
        }
    )


def log_phase_end(logger: logging.Logger, phase: str, device_name: str,
                 duration_ms: Optional[int] = None, success: bool = True,
                 **kwargs) -> None:
    """
    Log the end of a phase.
    
    Args:
        logger: Logger instance
        phase: Phase name
        device_name: Device name
        duration_ms: Phase duration in milliseconds
        success: Whether phase was successful
        **kwargs: Additional context
    """
    logger.info(
        f"Completed phase: {phase} (success: {success})",
        extra={
            "phase": phase,
            "device_name": device_name,
            "phase_action": "end",
            "duration_ms": duration_ms,
            "success": success,
            **kwargs
        }
    )


def log_device_discovery(logger: logging.Logger, device_name: str, 
                        discovery_result: Dict[str, Any]) -> None:
    """
    Log device discovery results.
    
    Args:
        logger: Logger instance
        device_name: Device name
        discovery_result: Discovery result data
    """
    logger.info(
        f"Device discovery result: {device_name}",
        extra={
            "device_name": device_name,
            "event_type": "discovery",
            "discovery_result": discovery_result
        }
    )


def log_device_state_change(logger: logging.Logger, device_name: str,
                           old_state: str, new_state: str, **kwargs) -> None:
    """
    Log device state changes.
    
    Args:
        logger: Logger instance
        device_name: Device name
        old_state: Previous state
        new_state: New state
        **kwargs: Additional context
    """
    logger.info(
        f"Device state change: {old_state} -> {new_state}",
        extra={
            "device_name": device_name,
            "event_type": "state_change",
            "old_state": old_state,
            "new_state": new_state,
            **kwargs
        }
    )


def log_playback_event(logger: logging.Logger, device_name: str, event_type: str,
                      **kwargs) -> None:
    """
    Log playback events.
    
    Args:
        logger: Logger instance
        device_name: Device name
        event_type: Type of playback event
        **kwargs: Additional context
    """
    logger.info(
        f"Playback event: {event_type}",
        extra={
            "device_name": device_name,
            "event_type": "playback",
            "playback_action": event_type,
            **kwargs
        }
    )


def log_error(logger: logging.Logger, device_name: str, error: Exception,
              context: Optional[Dict[str, Any]] = None) -> None:
    """
    Log errors with context.
    
    Args:
        logger: Logger instance
        device_name: Device name
        error: Exception that occurred
        context: Additional context
    """
    logger.error(
        f"Error occurred: {str(error)}",
        extra={
            "device_name": device_name,
            "event_type": "error",
            "error_type": type(error).__name__,
            "error_message": str(error),
            "context": context or {}
        },
        exc_info=True
    )


def log_metrics(logger: logging.Logger, device_name: str, metrics: Dict[str, Any]) -> None:
    """
    Log performance metrics.
    
    Args:
        logger: Logger instance
        device_name: Device name
        metrics: Metrics data
    """
    logger.info(
        f"Performance metrics for {device_name}",
        extra={
            "device_name": device_name,
            "event_type": "metrics",
            "metrics": metrics
        }
    )

