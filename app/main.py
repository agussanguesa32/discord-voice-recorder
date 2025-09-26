import asyncio
import datetime
import logging
import os
import pathlib
import re
import zipfile
import subprocess
import time

import discord
from discord import Option
from discord import opus as discord_opus
from discord.sinks import WaveSink


# Optional .env loading when running outside Docker
try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
except Exception:
    pass


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("discord-voice-bot")


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
RECORDINGS_DIR = os.getenv("RECORDINGS_DIR", "/app/recordings").strip() or "/app/recordings"
MERGE_TRACKS = os.getenv("MERGE_TRACKS", "true").strip().lower() in ("1", "true", "yes", "y", "on")
ZIP_RECORDINGS = os.getenv("ZIP_RECORDINGS", "false").strip().lower() in ("1", "true", "yes", "y", "on")
SAVE_INDIVIDUAL = os.getenv("SAVE_INDIVIDUAL", "false").strip().lower() in ("1", "true", "yes", "y", "on")
MP3_BITRATE = os.getenv("MP3_BITRATE", "64k").strip() or "64k"


def ensure_directory(path: str) -> None:
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)


def sanitize_filename(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    return safe or "audio"


# Minimal intents (guilds for slash commands, voice_states for voice connection)
intents = discord.Intents.none()
intents.guilds = True
intents.voice_states = True

bot = discord.Bot(intents=intents)


# Active sessions per guild
# guild_id -> {"channel_id": int, "session_dir": str, "started_at": datetime}
active_sessions: dict[int, dict] = {}


class AlignedWaveSink(WaveSink):
    """Sink that records the first audio instant per user using a monotonic clock."""

    def __init__(self, session_started_mono: float) -> None:
        super().__init__()
        self.session_started_mono = session_started_mono
        self.user_first_mono: dict[int, float] = {}

    def write(self, data, user):  # type: ignore[override]
        try:
            user_id = getattr(user, "id", None)
            if user_id is None:
                user_id = int(user)
        except Exception:
            user_id = None

        if user_id is not None and user_id not in self.user_first_mono:
            self.user_first_mono[user_id] = time.monotonic()

        return super().write(data, user)


async def _save_recordings_zip(
    sink: WaveSink,
    session_dir: str,
    guild: discord.Guild | None,
    started_ts: float | None,
    ended_ts: float | None,
) -> tuple[str | None, str | None]:
    """Saves per-user tracks, performs mixdown and optionally zips.

    Returns (mix_path, zip_path), any of them may be None.

    Keys in sink.audio_data can be discord.User/Member or user_id (int).
    """
    ensure_directory(session_dir)
    try:
        os.chmod(session_dir, 0o777)
    except Exception:
        pass

    # Pycord exposes sink.audio_data as Dict[discord.User|int, AudioData]
    per_user_files: list[str] = []
    file_delay_ms: dict[str, int] = {}
    for user_key, audio_data in sink.audio_data.items():  # type: ignore[attr-defined]
        # Resolve user_id and display name
        if hasattr(user_key, "id"):
            user_id = getattr(user_key, "id")
            display = getattr(user_key, "display_name", None) or getattr(user_key, "name", None)
        else:
            # user_key is probably an int
            user_id = int(user_key)
            display = None
            member = guild.get_member(user_id) if guild else None
            if member:
                display = member.display_name or member.name
            if not display:
                user_obj = bot.get_user(user_id)
                if user_obj:
                    display = getattr(user_obj, "display_name", None) or user_obj.name

        username = sanitize_filename(display or f"user-{user_id}")
        filename = f"{username}_{user_id}.wav"
        file_path = os.path.join(session_dir, filename)
        with open(file_path, "wb") as f:
            f.write(audio_data.file.read())  # type: ignore[attr-defined]

        # Compute offset using the first received frame according to the custom sink
        delay_ms = 0
        try:
            resolved_user_id = user_id  # definido arriba
        except NameError:
            resolved_user_id = None

        if isinstance(sink, AlignedWaveSink) and resolved_user_id is not None:
            first_mono = sink.user_first_mono.get(resolved_user_id)
            if first_mono is not None:
                delay_ms = max(0, int((first_mono - sink.session_started_mono) * 1000))

        # skip empty files (users who didn't speak)
        if os.path.getsize(file_path) > 0:
            per_user_files.append(file_path)
            try:
                os.chmod(file_path, 0o666)
            except Exception:
                pass
            file_delay_ms[file_path] = delay_ms

    # Mix all tracks into a single file if enabled
    mix_path: str | None = None
    if MERGE_TRACKS and len(per_user_files) >= 1:
        mix_path = os.path.join(session_dir, "mixdown.mp3")
        try:
            inputs_with_offsets = [(fp, file_delay_ms.get(fp, 0)) for fp in per_user_files]
            session_duration = None
            if started_ts is not None and ended_ts is not None and ended_ts > started_ts:
                session_duration = ended_ts - started_ts
            _mix_mp3_files_with_offsets(inputs_with_offsets, mix_path, session_duration)
        except Exception as mix_err:  # pragma: no cover
            logger.warning(f"Failed to mix tracks: {mix_err}")
            mix_path = None
        else:
            try:
                os.chmod(mix_path, 0o666)
            except Exception:
                pass

    # Delete individual tracks if we don't want to keep them
    if not SAVE_INDIVIDUAL:
        for f in list(per_user_files):
            try:
                os.remove(f)
            except Exception:
                pass

    # Zip output if requested
    zip_path: str | None = None
    if ZIP_RECORDINGS:
        files_to_zip: list[str] = []
        if SAVE_INDIVIDUAL:
            files_to_zip.extend(per_user_files)
        if mix_path and os.path.exists(mix_path):
            files_to_zip.append(mix_path)
        if files_to_zip:
            zip_path = os.path.join(session_dir, "recordings.zip")
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for fpath in files_to_zip:
                    zf.write(fpath, arcname=os.path.basename(fpath))
            try:
                os.chmod(zip_path, 0o666)
            except Exception:
                pass

    return mix_path, zip_path


async def _on_recording_finished(sink: WaveSink, ctx: discord.ApplicationContext, session_dir: str) -> None:
    """Callback al finalizar la grabación: guarda archivos y desconecta."""
    try:
        # Obtener started_ts desde active_sessions si está disponible
        started_ts: float | None = None
        if ctx.guild and ctx.guild.id in active_sessions:
            val = active_sessions[ctx.guild.id].get("started_ts")
            if isinstance(val, (int, float)):
                started_ts = float(val)

        ended_ts = time.time()
        mix_path, zip_path = await _save_recordings_zip(sink, session_dir, ctx.guild, started_ts, ended_ts)
        # Desconectar del canal de voz
        try:
            await sink.vc.disconnect()  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover - desconexión best-effort
            pass

        # Aviso al usuario (ephemeral)
        try:
            msg = f"Grabación finalizada. Archivos guardados en: {session_dir}"
            if mix_path:
                msg += f"\nMix: {os.path.basename(mix_path)}"
            if zip_path:
                msg += f"\nZIP: {os.path.basename(zip_path)}"
            await ctx.followup.send(msg, ephemeral=True)
        except Exception:
            # Si no hubo defer previo, intentar un send simple
            try:
                await ctx.respond(
                    f"Grabación finalizada. Archivos guardados en: {session_dir}",
                    ephemeral=True,
                )
            except Exception:
                logger.warning("No se pudo notificar por mensaje el fin de la grabación.")
    finally:
        # Limpiar estado de sesión
        if ctx.guild and ctx.guild.id in active_sessions:
            active_sessions.pop(ctx.guild.id, None)


def _mix_mp3_files(input_files: list[str], output_file: str) -> None:
    """Mix multiple WAV/MP3 tracks and export to MP3 using ffmpeg (amix)."""
    if not input_files:
        return
    if len(input_files) == 1:
        import shutil

        shutil.copyfile(input_files[0], output_file)
        return

    cmd: list[str] = ["ffmpeg", "-y"]
    for f in input_files:
        cmd += ["-i", f]
    filter_complex = f"amix=inputs={len(input_files)}:duration=longest:normalize=1"
    cmd += [
        "-filter_complex",
        filter_complex,
        "-c:a",
        "libmp3lame",
        "-b:a",
        MP3_BITRATE,
        output_file,
    ]

    completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed with code {completed.returncode}: {completed.stderr.decode(errors='ignore')[:500]}"
        )


def _mix_mp3_files_with_offsets(
    inputs_with_offsets: list[tuple[str, int]], output_file: str, session_duration_s: float | None
) -> None:
    """
    Mix multiple WAV/MP3 applying per-track delay (ms) and export to MP3.

    Adds a silence source from the start to ensure the mix starts at t=0
    and can force the total session duration to avoid early cuts.
    """
    if not inputs_with_offsets:
        return
    if len(inputs_with_offsets) == 1:
        import shutil

        shutil.copyfile(inputs_with_offsets[0][0], output_file)
        return

    cmd: list[str] = ["ffmpeg", "-y"]
    for f, _d in inputs_with_offsets:
        cmd += ["-i", f]

    # If we know the session duration, generate anullsrc of that length
    # so the mix is at least that long and starts at zero.
    use_silence = session_duration_s is not None and session_duration_s > 0
    if use_silence:
        cmd += [
            "-f",
            "lavfi",
            "-t",
            f"{session_duration_s}",
            "-i",
            "anullsrc=r=48000:cl=stereo",
        ]

    # Build filter_complex with adelay for each input
    filter_parts: list[str] = []
    labels: list[str] = []
    for idx, (_f, dms) in enumerate(inputs_with_offsets):
        in_label = f"[{idx}:a]"
        out_label = f"[a{idx}]"
        labels.append(out_label)
        filter_parts.append(f"{in_label}adelay={max(0, int(dms))}:all=1{out_label}")

    if use_silence:
        # Extra silence input will be at the end (last index)
        silence_label = f"[{len(inputs_with_offsets)}:a]"
        labels.append(silence_label)

    amix = f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:normalize=1[aout]"
    filter_complex = ";".join(filter_parts + [amix])

    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        "[aout]",
        "-c:a",
        "libmp3lame",
        "-b:a",
        MP3_BITRATE,
        output_file,
    ]

    completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if completed.returncode != 0:
        raise RuntimeError(
            f"ffmpeg fallo con código {completed.returncode}: {completed.stderr.decode(errors='ignore')[:500]}"
        )


@bot.slash_command(name="start", description="Connect to a voice channel by ID and start recording (MP3)")
async def start_command(
    ctx: discord.ApplicationContext,
    channel_id: Option(str, "Voice channel ID (numeric)")
):
    await ctx.defer(ephemeral=True)

    # Basic validations
    try:
        target_channel_id = int(channel_id)
    except ValueError:
        await ctx.followup.send("Channel ID must be numeric.", ephemeral=True)
        return

    # Avoid duplicate sessions per guild
    if ctx.guild and ctx.guild.id in active_sessions:
        data = active_sessions[ctx.guild.id]
        await ctx.followup.send(
            f"There is already a recording in progress on channel ID {data['channel_id']}.",
            ephemeral=True,
        )
        return

    # Fetch the channel and validate it is a voice channel
    try:
        channel = await bot.fetch_channel(target_channel_id)
    except discord.HTTPException:
        channel = None

    if not isinstance(channel, discord.VoiceChannel):
        await ctx.followup.send("No voice channel found with that ID.", ephemeral=True)
        return

    # Connect
    try:
        voice_client = await channel.connect()
    except discord.ClientException as e:
        await ctx.followup.send(f"Could not connect: {e}", ephemeral=True)
        return

    # Prepare session and sink
    started_at = datetime.datetime.utcnow().replace(microsecond=0).isoformat().replace(":", "-")
    started_ts = time.time()
    session_dir = os.path.join(
        RECORDINGS_DIR,
        sanitize_filename(channel.name),
        sanitize_filename(started_at),
    )
    ensure_directory(session_dir)

    started_ts = time.time()
    started_mono = time.monotonic()
    sink = AlignedWaveSink(session_started_mono=started_mono)
    # Start recording; callback receives sink and extra args
    voice_client.start_recording(sink, _on_recording_finished, ctx, session_dir)  # type: ignore[call-arg]

    if ctx.guild:
        active_sessions[ctx.guild.id] = {
            "channel_id": channel.id,
            "session_dir": session_dir,
            "started_at": started_at,
            "started_ts": started_ts,
            "started_mono": started_mono,
        }

    await ctx.followup.send(
        f"Recording in: {channel.name} (ID {channel.id}). Use /stop with the same ID to stop.",
        ephemeral=True,
    )


@bot.slash_command(name="stop", description="Stop recording and save files")
async def stop_command(
    ctx: discord.ApplicationContext,
    channel_id: Option(str, "Voice channel ID (numeric)")
):
    await ctx.defer(ephemeral=True)

    try:
        target_channel_id = int(channel_id)
    except ValueError:
        await ctx.followup.send("Channel ID must be numeric.", ephemeral=True)
        return

    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if not voice_client or not voice_client.is_connected():
        await ctx.followup.send("I'm not connected to any voice channel in this server.", ephemeral=True)
        return

    if not voice_client.channel or voice_client.channel.id != target_channel_id:
        await ctx.followup.send(
            "The bot is not recording in that voice channel.",
            ephemeral=True,
        )
        return

    try:
        voice_client.stop_recording()
        await ctx.followup.send("Stopping recording. Saving files...", ephemeral=True)
    except Exception as e:
        await ctx.followup.send(f"Could not stop recording: {e}", ephemeral=True)


def _validate_env() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError("Missing DISCORD_TOKEN environment variable.")
    ensure_directory(RECORDINGS_DIR)


if __name__ == "__main__":
    _validate_env()
    # Explicitly load Opus in Docker environments
    try:
        if not discord_opus.is_loaded():
            discord_opus.load_opus("libopus.so.0")
            logger.info("Opus loaded: libopus.so.0")
    except Exception as e:
        logger.warning(f"Could not load Opus: {e}")
    logger.info("Starting voice bot with Pycord...")
    bot.run(DISCORD_TOKEN)


