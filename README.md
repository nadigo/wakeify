# Wakeify

**Wake up and smell the coffee ☕**

Wakeify is an alarm system that wakes you up with your favorite Spotify playlists on any Spotify Connect device. Set your alarms and let Wakeify handle the rest.

## Features

- ⏰ **Smart Scheduling**: Schedule multiple alarms for different days
- 🎵 **Spotify Integration**: Play any Spotify playlist on your devices
- 🔍 **Auto Discovery**: Automatically finds all Spotify Connect devices on your network
- 🌐 **Web Interface**: Beautiful, modern UI for managing your alarms
- 🔄 **Reliable Playback**: Multi-layer fallback system ensures your alarm always plays
- 📊 **Detailed Logging**: Track what's happening with structured logs

## Quick Start

### Prerequisites

- A Spotify premium account
- Spotify Connect devices (speakers, smart displays, etc.)
- Docker and Docker Compose installed (macvlan network recommended)

### Installation

#### 1. Clone the Repository

```bash
git clone https://github.com/nadigo/Wakeify.git
cd Wakeify
```

#### 2. Set Up Spotify Developer Account [premium accounts only]

1. Go to [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) 
2. Click "Create App"
3. Enter app name (e.g., "Wakeify")
4. Add app description: "Wakeify alarm system"
5. Check "I understand and agree..."
6. Click "Create"

#### 3. Configure Spotify App

1. In your app settings, click "Edit Settings"
2. Add redirect URI: `https://your-container-ip/callback` (e.g., `https://192.168.1.11/callback`)
   - **Note:** Spotify requires HTTPS for security
3. Scroll down and click "Add"
4. Click "Save" at the bottom
5. Note down your **Client ID** and **Client Secret** (click "View client secret")

#### 4. Configure Environment

```bash
# Copy the example environment file
cp .env.example .env

# Generate a secure app secret
openssl rand -hex 32

# Edit .env and fill in your values
nano .env
```

**Required** environment variables:
```env
SPOTIFY_CLIENT_ID=your_client_id_here
SPOTIFY_CLIENT_SECRET=your_client_secret_here
SPOTIFY_REDIRECT_URI=https://your-container-ip/callback
APP_SECRET=paste_the_generated_secret_here

# Network configuration (adjust for your network)
CONTAINER_IP=192.168.1.11
GATEWAY_IP=192.168.1.1
NETWORK_SUBNET=192.168.1.0/24
NETWORK_INTERFACE=eth0
```

**Important:** Wakeify requires HTTPS. The `SPOTIFY_REDIRECT_URI` must use `https://` protocol.

**Optional** variables:
```env
DEFAULT_SPEAKER=      # Leave empty for auto-detection
DEFAULT_VOLUME=30     # Default volume (0-100)
DEFAULT_SHUFFLE=true  # Shuffle playlists
BASE_DIR=/data/wakeify
LOG_LEVEL=INFO        # DEBUG for more detailed logs
```

#### 5. Set Up Network

Wakeify needs ARP level networking to discover devices on your local network.
Your docker setup needs to support L@ networing, in a private LAN setup (192.168.1.x) macvlan is required (see below) 

```bash
# Create macvlan network (uses values from .env file)
# Adjust CONTAINER_IP, GATEWAY_IP, NETWORK_SUBNET, and NETWORK_INTERFACE in .env first!
docker network create -d macvlan \
  --subnet=192.168.1.0/24 \
  --gateway=192.168.1.1 \
  -o parent=eth0 \
  macvlan
```

**Important:** All network configuration is in `.env`:
- `CONTAINER_IP`: Set to your desired container IP
- `GATEWAY_IP`: Set to your router IP
- `NETWORK_SUBNET`: Set to your network subnet
- `NETWORK_INTERFACE`: Set to your network interface (check with `ip addr`)

#### 6. Start Wakeify

```bash
# Start the service
docker-compose up -d

# Check logs
docker-compose logs -f
```

#### 7. Access Web Interface

1. Open browser to `https://your-container-ip` (Wakeify uses HTTPS only)
2. Accept the self-signed certificate warning (certificates are auto-generated)
3. Click "Connect to Spotify" and authorize Wakeify
4. Start setting up alarms!

## Usage

### Setting an Alarm

1. Open the web interface
2. Select a Spotify playlist from the dropdown
3. Choose your target speaker device
4. Set the alarm time and select days of the week
5. Optional: Set volume and enable shuffle
6. Optional: Set a stop time to automatically stop playback
7. Click "Set Alarm"

### Managing Alarms

- **View Alarms**: All scheduled alarms appear on the home page
- **Test Alarm**: Click "Play Now" to test an alarm immediately
- **Delete Alarm**: Click the trash icon to remove an alarm
- **Stop Playback**: Use the stop button to halt current playback

### Device Discovery

Wakeify automatically discovers all Spotify Connect devices on your network. Devices are cached for fast loading.

To manually refresh devices:
- For detailed device status, use the 'System Status' module by clicking on the icon in the header
- Or use `/api/devices/refresh` endpoint

## How It Works

### Timeline Execution

When an alarm fires, Wakeify follows this timeline:

```
T-60s: Pre-warm (if enabled)
  └─ Discover device via mDNS

T-30s: GetInfo Phase
  └─ Wake device via getInfo endpoint

T-10s: AddUser Phase
  └─ Authenticate device

T-10s to T-0: Cloud Polling
  └─ Wait for device to appear in Spotify API

T-0: Play Phase
  └─ Start playlist playback

T+2s: Failover Check
  └─ Activate fallback if needed
```

### Fallback System

If primary playback fails, Wakeify tries multiple fallback methods:

1. Quick check if device already available
2. Generic IP wake-up (HTTP requests, pings)
3. mDNS queries and authentication
4. Force connection via Spotify API
5. Error logging with helpful instructions

## Configuration

### Environment Variables

See `.env.example` for all available options.

**Required:**
- `SPOTIFY_CLIENT_ID`: Your Spotify app client ID
- `SPOTIFY_CLIENT_SECRET`: Your Spotify app client secret
- `SPOTIFY_REDIRECT_URI`: Your OAuth redirect URI
- `APP_SECRET`: Secure random string for sessions

**Optional:**
- `DEFAULT_VOLUME`: Default volume (0-100)
- `DEFAULT_SHUFFLE`: Enable shuffle by default
- `LOG_LEVEL`: Logging level (INFO, DEBUG)
- `TZ`: Timezone (default: America/New_York)

### Network Requirements

Wakeify requires macvlan networking for device discovery:
- Enables mDNS (Bonjour) discovery
- Allows direct communication with devices
- Network configuration in docker-compose.yml

### SSL Certificates

Wakeify generates self-signed certificates using mkcert during first startup. These are stored in the `ssl/` directory.

For production, consider:
- Using Let's Encrypt certificates
- Installing proper CA-signed certificates
- Using a reverse proxy (nginx, Traefik)

## Troubleshooting

### "No devices found" in dropdown

- Visit `/test/speakers` to force device discovery
- Check Docker logs: `docker-compose logs -f`
- Verify macvlan network is configured correctly
- Ensure `NET_BROADCAST` capability is enabled

### "Alarm not playing on device"

- Open Spotify app on phone/computer
- Select the target device and play any song
- This authenticates the device with your Spotify account
- Retry the alarm

### "Device discovery slow"

This is expected behavior:
- First load: 2-3 seconds (full mDNS scan)
- Subsequent loads: Instant (cached results)
- Cache refreshes every 2 minutes in background

### Container won't start

- Check `.env` file exists and has all required variables
- Verify macvlan network is created
- Check Docker logs for specific errors
- Ensure port 443 is available (Wakeify uses HTTPS only)
- Verify SSL certificates were generated in `ssl/` directory

### SSL certificate errors

- **Normal:** Self-signed certificate warning in browser
- Click "Advanced" → "Proceed to site" (certificates are auto-generated)
- SSL certificates are generated on first container start
- If certificates fail to generate, check Docker logs for mkcert installation errors
- Wakeify requires HTTPS - HTTP access is not supported

## Project Structure

```
Wakeify/
├── app/                   # FastAPI application
│   ├── main.py           # Main entry point
│   ├── alarm_config.py   # Configuration system
│   ├── device_registry.py # Device management
│   ├── spotify_client.py # Spotify API bridge
│   └── templates/        # Web UI
├── playback/             # Playback engine
│   ├── alarm_playback/   # Core playback logic
│   └── requirements.txt  # Dependencies
├── data/                 # Runtime data (git-ignored)
│   ├── alarms.json       # Saved alarms
│   ├── devices.json      # Device cache
│   └── token.json        # OAuth tokens
├── ssl/                  # SSL certificates (git-ignored)
├── tests/                # Test files
├── docker-compose.yml    # Docker configuration
├── .env.example          # Environment template
└── README.md             # This file
```

## Development

### Running Locally

```bash
# Install dependencies
pip install -r app/requirements.txt
pip install -e playback/

# Set up environment
cp .env.example .env
# Edit .env with your values

# Run application
cd app
python main.py
```

### Running Tests

```bash
# Install test dependencies
pip install pytest pytest-asyncio

# Run tests
pytest tests/
```

## Security

**IMPORTANT:** Never commit secrets to version control.

- **HTTPS Only:** Wakeify requires HTTPS for all connections
- All secrets stored in `.env` file (excluded from git)
- `.env.example` provides template without real values
- Token files in `data/` excluded from git
- SSL certificates in `ssl/` excluded from git
- Self-signed certificates auto-generated on first run


## Technology Stack

- **Python 3.11** - Core language
- **FastAPI** - Web framework
- **APScheduler** - Alarm scheduling
- **Spotipy** - Spotify Web API client
- **Zeroconf** - mDNS device discovery
- **Docker** - Containerization

## License

MIT License

Copyright (c) 2024 Wakeify

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Support

Having issues? Check these resources:
1. [DOCUMENTATION.md](DOCUMENTATION.md) - Detailed technical docs
2. Docker logs - `docker-compose logs -f`
3. Debug mode - Set `LOG_LEVEL=DEBUG` in `.env`
4. [GitHub Issues](https://github.com/nadigo/Wakeify/issues) - Report bugs

## Contributing

Contributions welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Submit a pull request

## Version

**Current Version:** 2.0.0  
**Status:** Production Ready

Wake up and smell the coffee ☕
