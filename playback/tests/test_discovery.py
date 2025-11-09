"""
Basic smoke tests for discovery module
"""

import pytest
import time
from unittest.mock import Mock, patch

from alarm_playback.discovery import mdns_discover_connect, DiscoveryResult
from alarm_playback.models import DiscoveryResult


class TestDiscovery:
    """Test discovery functionality"""
    
    def test_discovery_result_is_complete(self):
        """Test DiscoveryResult.is_complete property"""
        # Complete result
        complete = DiscoveryResult(
            ip="192.168.1.100",
            port=8080,
            cpath="/spotify",
            instance_name="TestDevice"
        )
        assert complete.is_complete is True
        
        # Incomplete results
        incomplete_ip = DiscoveryResult(port=8080, cpath="/spotify", instance_name="TestDevice")
        assert incomplete_ip.is_complete is False
        
        incomplete_port = DiscoveryResult(ip="192.168.1.100", cpath="/spotify", instance_name="TestDevice")
        assert incomplete_port.is_complete is False
        
        incomplete_cpath = DiscoveryResult(ip="192.168.1.100", port=8080, instance_name="TestDevice")
        assert incomplete_cpath.is_complete is False
    
    @patch('alarm_playback.discovery.ServiceBrowser')
    @patch('alarm_playback.discovery.Zeroconf')
    def test_mdns_discover_connect_timeout(self, mock_zeroconf, mock_browser):
        """Test mDNS discovery timeout behavior"""
        # Mock empty discovery result
        mock_listener = Mock()
        mock_listener.discovered_services = []
        mock_listener.snapshot.return_value = []
        mock_listener.wait_for_first.return_value = False
        
        with patch('alarm_playback.discovery.SpotifyConnectListener', return_value=mock_listener):
            result = mdns_discover_connect("TestDevice", timeout_s=0.1)
            
            assert isinstance(result, DiscoveryResult)
            assert result.is_complete is False
    
    def test_discovery_result_creation(self):
        """Test DiscoveryResult creation and properties"""
        result = DiscoveryResult(
            ip="192.168.1.100",
            port=8080,
            cpath="/spotify",
            instance_name="TestDevice",
            txt_records={"version": "1.0"}
        )
        
        assert result.ip == "192.168.1.100"
        assert result.port == 8080
        assert result.cpath == "/spotify"
        assert result.instance_name == "TestDevice"
        assert result.txt_records == {"version": "1.0"}
        assert result.is_complete is True

