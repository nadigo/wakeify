"""
CLI for manual testing of alarm playback system
"""

import click
import json
import sys
import os
from typing import Optional

from .config import AlarmPlaybackConfig, DeviceProfile
from .discovery import mdns_discover_connect, discover_all_connect_devices
from .zeroconf_client import get_info, add_user, check_device_health
from .spotify_api import SpotifyApiWrapper, TokenManager
from .orchestrator import AlarmPlaybackEngine
from .logging_utils import setup_logging, get_logger

logger = get_logger(__name__)


@click.group()
@click.option('--config-file', '-c', help='Configuration file path')
@click.option('--log-level', default='INFO', help='Log level')
@click.option('--log-format', default='text', type=click.Choice(['text', 'json']), help='Log format')
@click.pass_context
def cli(ctx, config_file, log_level, log_format):
    """Alarm Playback CLI - Test and control alarm playback system"""
    # Setup logging
    setup_logging(log_level=log_level, log_format=log_format)
    
    # Load configuration
    if config_file and os.path.exists(config_file):
        # Load from file (would need to implement YAML loading)
        ctx.obj = {'config': None}
    else:
        # Load from environment
        ctx.obj = {'config': AlarmPlaybackConfig.from_env()}
    
    ctx.ensure_object(dict)


@cli.command()
@click.argument('device_name')
@click.option('--timeout', '-t', default=1.5, help='Discovery timeout in seconds')
def discover(device_name, timeout):
    """Discover Spotify Connect devices via mDNS"""
    logger.info(f"Discovering device: {device_name}")
    
    result = mdns_discover_connect(device_name, timeout_s=timeout)
    
    if result.is_complete:
        click.echo(f"Found device: {result.instance_name}")
        click.echo(f"  IP: {result.ip}")
        click.echo(f"  Port: {result.port}")
        click.echo(f"  CPath: {result.cpath}")
        if result.txt_records:
            click.echo(f"  TXT Records: {result.txt_records}")
    else:
        click.echo(f"Device '{device_name}' not found")
        sys.exit(1)


@cli.command()
def discover_all():
    """Discover all Spotify Connect devices on the network"""
    logger.info("Discovering all Spotify Connect devices")
    
    devices = discover_all_connect_devices(timeout_s=3.0)
    
    if devices:
        click.echo(f"Found {len(devices)} Spotify Connect devices:")
        for device in devices:
            click.echo(f"  - {device.instance_name} at {device.ip}:{device.port}")
            if device.cpath:
                click.echo(f"    CPath: {device.cpath}")
    else:
        click.echo("No Spotify Connect devices found")


@cli.command()
@click.argument('device_name')
@click.option('--ip', help='Device IP address')
@click.option('--port', type=int, help='Device port')
@click.option('--cpath', help='Device CPath')
def touch(device_name, ip, port, cpath):
    """Test getInfo endpoint on a device"""
    logger.info(f"Testing getInfo for device: {device_name}")
    
    # If not provided, try to discover the device
    if not all([ip, port, cpath]):
        result = mdns_discover_connect(device_name, timeout_s=1.5)
        if not result.is_complete:
            click.echo(f"Could not discover device '{device_name}'")
            sys.exit(1)
        ip = result.ip
        port = result.port
        cpath = result.cpath
    
    # Test getInfo
    success = get_info(ip, port, cpath, timeout_s=1.5)
    
    if success:
        click.echo(f"Device {device_name} is responding to getInfo")
    else:
        click.echo(f"Device {device_name} is not responding to getInfo")
        sys.exit(1)


@cli.command()
@click.argument('device_name')
@click.option('--mode', type=click.Choice(['blob_clientKey', 'access_token']), 
              default='blob_clientKey', help='Authentication mode')
@click.option('--ip', help='Device IP address')
@click.option('--port', type=int, help='Device port')
@click.option('--cpath', help='Device CPath')
@click.pass_context
def adduser(ctx, device_name, mode, ip, port, cpath):
    """Test addUser authentication on a device"""
    logger.info(f"Testing addUser for device: {device_name} (mode: {mode})")
    
    config = ctx.obj['config']
    if not config:
        click.echo("Configuration not loaded")
        sys.exit(1)
    
    # If not provided, try to discover the device
    if not all([ip, port, cpath]):
        result = mdns_discover_connect(device_name, timeout_s=1.5)
        if not result.is_complete:
            click.echo(f"Could not discover device '{device_name}'")
            sys.exit(1)
        ip = result.ip
        port = result.port
        cpath = result.cpath
    
    try:
        # Create token manager and get credentials
        token_manager = TokenManager(config.spotify)
        api = SpotifyApiWrapper(token_manager)
        
        from .adapters.adduser_spotifywebapipython import create_credential_provider
        provider = create_credential_provider(api._get_client(), "generic")
        
        if mode == "blob_clientKey":
            creds = provider.get_blob_clientkey_creds()
        else:
            creds = provider.get_access_token_creds()
        
        # Test addUser
        success = add_user(ip, port, cpath, mode, creds, timeout_s=2.5)
        
        if success:
            click.echo(f"Successfully authenticated with device {device_name}")
        else:
            click.echo(f"Authentication failed for device {device_name}")
            sys.exit(1)
            
    except Exception as e:
        click.echo(f"Error during authentication: {e}")
        sys.exit(1)


@cli.command()
@click.pass_context
def list_devices(ctx):
    """List available Spotify devices"""
    logger.info("Listing Spotify devices")
    
    config = ctx.obj['config']
    if not config:
        click.echo("Configuration not loaded")
        sys.exit(1)
    
    try:
        token_manager = TokenManager(config.spotify)
        api = SpotifyApiWrapper(token_manager)
        
        devices = api.get_devices(force_refresh=True)
        
        if devices:
            click.echo(f"Found {len(devices)} Spotify devices:")
            for device in devices:
                status = "Active" if device.is_active else "Inactive"
                volume = f" (Volume: {device.volume_percent}%)" if device.volume_percent else ""
                click.echo(f"  - {device.name} - {device.id} {status}{volume}")
        else:
            click.echo("No Spotify devices found")
            
    except Exception as e:
        click.echo(f"Error listing devices: {e}")
        sys.exit(1)


@cli.command()
@click.argument('device_name')
@click.option('--context', help='Context URI to play')
@click.option('--volume', type=int, help='Volume level (0-100)')
@click.pass_context
def play(ctx, device_name, context, volume):
    """Test immediate playback on a device"""
    logger.info(f"Testing immediate playback on device: {device_name}")
    
    config = ctx.obj['config']
    if not config:
        click.echo("Configuration not loaded")
        sys.exit(1)
    
    try:
        token_manager = TokenManager(config.spotify)
        api = SpotifyApiWrapper(token_manager)
        
        # Get devices
        devices = api.get_devices(force_refresh=True)
        target_device = None
        
        for device in devices:
            if device.name == device_name:
                target_device = device
                break
        
        if not target_device:
            click.echo(f"Device '{device_name}' not found in Spotify devices")
            sys.exit(1)
        
        # Set volume if specified
        if volume is not None:
            api.put_volume(target_device.id, volume)
            click.echo(f"Set volume to {volume}%")
        
        # Start playback
        context_uri = context or config.context_uri
        if not context_uri:
            click.echo("No context URI provided or configured")
            sys.exit(1)
        
        api.put_play(target_device.id, context_uri)
        click.echo(f"Started playback on {device_name}")
        
    except Exception as e:
        click.echo(f"Error during playback: {e}")
        sys.exit(1)


@cli.command()
@click.argument('device_name')
@click.option('--context', help='Context URI to play')
@click.pass_context
def alarm(ctx, device_name, context):
    """Run full alarm timeline simulation"""
    logger.info(f"Running alarm simulation for device: {device_name}")
    
    config = ctx.obj['config']
    if not config:
        click.echo("Configuration not loaded")
        sys.exit(1)
    
    # Override context URI if provided
    if context:
        config.context_uri = context
    
    try:
        # Create engine
        engine = AlarmPlaybackEngine(config)
        
        # Run alarm playback
        click.echo(f"Starting alarm playback for {device_name}...")
        metrics = engine.play_alarm(device_name)
        
        # Display results
        click.echo("\nAlarm playback completed!")
        click.echo(f"Branch: {metrics.branch}")
        click.echo(f"Total duration: {metrics.total_duration_ms}ms")
        
        if metrics.discovered_ms:
            click.echo(f"Discovery: {metrics.discovered_ms}ms")
        if metrics.getinfo_ms:
            click.echo(f"GetInfo: {metrics.getinfo_ms}ms")
        if metrics.adduser_ms:
            click.echo(f"AddUser: {metrics.adduser_ms}ms")
        if metrics.cloud_visible_ms:
            click.echo(f"Cloud visible: {metrics.cloud_visible_ms}ms")
        if metrics.play_ms:
            click.echo(f"Play: {metrics.play_ms}ms")
        
        if metrics.errors:
            click.echo(f"\nErrors encountered: {len(metrics.errors)}")
            for error in metrics.errors:
                click.echo(f"  - {error['error']}")
        
    except Exception as e:
        click.echo(f"Alarm playback failed: {e}")
        sys.exit(1)


@cli.command()
@click.argument('device_name')
@click.option('--ip', help='Device IP address')
@click.option('--port', type=int, help='Device port')
@click.option('--cpath', help='Device CPath')
def health(device_name, ip, port, cpath):
    """Check device health"""
    logger.info(f"Checking health for device: {device_name}")
    
    # If not provided, try to discover the device
    if not all([ip, port, cpath]):
        result = mdns_discover_connect(device_name, timeout_s=1.5)
        if not result.is_complete:
            click.echo(f"Could not discover device '{device_name}'")
            sys.exit(1)
        ip = result.ip
        port = result.port
        cpath = result.cpath
    
    # Check health
    health_info = check_device_health(ip, port, cpath, timeout_s=1.0)
    
    click.echo(f"Health check for {device_name}:")
    click.echo(f"  Reachable: {'Yes' if health_info['reachable'] else 'No'}")
    click.echo(f"  Responding: {'Yes' if health_info['responding'] else 'No'}")
    
    if health_info['response_time_ms']:
        click.echo(f"  Response time: {health_info['response_time_ms']:.1f}ms")
    
    if health_info['error']:
        click.echo(f"  Error: {health_info['error']}")
    
    if not health_info['reachable']:
        sys.exit(1)


@cli.command()
@click.pass_context
def status(ctx):
    """Show system status and configuration"""
    config = ctx.obj['config']
    if not config:
        click.echo("Configuration not loaded")
        sys.exit(1)
    
    click.echo("Alarm Playback System Status:")
    click.echo(f"  Log level: {config.log_level}")
    click.echo(f"  Log format: {config.log_format}")
    click.echo(f"  Default context URI: {config.context_uri}")
    click.echo(f"  Target devices: {len(config.targets)}")
    
    for device in config.targets:
        click.echo(f"    - {device.name} (volume: {device.volume_preset}%)")
    
    click.echo("  Fallback: disabled (AirPlay pipeline removed)")


if __name__ == '__main__':
    cli()

