# Wakeify - Technical Documentation

> **Architecture, APIs, and implementation details**

## Table of Contents

1. [System Overview](#system-overview)
2. [Architecture](#architecture)
3. [Core Components](#core-components)
4. [API Reference](#api-reference)
5. [Playback Logic](#playback-logic)
6. [Device Discovery](#device-discovery)
7. [Configuration](#configuration)
8. [Troubleshooting](#troubleshooting)
9. [Security](#security)

---

## System Overview

### Purpose

Wakeify is a web-based alarm system that plays Spotify playlists on any Spotify Connect devices at scheduled times with clear recovery guidance when devices need manual attention.

### Key Features

- **Timeline-Based Execution** 
- **Automatic Device Discovery** via mDNS (Zeroconf)
- **Spotify Connect Integration** via Web API
- **APScheduler** for precise timing
- **Web UI** for alarm management
- **Guided Recovery** - Clear instructions when Spotify Connect devices need manual authentication

### Technology Stack

- **Python 3.11** with FastAPI
- **APScheduler** for alarm scheduling
- **spotipy** for Spotify Web API
- **zeroconf** for mDNS discovery
- **Docker** deployment with macvlan networking

---

## Architecture

### System Flow

```
User Sets Alarm → APScheduler → Alarm Time → Timeline Execution → Playback
                                          ↓
                                    If Fails → Fallback
                                          ↓
                                    Alarm Monitoring
```

### Component Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    FastAPI Application                       │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │   Web UI     │  │  Alarm APIs  │  │ Device APIs  │    │
│  │  (Jinja2)    │  │              │  │              │    │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘    │
│         │                  │                  │             │
│  ┌──────▼──────────────────▼──────────────────▼─────────┐  │
│  │          Alarm Configuration Manager                  │  │
│  │     - Load from alarms.json                          │  │
│  │     - Schedule via APScheduler                       │  │
│  │     - Manage device list                             │  │
│  └───────────────────────┬──────────────────────────────┘  │
│                          │                                   │
└──────────────────────────┼───────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│              Alarm Playback Engine                            │
│                                                              │
│  ┌────────────────────────────────────────────────────┐    │
│  │  T-60s Timeline Orchestrator                       │    │
│  │  1. mDNS Discovery      → Find device IP           │    │
│  │  2. getInfo Phase       → Wake device              │    │
│  │  3. addUser Phase       → Authenticate             │    │
│  │  4. Cloud Polling       → Wait for device API      │    │
│  │  5. Play Phase          → Start playback           │    │
│  └────────────────────────────────────────────────────┘    │
│                                                              │
│  ┌────────────────────────────────────────────────────┐    │
│  │  Fallback System                                    │    │
│  │  - Quick check for existing device                  │    │
│  │  - Generic IP wake-up                               │    │
│  │  - mDNS auth registration                           │    │
│  │  - Force connection via Spotify                     │    │
│  └────────────────────────────────────────────────────┘    │
│                                                              │
│  ┌────────────────────────────────────────────────────┐    │
│  │  Circuit Breaker                                    │    │
│  │  - Track failures per device                        │    │
│  │  - Skip primary path after 3 failures               │    │
│  │  - Auto-recovery after 5 minutes                    │    │
│  └────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────┐
│                     External Services                         │
│                                                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │   mDNS/     │  │   Spotify   │  │  Devices   │        │
│  │  Zeroconf   │  │   Web API   │  │   Devices   │        │
│  │             │  │             │  │             │        │
│  │ - Discovery │  │ - Auth      │  │ - Streaming │        │
│  │ - Wake-up   │  │ - Devices   │  │ - Fallback  │        │
│  │ - Auth      │  │ - Playback  │  │             │        │
│  └─────────────┘  └─────────────┘  └─────────────┘        │
└──────────────────────────────────────────────────────────────┘
```

---

## Core Components

### 1. Main Application (`app/main.py`)

**Responsibilities:**
- FastAPI application setup
- Alarm CRUD operations
- Device discovery via cache
- APScheduler integration
- UI rendering

**Key Endpoints:**
```python
@app.get("/")                        # Home page with alarm list
@app.post("/set_alarm")              # Create/update alarm
@app.delete("/delete_alarm/{id}")    # Delete alarm
@app.post("/play_alarm_now/{id}")    # Immediate playback
@app.get("/api/devices")             # Device discovery
@app.get("/test/speakers")           # Speaker test page
```

### Application Flow Audit (2025-11-08)

- **Startup Lifespan:** `lifespan()` loads JSON data, instantiates `AlarmPlaybackConfig`, boots APScheduler (America/New_York), schedules alarms, and spawns two daemon threads (`background_device_registration`, `background_cache_refresh`). Both threads run while the global `running` flag is true.
- **Data Persistence:** Alarms and device profiles live in `data/alarms.json` and `data/devices.json`. CRUD endpoints modify in-memory lists, then `save_data()` flushes JSON synchronously before re-scheduling. Device profile mutations in `run_alarm()` persist immediately to avoid data loss on crash.
- **Scheduling Path:** `schedule_alarms()` maps comma-separated `dow` strings to cron expressions and creates three jobs per alarm (prewarm, run, optional stop). Misfires run immediately by design (`misfire_grace_time=None`) to avoid skipped wakeups after brief downtime.
- **Background Duties:** Registration thread polls Spotify Web API every 60s to ensure configured targets appear; it triggers `_mdns_auth_user_registration()` when missing. Cache refresh thread (300s loop) primes device cache via Zeroconf discovery, `check_device_health()`, and Spotify Web API, reusing a friendly-name cache (TTL 900s).
- **Blocking Calls & Risks:** Startup and background loops rely on synchronous I/O (filesystem, HTTP via Spotipy, Zeroconf, requests). Long network timeouts inside background threads can stall cache freshness but do not block the async event loop. Zeroconf discovery and Spotipy calls in `/` and `/api/devices` routes offload to executors when cache miss occurs, but repeated cache busts can still add ~2–3s latency.
- **External Dependencies:** Spotify OAuth via `spotipy.SpotifyOAuth`, Spotify Web API (devices, playback, playlists), Zeroconf/mDNS discovery, HTTP `getInfo` on target devices, and APScheduler.
- **Configuration Coupling:** `AlarmSystemConfig` bridges environment variables into `AlarmPlaybackConfig`. DeviceRegistry feeds friendly names and profiles using the same playback library types, so schema drift between `app` and `playback` modules must stay synchronized.
- **Global State:** Module-level globals (`alarms`, `devices`, `spotify`, caches) simplify access but require careful synchronization. All mutations happen on the main thread or serialized operations; background threads only read or swap entire dicts, which is safe under CPython GIL but should avoid partial writes.

### 2. Alarm Playback Engine (`playback/alarm_playback/orchestrator.py`)

**Responsibilities:**
- Timeline orchestration (T-60s to T+2s)
- Device discovery and authentication
- Playback control via Spotify Web API
- Fallback activation
- Circuit breaker management

**Timeline Phases:**
1. **Discovery** (T-60s): mDNS discovery for device IP/port/cpath
2. **GetInfo** (T-30s): Wake device via getInfo endpoint
3. **AddUser** (T-10s): Authenticate device via addUser endpoint
4. **Cloud Polling** (T-10s to T-0): Poll Spotify API for device
5. **Play** (T-0): Start playback with 404 retry
6. **Failover** (T+2s): Activate fallback if primary fails

### 3. Device Discovery (`playback/alarm_playback/discovery.py`)

**Responsibilities:**
- mDNS/Bonjour discovery for Spotify Connect devices
- Service listener for `_spotify-connect._tcp.local`
- Device IP, port, and cpath extraction
- Generic device name extraction

**Discovery Process:**
1. Service browser listens for `_spotify-connect._tcp.local`
2. Extract IP, port, cpath from TXT records
3. Get friendly name from getInfo endpoint (or fallback to instance name)
4. Health check via HTTP
5. Cache result for 2 minutes

---

## API Reference

### Web Pages

#### `GET /`
**Purpose:** Home page with alarm management UI  
**Response:** Rendered HTML with alarm list, playlist selector, device selector

#### `GET /test/speakers`
**Purpose:** Speaker test page showing all discovered devices  
**Response:** Rendered HTML with device status, IP, last seen, response time

### Alarm Management

#### `POST /set_alarm`
**Purpose:** Create or update an alarm  

**Body Parameters:**
- `playlist_uri` (str): Spotify playlist URI
- `device_name` (str): Target device name
- `hour` (int): Alarm hour (0-23)
- `minute` (int): Alarm minute (0-59)
- `dow` (str): Days of week (comma-separated, e.g., "mon,tue,wed")
- `volume` (int): Volume level (0-100)
- `shuffle` (bool): Shuffle mode
- `stop_hour` (int, optional): Stop hour
- `stop_minute` (int, optional): Stop minute

**Response:** Redirect to home page

#### `DELETE /delete_alarm/{alarm_id}`
**Purpose:** Delete an alarm  
**Response:** Redirect to home page

#### `POST /play_alarm_now/{alarm_id}`
**Purpose:** Play alarm immediately (test mode)  
**Response:** JSON status

### Device Management

#### `GET /api/devices`
**Purpose:** Get all discovered devices  
**Response:** JSON with device list (cached for 2 minutes)

**Response Example:**
```json
{
  "total_devices": 3,
  "online_devices": 2,
  "offline_devices": 1,
  "devices": [
    {
      "name": "Living Room Speaker",
      "ip": "192.168.1.100",
      "port": 80,
      "cpath": "/spotifyconnect/zeroconf",
      "is_online": true,
      "last_seen": 1703808000.0,
      "response_time_ms": 45.2,
      "error": null
    }
  ]
}
```

---

## Playback Logic

### Timeline Execution (T-60s to T+2s)

```
T-60s: Pre-warm (if enabled)
  ├─ mDNS Discovery → Find device IP/port/cpath
  └─ Cache results for T-0 use

T-30s: getInfo Phase
  └─ Call getInfo endpoint to wake device

T-10s: addUser Phase
  └─ Authenticate device via addUser endpoint
  └─ Token refresh before and after addUser
  └─ Wait ALARM_ADDUSER_WAIT_S (default: 5s) after completion

T-10s to T-0: Cloud Polling
  ├─ Poll Spotify Web API (intervals: ALARM_POLL_SLEEP_FAST_S/SLOW_S)
  ├─ Check if device appears in /me/player/devices
  ├─ Extended deadline if addUser succeeded (+ALARM_POLL_DEADLINE_EXTENSION_S)
  ├─ Token refresh periodically during polling
  └─ Wait up to ALARM_POLL_DEADLINE_S (default: 20s) total

T-0: Play Phase
  ├─ Transfer playback to device
  ├─ Set volume
  ├─ Start playlist
  ├─ Retry on 404 errors
  └─ Confirmation loop (verify device is actively playing)

T+2s: Failover Check
  └─ If playback failed → Activate fallback
```

**Fast Path Optimization:**
When a device is already available in Spotify Web API (common after first use), Wakeify uses an optimized fast path:
- **Skips:** mDNS discovery, getInfo, addUser, and cloud polling
- **Skips:** Debounce delay (device already active)
- **Includes:** Immediate playback with confirmation loop
- **Result:** ~1.6-1.7 seconds total execution time

**Configurable Timeouts:**
All timing values are configurable via environment variables (see [Configuration](#configuration) section). This allows fine-tuning for different network conditions and device types.

**Enhanced Authentication:**
- Token refresh before and after `addUser` to ensure valid credentials
- Extended polling deadline (+15s default) if `addUser` succeeds
- `getInfo` call after `addUser` to extract additional device names for better matching
- Periodic token refresh during cloud polling phase

### Fallback Sequence

When primary playback fails, Wakeify tries multiple fallback methods:

1. **Quick Check** - Check if device already in Spotify devices
2. **Generic IP Wake-up** - HTTP requests, mDNS queries, pings
3. **Generic mDNS Wake-up** - Additional mDNS queries and ping
4. **mDNS Auth** - Call `addUser` with access_token
5. **Force Connection** - Force transfer playback via Spotify API
6. **Final Failure** - Log error with helpful instructions

**No Device Switching Policy:** Wakeify only attempts to wake your selected device, never switches to alternatives.

### Circuit Breaker Pattern

```python
class CircuitBreakerState:
    def record_failure(self):
        """Record failure and open circuit after 3 failures"""
        self.failure_count += 1
        if self.failure_count >= 3:
            self.is_open = True
    
    def should_bypass_primary(self) -> bool:
        """Check if should skip primary path"""
        if self.is_open:
            # Auto-recover after 5 minutes
            if time.time() - self.last_failure_time > 300:
                self.is_open = False
                return False
            return True
        return False
```

---

## Device Discovery

### mDNS Discovery Process

1. **Service Browser** listens for `_spotify-connect._tcp.local`
2. **Service Found** → Extract IP, port, cpath from TXT records
3. **Name Extraction** → Get friendly name from getInfo endpoint
4. **Health Check** → Test device responsiveness via HTTP
5. **Cache Result** → Store for 2 minutes

### Device Name Extraction

Priority order for device names:
1. getInfo endpoint `remoteName` field (device's own friendly name)
2. getInfo endpoint `displayName` field
3. TXT records `CN`, `Name`, `DisplayName`, or `FriendlyName` fields
4. Cleaned instance name (remove technical suffixes)
5. Raw instance name

This ensures Wakeify works with any Spotify Connect device.

---

## Configuration

### Environment Variables

See `.env.example` for all available options. Minimum required:

- `SPOTIFY_CLIENT_ID`: Your Spotify app client ID
- `SPOTIFY_CLIENT_SECRET`: Your Spotify app client secret
- `SPOTIFY_REDIRECT_URI`: Your OAuth redirect URI
- `APP_SECRET`: Secure random string for sessions

Optional settings control:
- Default alarm preferences
- Timeline timings
- Fallback behavior
- Circuit breaker thresholds
- Logging level

### Alarm Playback Timeouts

All alarm playback timeouts are configurable via environment variables. These allow fine-tuning for different network conditions and device types:

**Polling and Deadlines:**
- `ALARM_POLL_DEADLINE_S` (default: 20): Total time to poll for device in Spotify API (seconds)
- `ALARM_POLL_DEADLINE_EXTENSION_S` (default: 15): Additional polling time after successful addUser (seconds)
- `ALARM_POLL_SLEEP_FAST_S` (default: 0.5): Sleep interval during fast polling (seconds)
- `ALARM_POLL_SLEEP_SLOW_S` (default: 1.0): Sleep interval during slow polling (seconds)

**Device Communication:**
- `ALARM_MDNS_TIMEOUT_S` (default: 1.5): mDNS discovery timeout (seconds)
- `ALARM_GETINFO_TIMEOUT_S` (default: 1.5): getInfo request timeout (seconds)
- `ALARM_ADDUSER_TIMEOUT_S` (default: 2.5): addUser request timeout (seconds)
- `ALARM_DEVICE_INFO_TIMEOUT_S` (default: 2.0): getDeviceInfo request timeout (seconds)
- `ALARM_VERIFY_DEVICE_TIMEOUT_S` (default: 0.5): verifyDeviceReady timeout (seconds)

**Authentication and Confirmation:**
- `ALARM_ADDUSER_WAIT_S` (default: 5.0): Wait time after addUser before checking devices (seconds)
- `ALARM_CONFIRMATION_SLEEP_S` (default: 0.2): Sleep time in playback confirmation loop (seconds)

All timeout values can be adjusted in `docker-compose.yml` or `.env` file to optimize for your specific network and devices.

### Docker Compose Configuration

Wakeify requires macvlan networking for mDNS discovery:

```yaml
networks:
  macvlan:
    external: true
```

Create the macvlan network:
```bash
docker network create -d macvlan \
  --subnet=192.168.1.0/24 \
  --gateway=192.168.1.1 \
  -o parent=eth0 \
  macvlan
```

---

## Troubleshooting

### "No devices found" in dropdown

**Solution:**
1. Visit `/test/speakers` to populate cache
2. Check Docker logs: `docker-compose logs -f wakeify`
3. Verify macvlan network is configured
4. Ensure `NET_BROADCAST` capability is enabled

### "Alarm not playing on device"

**Solution:**
1. Open Spotify app on your phone/computer
2. Manually select device and play a song
3. This authenticates the device with Spotify
4. Retry alarm

### "Device discovery slow"

This is expected:
- First load: 2-3 seconds (full mDNS scan)
- Subsequent loads: Instant (cached results)
- Cache refreshes every 2 minutes in background

### Container won't start

**Check:**
1. `.env` file exists and has all required variables
2. Macvlan network is created
3. Docker logs for specific errors
4. Port 443 is available (Wakeify uses HTTPS only)
5. SSL certificates generated in `ssl/` directory

### SSL certificate errors

- **Self-signed warning is normal** - this is expected behavior
- Click "Advanced" → "Proceed to site" in your browser
- SSL certificates auto-generated on first run using mkcert
- If certificate generation fails, check Docker logs for mkcert errors
- **Wakeify requires HTTPS** - HTTP access is not supported

---

## Security

**CRITICAL:** Never commit secrets to version control.

### HTTPS Only

**Wakeify requires HTTPS for all connections:**
- All web traffic uses SSL/TLS encryption
- Self-signed certificates auto-generated on first run
- Users must accept self-signed certificate warning in browser
- Production deployments should use proper CA-signed certificates
- HTTP access is **not supported** and will fail

### Required Secrets

1. **Spotify API Credentials**
   - `SPOTIFY_CLIENT_ID`: Your Spotify app client ID
   - `SPOTIFY_CLIENT_SECRET`: Your Spotify app client secret
   - Get from [Spotify Developer Dashboard](https://developer.spotify.com/dashboard)

2. **Application Secret**
   - `APP_SECRET`: Secure random string for session management
   - Generate with: `openssl rand -hex 32`

### Setup Instructions

1. Copy `.env.example`