# Alarm Playback Module

A robust Python system for waking Spotify Connect speakers (especially Devialet Phantom) and starting playback at alarm time.

## Features

- **Resilient Discovery**: Performs mDNS discovery, Zeroconf getInfo, and addUser flows to wake Spotify Connect speakers
- **Spotify Web API Integration**: Control playback, volume, and device selection
- **Circuit Breaker**: Prevents repeated failures on problematic devices
- **Structured Logging**: JSON logging with detailed metrics and timing
- **CLI Testing Tools**: Comprehensive command-line interface for testing
- **Precise Spotify Connect Control**: Handles discovery, authentication, and playback timeline for Spotify devices
- **Guided Recovery**: Logs clear manual steps when Spotify refuses to expose the device

## Quick Start

### 1. Installation

```bash
# Clone the repository
git clone https://github.com/nadigo/Wakeify
cd Wakeify/playback

# Install dependencies
pip install -r requirements.txt

# Install the package
pip install -e .
```

### 2. Configuration

Copy the environment template from the project root and configure your Spotify credentials:

```bash
cp ../.env.example ../.env
cd ..
```

Edit `.env` with your Spotify app credentials:

```env
# Spotify API Credentials (required)
SPOTIFY_CLIENT_ID=your_client_id_here
SPOTIFY_CLIENT_SECRET=your_client_secret_here
SPOTIFY_REFRESH_TOKEN=your_refresh_token_here

# Default playlist for alarms
ALARM_CONTEXT_URI=spotify:playlist:your_default_playlist_id

# Logging
LOG_LEVEL=INFO
LOG_FORMAT=json
```

### 3. Spotify App Setup

1. Go to [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)
2. Create a new app
3. Set redirect URI to `https://your-container-ip/callback` (e.g., `https://192.168.1.11/callback`)
4. Note down your Client ID and Client Secret
5. Generate a refresh token using the authorization flow

### 4. Testing

Test your setup with the CLI:

```bash
# Discover devices on your network
alarm-cli discover-all

# Test a specific device
alarm-cli discover "Phantom"

# Test device wake-up
alarm-cli touch "Phantom"

# Test authentication
alarm-cli adduser "Phantom" --mode blob_clientKey

# List Spotify devices
alarm-cli list-devices

# Test immediate playback
alarm-cli play "Phantom" --context "spotify:playlist:your_playlist_id"

# Run full alarm simulation
alarm-cli alarm "Phantom"
```

## Usage

### Basic Python Usage

```python
from alarm_playback import AlarmPlaybackEngine, AlarmPlaybackConfig

# Load configuration from environment
config = AlarmPlaybackConfig.from_env()

# Add your target devices
config.targets = [
    DeviceProfile(
        name="Phantom",
        volume_preset=35,
        auth_mode_for_adduser="blob_clientKey",
        capabilities=["connect", "airplay"],
        fallback_policy="both"
    )
]

# Create engine and run alarm
engine = AlarmPlaybackEngine(config)
metrics = engine.play_alarm("Phantom")

print(f"Alarm completed via: {metrics.branch}")
print(f"Total duration: {metrics.total_duration_ms}ms")
```

### Configuration Options

#### Device Profiles

```python
DeviceProfile(
    name="Phantom",                    # Friendly name for device matching
    ip="192.168.1.100",               # Static IP (optional)
    cpath="/spotify",                 # Zeroconf CPath (optional)
    port=8080,                        # Zeroconf port (optional)
    volume_preset=35,                 # Volume level 0-100
    auth_mode_for_adduser="blob_clientKey",  # Authentication mode
    capabilities=["connect", "airplay"],     # Device capabilities
    fallback_policy="both",           # Fallback behavior
    max_wake_wait_s=22               # Max time to wait for device wake
)
```

#### Timing Configuration

```python
Timings(
    prewarm_s=60,                     # Pre-warm time (T-60s)
    poll_fast_period_s=5.0,           # Fast polling period
    total_poll_deadline_s=20,         # Total polling deadline
    debounce_after_seen_s=0.6,        # Debounce after device seen
    retry_404_delay_s=0.7,            # Delay before 404 retry
    failover_fire_after_s=2.0         # Failover timeout (T+2s)
)
```

## Architecture

### Timeline Flow

1. **T-60s Pre-warm**: mDNS discovery to find device
2. **T-30s Activate**: getInfo check to wake device
3. **T-10s Poll**: Fast polling for device to appear in Spotify API
4. **T-0 Fire**: Transfer playback, set volume, start playing
5. **T+2s Confirm**: Verify playback is active; if Spotify still hides the device, log manual recovery instructions.

### State Machine

```
UNKNOWN → DISCOVERED → LOCAL_AWAKE → LOGGED_IN → CLOUD_VISIBLE → STAGED → PLAYING
                ↓
     DEEP_SLEEP_SUSPECTED
```

## Dependencies

### Required External Services

None. Wakeify interacts only with Spotify Web API and Zeroconf-enabled speakers.

### System Dependencies

```bash
# Ubuntu/Debian
sudo apt-get install avahi-utils

# macOS
brew install avahi
```

## CLI Commands

### Device Discovery
- `