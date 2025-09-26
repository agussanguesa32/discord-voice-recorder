## Discord Voice Recorder Bot (Pycord)

Record Discord voice channels on demand using slash commands. Starts with `/start channel_id:<ID>`, stops with `/stop channel_id:<ID>`. Saves a mixed `mixdown.mp3` (recommended) and can optionally keep per-user tracks or a ZIP.

### Highlights

- Slash commands via Pycord
- Robust voice recording using official Sinks
- Track alignment using monotonic timestamps + ffmpeg `adelay`
- MP3 output at configurable bitrate (default 64 kbps)
- Dockerized with ffmpeg, libopus, PyNaCl
- Host folder mapped to `${HOME}/discord_bot_recordings`
- Permissions-friendly (0777 dirs, 0666 files) via entrypoint

### Requirements

- Docker and Docker Compose
- A Discord bot token

### Setup

1) Create `.env` from `.env.example` and set at least:

```env
DISCORD_TOKEN=your_discord_bot_token
```

Optional environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | Log verbosity |
| `RECORDINGS_DIR` | `/app/recordings` | Container recordings directory |
| `MERGE_TRACKS` | `true` | Create `mixdown.mp3` |
| `SAVE_INDIVIDUAL` | `false` | Keep per-user files |
| `ZIP_RECORDINGS` | `false` | Create a ZIP bundle |
| `MP3_BITRATE` | `64k` | MP3 bitrate (e.g. `64k`, `96k`, `128k`) |

### Run with Docker Compose

By default, recordings are stored on the host at `${HOME}/discord_bot_recordings`.

```bash
docker compose up --build -d
```

If you prefer a custom host directory, set `RECORDINGS_HOST_DIR` when running:

```bash
RECORDINGS_HOST_DIR=~/my_recordings docker compose up --build -d
```

### Commands

- `/start channel_id:<ID>`: Connect to the voice channel (by ID) and start recording.
- `/stop channel_id:<ID>`: Stop recording in that channel and save files.

Notes:
- Only one active recording per guild.
- Output path: `<channel_name>/<timestamp>/`. Contents may include:
  - `mixdown.mp3` (if `MERGE_TRACKS=true`)
  - Per-user tracks (`.wav`) if `SAVE_INDIVIDUAL=true`
  - `recordings.zip` if `ZIP_RECORDINGS=true`

### Multiple instances (e.g., Dokploy)

`docker-compose.yml` supports per-instance variables:

- `CONTAINER_NAME` (default: `discord-voice-bot`)
- `ENV_FILE` (default: `.env`)
- `RECORDINGS_HOST_DIR` (default: `${HOME}/discord_bot_recordings`)

Run two instances with different variables (example):

```bash
CONTAINER_NAME=discord-voice-bot-1 ENV_FILE=.env.bot1 RECORDINGS_HOST_DIR=~/discord_bot_recordings/bot1 docker compose up -d
CONTAINER_NAME=discord-voice-bot-2 ENV_FILE=.env.bot2 RECORDINGS_HOST_DIR=~/discord_bot_recordings/bot2 docker compose up -d
```

In Dokploy, create two apps from the same repo and set different values for those variables and `DISCORD_TOKEN`.

### How it works (alignment)

- Uses a custom `AlignedWaveSink` to capture the first real audio frame per user using a monotonic clock.
- Calculates per-user offsets relative to the bot connection time.
- Mixes with ffmpeg using `adelay=<ms>` per track and a leading `anullsrc` to anchor t=0.
- Exports final `mixdown.mp3` at `MP3_BITRATE` (default `64k`).

### Permissions

- Entry point sets `umask 000` and ensures `/app/recordings` (and new files) are writable by everyone (0777 / 0666).
- If you already have restrictive files locally, fix once:

```bash
chmod -R a+rwX ${HOME}/discord_bot_recordings
```

### Local development (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m app.main
```

### MP3 quality and file size

Approximate per-hour sizes:
- 64 kbps: ~29 MB
- 96 kbps: ~43 MB
- 128 kbps: ~58 MB

Pycord Docs: [docs](https://docs.pycord.dev/en/master/index.html)


