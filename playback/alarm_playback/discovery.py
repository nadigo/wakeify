"""
mDNS/DNS-SD discovery for Spotify Connect devices
"""

import socket
import time
import logging
from threading import Event
from typing import Dict, List, Optional
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf, ServiceInfo

from .models import DiscoveryResult

logger = logging.getLogger(__name__)


class SpotifyConnectListener(ServiceListener):
    """Service listener for Spotify Connect devices"""
    
    def __init__(self, instance_hint: Optional[str] = None):
        self.discovered_services: List[DiscoveryResult] = []
        self.instance_hint = instance_hint
        self._completed = False
        self._has_new_service = Event()
        self._services_by_instance: Dict[str, DiscoveryResult] = {}
    
    def add_service(self, zeroconf: Zeroconf, type_: str, name: str) -> None:
        """Called when a Spotify Connect service is discovered"""
        logger.debug(f"Discovered service: {name} (type: {type_})")
        
        info = zeroconf.get_service_info(type_, name)
        if not info:
            logger.warning(f"Could not get service info for {name}")
            return
        
        # Extract service details
        ip_addresses = []
        try:
            ip_addresses = info.parsed_addresses()
        except AttributeError:
            pass  # Older zeroconf versions may not expose parsed_addresses()
        
        ip = ip_addresses[0] if ip_addresses else (socket.inet_ntoa(info.addresses[0]) if info.addresses else None)
        port = info.port
        cpath = None
        # Extract instance name from the service name (format: instance_name._spotify-connect._tcp.local.)
        instance_name = name.split('.')[0]
        
        # Extract CPath from TXT records
        if info.properties:
            cpath = info.properties.get(b'CPath', b'').decode('utf-8')
            if cpath and not cpath.startswith('/'):
                cpath = '/' + cpath
        
        # Filter by instance hint if provided
        if self.instance_hint and self.instance_hint.lower() not in instance_name.lower():
            logger.debug(f"Skipping {instance_name} - doesn't match hint '{self.instance_hint}'")
            return
        
        # Convert TXT records to dict
        txt_records = {}
        if info.properties:
            for key, value in info.properties.items():
                txt_key = key.decode('utf-8', errors='ignore')
                if not txt_key:
                    continue
                txt_records[txt_key] = value.decode('utf-8', errors='ignore') if isinstance(value, bytes) else str(value)
        
        result = DiscoveryResult(
            ip=ip,
            port=port,
            cpath=cpath,
            instance_name=instance_name,
            txt_records=txt_records
        )
        
        key = instance_name.lower()
        existing = self._services_by_instance.get(key)
        self._services_by_instance[key] = result
        if existing:
            # Update in-place to keep deterministic ordering
            for idx, existing_result in enumerate(self.discovered_services):
                if existing_result.instance_name.lower() == key:
                    self.discovered_services[idx] = result
                    break
        else:
            self.discovered_services.append(result)
        
        self._has_new_service.set()
        logger.debug(f"Added service: {instance_name} at {ip}:{port} (cpath: {cpath})")
    
    def remove_service(self, zeroconf: Zeroconf, type_: str, name: str) -> None:
        """Called when a service is removed"""
        logger.debug(f"Service removed: {name}")
    
    def update_service(self, zeroconf: Zeroconf, type_: str, name: str) -> None:
        """Called when a service is updated"""
        logger.debug(f"Service updated: {name}")

    def wait_for_first(self, timeout_s: float) -> bool:
        """Block until at least one service is discovered or timeout expires."""
        if self._has_new_service.is_set():
            return True
        self._has_new_service.clear()
        return self._has_new_service.wait(timeout_s)

    def wait_for_accumulation(self, total_timeout_s: float, idle_grace_s: float = 0.3) -> None:
        """
        Wait for services to accumulate up to a total timeout, with an idle grace window
        to collect late-arriving responses.
        """
        deadline = time.monotonic() + max(0.0, total_timeout_s)
        if self._has_new_service.is_set():
            self._has_new_service.clear()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not self._has_new_service.wait(remaining):
                break
            self._has_new_service.clear()
            if idle_grace_s <= 0:
                continue
            idle_deadline = time.monotonic() + idle_grace_s
            while True:
                idle_remaining = idle_deadline - time.monotonic()
                if idle_remaining <= 0:
                    break
                if self._has_new_service.wait(idle_remaining):
                    self._has_new_service.clear()
                    idle_deadline = time.monotonic() + idle_grace_s
                else:
                    break

    def snapshot(self) -> List[DiscoveryResult]:
        """Return a deduplicated snapshot of discovered services."""
        if not self._services_by_instance:
            return []
        return list(self._services_by_instance.values())


def mdns_discover_connect(instance_hint: Optional[str] = None, timeout_s: float = 1.5) -> DiscoveryResult:
    """
    Browse DNS-SD service '_spotify-connect._tcp.local.'.
    
    Args:
        instance_hint: Optional hint to filter instances by name
        timeout_s: Discovery timeout in seconds
        
    Returns:
        DiscoveryResult with device information, or empty result if none found
    """
    logger.info(f"Starting mDNS discovery for Spotify Connect devices (hint: {instance_hint}, timeout: {timeout_s}s)")
    
    zeroconf = Zeroconf()
    listener = SpotifyConnectListener(instance_hint)
    
    try:
        # Browse for Spotify Connect services
        logger.info(f"Browsing for services of type: _spotify-connect._tcp.local.")
        browser = ServiceBrowser(zeroconf, "_spotify-connect._tcp.local.", listener)
        
        # Wait for discovery
        listener.wait_for_first(timeout_s)
        
        # Stop browsing (suppress cleanup errors)
        try:
            browser.cancel()
        except Exception as e:
            # Known issue with zeroconf cleanup - non-fatal
            logger.debug(f"ServiceBrowser cleanup warning (non-fatal): {e}")
        
        # Return best match
        services = listener.snapshot()
        if not services:
            services = listener.discovered_services

        if services:
            # Try exact matches first
            if instance_hint:
                exact_matches = [
                    s for s in services 
                    if instance_hint.lower() == s.instance_name.lower()
                ]
                if exact_matches:
                    result = exact_matches[0]
                    logger.info(f"Found exact match for hint '{instance_hint}'")
                else:
                    # Try partial matches (case-insensitive)
                    partial_matches = [
                        s for s in services 
                        if instance_hint.lower() in s.instance_name.lower() or s.instance_name.lower() in instance_hint.lower()
                    ]
                    if partial_matches:
                        result = partial_matches[0]
                        logger.info(f"Found partial match for hint '{instance_hint}': {result.instance_name}")
                    else:
                        # No match, use first device
                        result = services[0]
                        logger.info(f"No match for hint '{instance_hint}', using first available: {result.instance_name}")
            else:
                # No hint, use first device
                result = services[0]
            
            logger.info(f"Discovery successful: {result.instance_name} at {result.ip}:{result.port}")
            return result
        else:
            logger.warning("No Spotify Connect devices discovered")
            return DiscoveryResult()
    
    except Exception as e:
        logger.error(f"mDNS discovery failed: {e}")
        return DiscoveryResult()
    
    finally:
        zeroconf.close()


def discover_all_connect_devices(timeout_s: float = 3.0) -> List[DiscoveryResult]:
    """
    Discover all available Spotify Connect devices on the network.
    
    Args:
        timeout_s: Discovery timeout in seconds
        
    Returns:
        List of DiscoveryResult objects for all found devices
    """
    logger.info(f"Discovering all Spotify Connect devices (timeout: {timeout_s}s)")
    
    zeroconf = None
    browser = None
    
    try:
        zeroconf = Zeroconf()
        listener = SpotifyConnectListener()  # No hint = discover all
        
        browser = ServiceBrowser(zeroconf, "_spotify-connect._tcp.local.", listener)
        logger.debug("ServiceBrowser created, waiting for discovery...")
        
        # Wait for discovery
        listener.wait_for_accumulation(timeout_s)
        
        # Stop browsing (suppress cleanup errors)
        try:
            browser.cancel()
        except Exception as e:
            # Known issue with zeroconf cleanup - non-fatal
            logger.debug(f"ServiceBrowser cleanup warning (non-fatal): {e}")
        
        services = listener.snapshot()
        if not services:
            services = listener.discovered_services
        logger.info(f"Discovered {len(services)} Spotify Connect devices")
        return services
    
    except Exception as e:
        logger.error(f"Full discovery failed: {e}")
        import traceback
        traceback.print_exc()
        return []
    
    finally:
        if browser:
            try:
                browser.cancel()
            except:
                pass
        if zeroconf:
            try:
                zeroconf.close()
            except:
                pass


def resolve_service_info(service_name: str, timeout_s: float = 2.0) -> Optional[DiscoveryResult]:
    """
    Resolve specific service information by name.
    
    Args:
        service_name: Full service name (e.g., "Device._spotify-connect._tcp.local.")
        timeout_s: Resolution timeout in seconds
        
    Returns:
        DiscoveryResult if successful, None otherwise
    """
    logger.info(f"Resolving service info for: {service_name}")
    
    zeroconf = Zeroconf()
    
    try:
        info = zeroconf.get_service_info("_spotify-connect._tcp.local.", service_name)
        if not info:
            logger.warning(f"Could not resolve service info for {service_name}")
            return None
        
        # Extract service details
        ip = socket.inet_ntoa(info.addresses[0]) if info.addresses else None
        port = info.port
        cpath = None
        instance_name = info.name.split('.')[0]
        
        # Extract CPath from TXT records
        if info.properties:
            cpath = info.properties.get(b'CPath', b'').decode('utf-8')
            if cpath and not cpath.startswith('/'):
                cpath = '/' + cpath
        
        # Convert TXT records to dict
        txt_records = {}
        if info.properties:
            for key, value in info.properties.items():
                txt_records[key.decode('utf-8')] = value.decode('utf-8')
        
        result = DiscoveryResult(
            ip=ip,
            port=port,
            cpath=cpath,
            instance_name=instance_name,
            txt_records=txt_records
        )
        
        logger.info(f"Resolved service: {instance_name} at {ip}:{port}")
        return result
    
    except Exception as e:
        logger.error(f"Service resolution failed: {e}")
        return None
    
    finally:
        zeroconf.close()

