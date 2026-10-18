# gopro-nas-sync

A small daemon for a Raspberry Pi that copies new photos and videos from your GoPro to a NAS.

An HTTP `POST` wakes the camera over Bluetooth. The Pi joins the camera's Wi-Fi and streams every file it hasn't
copied yet straight to the NAS over Ethernet. Nothing is buffered on the SD card. A web page shows the transfer
progress, pairs cameras, sets up the NAS share and manages the list of files already copied.

Built on the official [Open GoPro Python SDK](https://gopro.github.io/OpenGoPro/python_sdk/).

## Supported cameras

Every camera that speaks Open GoPro API 2.0:

- HERO9 Black (firmware 1.60 or later)
- HERO10 Black
- HERO11 Black and HERO11 Black Mini
- HERO12 Black
- HERO13 Black
- newer models as the SDK adds them

You can pair several cameras. A sync processes them one after another.

Older models (HERO8 and earlier, MAX) use a different, undocumented protocol and are not supported.

## Requirements

- A Raspberry Pi with built-in Wi-Fi and Bluetooth (Pi 3B+, 4, 5 or Zero 2 W with an Ethernet adapter), running
  **Raspberry Pi OS Bookworm or later** with NetworkManager. NetworkManager is the default on Bookworm.
- **Ethernet** connected to your LAN. The Wi-Fi chip is used only for the camera, so don't connect `wlan0` to your
  home Wi-Fi.
- A NAS reachable over Ethernet, with an SMB or NFS share. Alternatively, any directory you mount yourself.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/gopro-nas-sync/main/install.sh | sudo bash
```

Or from a checkout: `sudo ./install.sh`. The installer does the following:

- installs `bluez`, `network-manager`, `cifs-utils`, `nfs-common`, and [uv](https://docs.astral.sh/uv/) with a
  dedicated Python environment in `/opt/gopro-sync`
- creates the system user `gopro-sync`, which is allowed to use Bluetooth and to manage NetworkManager through a
  polkit rule
- installs a small root helper for mounting the NAS. sudoers lets the service run only that helper.
- asks for the port, an optional API token and the NAS settings, then starts `gopro-sync.service`

Running the installer again upgrades the install. Other commands:

- `sudo ./install.sh --reconfigure` runs the setup questions again.
- `sudo ./install.sh --uninstall [--purge]` removes the app. The files on your NAS are never touched.

## Usage

1. Open `http://<pi-address>:8080/`.
2. **NAS** tab: choose SMB, NFS or an existing directory. Click *Test connection*, then *Save and mount*. The app
   writes a systemd mount unit for `/mnt/gopronas` and checks that traffic to the NAS goes out through `eth0`.
3. **Cameras** tab: put the camera into pairing mode (on most models: *Preferences > Wireless Connections >
   Connect Device > GoPro Quik*), then click *Pair camera*.
4. Trigger a sync:

   ```bash
   curl -X POST http://<pi-address>:8080/api/sync
   # with a token:
   curl -X POST -H "Authorization: Bearer <token>" http://<pi-address>:8080/api/sync
   ```

   Or click *Sync now*, or run `gopro-sync sync` on the Pi.

Files are written to `<share>/<subdir>/<camera name>/<YYYY-MM-DD>/<file>`. You can change the layout in the NAS
tab. The camera keeps its files.

### Waking the camera

The camera can only be woken over Bluetooth while it's in standby: it must be paired, have wireless connections
turned on, and have been powered off recently. After several hours without a connection it stops advertising and
has to be switched on manually. A sync that can't find the camera fails with a clear error and can simply be
retried.

## How "already transferred" works

Each copied file is recorded with the camera serial number, the path on the camera and the camera's creation
timestamp. Including the timestamp matters because GoPro reuses names like `GX010001.MP4` after the SD card is
formatted, and those new files must still be copied.

A file is only recorded after it has been completely written and its size checked. The download goes to a `.part`
file that is renamed at the end, so interrupted transfers are retried on the next sync.

In the **History** tab you can remove single entries or reset the whole list (or one camera's list) so those files
are copied again.

## HTTP API

Requests that change something need `Authorization: Bearer <token>` when a token is configured.

| Method and path | Purpose |
| --- | --- |
| `POST /api/sync[?camera=<serial>]` | Start a sync (`202`, or `409` if one is already running) |
| `GET /api/status` | Current job and progress, plus the last result |
| `GET /api/events` | The same as a Server-Sent Events stream |
| `GET /api/cameras`, `POST /api/cameras/pair` | List cameras, or pair a camera that is in pairing mode |
| `PATCH /api/cameras/{serial}`, `DELETE /api/cameras/{serial}` | Rename, enable or disable, or forget a camera |
| `GET /api/nas`, `PUT /api/nas` | NAS settings and status; save, then mount |
| `POST /api/nas/test`, `POST /api/nas/mount`, `POST /api/nas/unmount` | Check the connection; mount or unmount |
| `GET /api/transferred?search=&camera=` | List of transferred files |
| `DELETE /api/transferred[?camera=]`, `DELETE /api/transferred/{id}` | Reset the list, or a single entry |

## Network design

- The camera's Wi-Fi gets its own NetworkManager profile (`gopro-sync-camera`) with `ipv4.never-default`, so the
  default route stays on `eth0` while the Pi is connected to the camera at `10.5.5.9`.
- Before waking a camera, the app checks:
  - that the NAS is mounted and writable,
  - that the route to the NAS goes through `eth0`,
  - and that there is enough free space.

  If any check fails, the camera is never woken.
- A NAS or LAN inside `10.5.5.0/24` (the camera's subnet) is refused.

## Development

```bash
uv sync
uv run pytest
uv run gopro-sync --config ./dev/config.toml serve --demo --state-dir ./dev/state --port 8099
```

`--demo` uses a simulated camera, so the web UI and the sync logic can be tried on any machine, including Windows.

Layout:

- `src/gopro_sync/cameras/`: the driver interface, the Open GoPro driver, the NetworkManager Wi-Fi controller and
  the per-model settings
- `src/gopro_sync/sync.py`: the sync worker
- `src/gopro_sync/nas.py`: NAS checks and mounting
- `src/gopro_sync/web.py` and `src/gopro_sync/static/`: the API and the web page
- `packaging/`: the systemd unit, the root mount helper, the polkit rule and the sudoers entry
- `install.sh`: the installer

Logs on the Pi: `journalctl -u gopro-sync -f`.
