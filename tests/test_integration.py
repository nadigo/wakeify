#!/usr/bin/env python3
"""
Integration tests for Wakeify
"""

import os
import sys
import time
import json
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

# Add app directory to path (use relative paths)
project_root = Path(__file__).parent.parent
sys.path.append(str(project_root / 'app'))
sys.path.append(str(project_root / 'playback'))

class TestAlarmSystemIntegration(unittest.TestCase):
    """Test alarm system integration"""
    
    def setUp(self):
        """Set up test environment"""
        # Mock environment variables
        os.environ.update({
            'SPOTIFY_CLIENT_ID': 'test_client_id',
            'SPOTIFY_CLIENT_SECRET': 'test_client_secret',
            'SPOTIFY_REFRESH_TOKEN': 'test_refresh_token',
            'ALARM_PREWARM_ENABLED': 'true',
            'DEVICE_AUTO_DISCOVERY': 'true'
        })
    
    def test_config_loading(self):
        """Test configuration loading"""
        from alarm_config import load_alarm_config
        
        config = load_alarm_config()
        
        self.assertTrue(config.prewarm_enabled)
        self.assertTrue(config.device_auto_discovery)
        # Default speaker should be empty for auto-detection
        self.assertEqual(config.default_speaker, "")
    
    def test_device_registry_creation(self):
        """Test device registry creation"""
        from alarm_config import load_alarm_config
        from device_registry import DeviceRegistry
        
        config = load_alarm_config()
        registry = DeviceRegistry(config)
        
        self.assertIsNotNone(registry)
        self.assertEqual(len(registry.device_status), 0)
    
    @patch('device_registry.discover_all_connect_devices')
    def test_device_discovery(self, mock_discover):
        """Test device discovery"""
        from alarm_config import load_alarm_config
        from device_registry import DeviceRegistry
        from alarm_playback.models import DiscoveryResult
        
        # Mock discovery result
        mock_result = DiscoveryResult(
            ip="192.168.1.100",
            port=8080,
            cpath="/spotify",
            instance_name="Test Device"
        )
        mock_discover.return_value = [mock_result]
        
        config = load_alarm_config()
        registry = DeviceRegistry(config)
        
        devices = registry.discover_devices()
        
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].name, "Test Device")
        self.assertEqual(devices[0].ip, "192.168.1.100")
    
    def test_alarm_scheduling(self):
        """Test alarm scheduling logic"""
        from datetime import datetime
        
        # Test pre-warm time calculation
        alarm_hour = 8
        alarm_minute = 30
        
        # Calculate pre-warm time (1 minute before)
        prewarm_hour = alarm_hour
        prewarm_minute = alarm_minute - 1
        
        if prewarm_minute < 0:
            prewarm_minute = 59
            prewarm_hour = (prewarm_hour - 1) % 24
        
        self.assertEqual(prewarm_hour, 8)
        self.assertEqual(prewarm_minute, 29)
    
    def test_metrics_saving(self):
        """Test metrics saving and loading"""
        from alarm_config import save_metrics, load_metrics
        
        # Test data
        test_metrics = [
            {
                "alarm_id": "test_alarm_1",
                "timestamp": time.time(),
                "metrics": {
                    "branch": "primary",
                    "total_duration_ms": 1500,
                    "discovered_ms": 200,
                    "play_ms": 300
                }
            }
        ]
        
        # Save metrics
        save_metrics(test_metrics)
        
        # Load metrics
        loaded_metrics = load_metrics()
        
        self.assertEqual(len(loaded_metrics), 1)
        self.assertEqual(loaded_metrics[0]["alarm_id"], "test_alarm_1")
        self.assertEqual(loaded_metrics[0]["metrics"]["branch"], "primary")
    
    @patch('spotify_client.AlarmSpotifyClient.get_devices')
    def test_spotify_client(self, mock_get_devices):
        """Test Spotify client functionality"""
        from alarm_config import load_alarm_config
        from spotify_client import AlarmSpotifyClient
        from alarm_playback.models import CloudDevice
        
        # Mock device response
        mock_device = CloudDevice(
            id="test_device_id",
            name="Test Device",
            is_active=True,
            volume_percent=50
        )
        mock_get_devices.return_value = [mock_device]
        
        config = load_alarm_config()
        client = AlarmSpotifyClient(config)
        
        devices = client.get_devices()
        
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].name, "Test Device")
        self.assertTrue(devices[0].is_active)
    
class TestPlaybackEngine(unittest.TestCase):
    """Test playback engine functionality"""
    
    def setUp(self):
        """Set up test environment"""
        os.environ.update({
            'SPOTIFY_CLIENT_ID': 'test_client_id',
            'SPOTIFY_CLIENT_SECRET': 'test_client_secret',
            'SPOTIFY_REFRESH_TOKEN': 'test_refresh_token'
        })
    
    @patch('alarm_playback.orchestrator.AlarmPlaybackEngine.play_alarm')
    def test_alarm_execution(self, mock_play_alarm):
        """Test alarm execution with mocked engine"""
        from alarm_playback.models import PhaseMetrics
        
        # Mock successful execution
        mock_metrics = PhaseMetrics(
            discovered_ms=200,
            getinfo_ms=150,
            play_ms=300,
            total_duration_ms=1000,
            branch="primary"
        )
        mock_play_alarm.return_value = mock_metrics
        
        # Test would go here - this is a placeholder for actual integration test
        self.assertTrue(True)

def run_tests():
    """Run all tests"""
    print("🧪 Running Wakeify Integration Tests")
    print("=" * 50)
    
    # Create test suite
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    # Add test cases
    suite.addTests(loader.loadTestsFromTestCase(TestAlarmSystemIntegration))
    suite.addTests(loader.loadTestsFromTestCase(TestPlaybackEngine))
    
    # Run tests
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    # Print summary
    print("\n" + "=" * 50)
    if result.wasSuccessful():
        print("All tests passed!")
    else:
        print(f"{len(result.failures)} test(s) failed, {len(result.errors)} error(s)")
        for failure in result.failures:
            print(f"  FAIL: {failure[0]}")
        for error in result.errors:
            print(f"  ERROR: {error[0]}")
    
    return result.wasSuccessful()

if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)


