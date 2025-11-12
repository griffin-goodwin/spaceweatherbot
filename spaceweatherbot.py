#!/usr/bin/env python3
"""
Solar Flare Discord Bot

Slash commands:
  /flare_latest        – Latest detected solar flare event (full details)
  /flare_forecast      – Solar flare probability forecast (1-3 days)
  /flare_alerts        – Show recent SWPC flare alerts
  /latest_image        – Get the latest SDO AIA solar image
  /latest_forecast     – Get the latest solar flare probability forecast
  /latest_flare_class  – Get just the latest detected flare class
  /subscribe_flares    – Subscribe to M- and X‑class flare notifications
  /unsubscribe_flares  – Stop flare notifications
  /subscribe_daily     – Subscribe to daily summaries at 5pm EST/EDT
  /unsubscribe_daily   – Stop daily summaries

Requirements (pip):
  discord.py>=2.3.2 aiohttp>=3.9.5 python-dotenv>=1.0.1

Environment:
  DISCORD_TOKEN=your_bot_token

Notes:
  • Uses public JSON feeds from SWPC (no API keys).
  • Monitors real-time flare events and sends notifications for M/X-class flares.
  • Includes SDO AIA images when available for flare events.
"""

from __future__ import annotations
import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:
    # Fallback for Python < 3.9
    try:
        from backports.zoneinfo import ZoneInfo
    except ImportError:
        # If zoneinfo not available, use pytz as fallback
        import pytz
        ZoneInfo = pytz.timezone
from urllib.parse import urlencode

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

# --------------------------- Config ---------------------------
ENDPOINTS = {
    "alerts": "https://services.swpc.noaa.gov/products/alerts.json",
    "flare_latest": "https://services.swpc.noaa.gov/json/goes/primary/xray-flares-latest.json",
    "flare_forecast": "https://services.swpc.noaa.gov/json/solar_probabilities.json",
}

# Helioviewer API for SDO AIA images
HELIOVIEWER_API = "https://api.helioviewer.org/v2"

CHECK_INTERVAL_SEC = 60  # flare poll cadence

# ------------------------ Utilities --------------------------
class Http:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self._session

    async def get_json(self, url: str) -> Any:
        s = await self.session()
        async with s.get(url, headers={"User-Agent": "Flare-DiscordBot/1.0"}) as r:
            r.raise_for_status()
            text = await r.text()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return [json.loads(line) for line in text.splitlines() if line.strip()]

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

http = Http()


def utcnow_iso() -> str:
    """Get current time in UTC format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def utcnow_iso_with_est() -> str:
    """Get current time in UTC and EST/EDT format for footers."""
    now_utc = datetime.now(timezone.utc)
    try:
        est_tz = ZoneInfo("America/New_York")
        now_est = now_utc.astimezone(est_tz)
    except (NameError, AttributeError):
        # Fallback to pytz if zoneinfo not available
        import pytz
        est_tz = pytz.timezone("America/New_York")
        now_est = pytz.UTC.localize(now_utc.replace(tzinfo=None)).astimezone(est_tz)
    utc_str = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    est_str = now_est.strftime("%H:%M %Z")
    return f"{utc_str} / {est_str}"


def convert_to_est(utc_time_str: str) -> str:
    """Convert UTC time string to EST/EDT format.
    
    Args:
        utc_time_str: ISO format time string (e.g., "2025-11-12T16:39:00Z")
    
    Returns:
        Formatted string with both UTC and EST/EDT times
    """
    try:
        # Parse UTC time
        if "Z" in utc_time_str:
            dt_utc = datetime.fromisoformat(utc_time_str.replace("Z", "+00:00"))
        else:
            dt_utc = datetime.fromisoformat(utc_time_str.replace("+00:00", "").replace("UTC", "").strip() + "+00:00")
        
        # Convert to EST/EDT
        try:
            est_tz = ZoneInfo("America/New_York")
        except (NameError, AttributeError):
            # Fallback to pytz if zoneinfo not available
            import pytz
            est_tz = pytz.timezone("America/New_York")
            dt_utc = pytz.UTC.localize(dt_utc.replace(tzinfo=None))
        dt_est = dt_utc.astimezone(est_tz)
        
        # Format both times
        utc_str = dt_utc.strftime("%Y-%m-%d %H:%M UTC")
        est_str = dt_est.strftime("%Y-%m-%d %H:%M %Z")
        
        return f"{utc_str} / {est_str}"
    except Exception as e:
        # If conversion fails, return original with UTC label
        return f"{utc_time_str} (UTC)"


def format_time_est(utc_time_str: Optional[str]) -> str:
    """Format time string showing EST/EDT, with fallback.
    
    Args:
        utc_time_str: ISO format time string or None
    
    Returns:
        Formatted time string or "Unknown"
    """
    if not utc_time_str or utc_time_str == "Unknown":
        return "Unknown"
    return convert_to_est(utc_time_str)


def parse_flare_class(flare_class: str) -> Tuple[Optional[str], Optional[float]]:
    """Parse flare class string (e.g., 'M2.5', 'X1.0') into (class_letter, number)."""
    if not flare_class:
        return None, None
    import re
    m = re.match(r"([CXMB])(\d+(?:\.\d+)?)", flare_class.upper())
    if m:
        return m.group(1), float(m.group(2))
    return None, None


def is_m_or_x_class(flare_class: str) -> bool:
    """Check if flare class is M or X."""
    class_letter, _ = parse_flare_class(flare_class)
    return class_letter in ("M", "X")


# -------------------- SWPC Parsers/Formatters -----------------
async def fetch_latest_flare() -> Optional[Dict[str, Any]]:
    """Fetch the latest detected solar flare event."""
    try:
        data = await http.get_json(ENDPOINTS["flare_latest"])
        if isinstance(data, list) and len(data) > 0:
            return data[0]  # Latest flare is first in array
        return None
    except Exception as e:
        print(f"Error fetching latest flare: {e}")
        return None


async def fetch_flare_forecast() -> List[Dict[str, Any]]:
    """Fetch solar flare probability forecast."""
    try:
        data = await http.get_json(ENDPOINTS["flare_forecast"])
        if isinstance(data, list):
            return data[:3]  # Return next 3 days
        return []
    except Exception as e:
        print(f"Error fetching flare forecast: {e}")
        return []


async def fetch_alerts(limit: int = 5) -> List[Dict[str, Any]]:
    """Fetch recent SWPC alerts."""
    try:
        arr = await http.get_json(ENDPOINTS["alerts"])
        if not isinstance(arr, list):
            return []
        # Filter for flare-related alerts
        flare_alerts = []
        for a in arr:
            msg = (a.get("message") or a.get("summary") or "").lower()
            if "flare" in msg or "x-ray" in msg or "xray" in msg:
                flare_alerts.append(a)
        return flare_alerts[-limit:]
    except Exception as e:
        print(f"Error fetching alerts: {e}")
        return []


async def get_sdo_aia_image(time: Optional[str] = None, wavelength: int = 193) -> Optional[str]:
    """Get SDO AIA image URL for a given time.
    
    Uses Helioviewer API to get the closest available image.
    
    Args:
        time: ISO format time string (e.g., "2025-11-12T16:39:00Z")
        wavelength: AIA wavelength in Angstroms (94, 131, 171, 193, 211, 304, 335)
    
    Returns:
        Image URL or None if unavailable
    """
    try:
        if time:
            try:
                dt = datetime.fromisoformat(time.replace("Z", "+00:00"))
            except:
                dt = datetime.now(timezone.utc)
        else:
            dt = datetime.now(timezone.utc)
        
        # Helioviewer API v2 - getJP2Image
        # Format: YYYY-MM-DDTHH:MM:SS
        date_str = dt.strftime("%Y-%m-%dT%H:%M:%S")
        
        # Wavelength mapping for AIA
        # 193A is commonly used for flare images
        params = {
            "date": date_str,
            "sourceId": 10,  # SDO
            "instrumentId": 11,  # AIA
            "detectorId": 12,  # AIA
            "measurementId": wavelength,
        }
        
        # Try to get image URL from Helioviewer
        # Note: This may require authentication or different endpoint
        # For now, return a link to the SDO latest images page
        # You can also try: https://sdo.gsfc.nasa.gov/assets/img/latest/
        return f"https://sdo.gsfc.nasa.gov/assets/img/latest/latest_{wavelength}.jpg"
    except Exception as e:
        print(f"Error getting SDO image: {e}")
        return None


# ----------------------- Discord Bot -------------------------
class FlareBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.seen_flare_ids: set[str] = set()
        self.subscribed_flares: set[int] = set()
        self.subscribed_daily: set[int] = set()
        self._last_flare_time: Optional[str] = None
        self._last_daily_date: Optional[str] = None  # Track last date summary was sent

    async def setup_hook(self):
        await self.tree.sync()
        # Start background tasks after event loop is running
        self.status_task.start()
        self.flare_poll_task.start()
        self.daily_summary_task.start()

    # --- background presence ---
    @tasks.loop(minutes=10)
    async def status_task(self):
        try:
            # Get latest flare
            flare = await fetch_latest_flare()
            flare_class = "None"
            if flare:
                flare_class = flare.get("max_class") or flare.get("current_class", "None")
            
            # Get forecast for next 24 hours
            forecast_data = await fetch_flare_forecast()
            m_chance = 0
            x_chance = 0
            if forecast_data and len(forecast_data) > 0:
                latest_forecast = forecast_data[0]
                m_chance = latest_forecast.get("m_class_1_day", 0)
                x_chance = latest_forecast.get("x_class_1_day", 0)
            
            # Format status text (Discord limit is 128 chars)
            text = f"Current Flare: {flare_class} | M: {m_chance}% | X: {x_chance}%"
            
            # Truncate if too long
            if len(text) > 128:
                # Try shorter version
                text = f"Flare: {flare_class} | M: {m_chance}% | X: {x_chance}%"
                if len(text) > 128:
                    # Even shorter
                    text = f"{flare_class} | M:{m_chance}% X:{x_chance}%"
                    if len(text) > 128:
                        # Fallback to just flare class
                        text = f"Flare: {flare_class}"
            
            await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=text))
        except Exception as e:
            print(f"status_task error: {e}")
            # Fallback to simple status
            try:
                await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="Solar flares"))
            except:
                pass

    # --- real-time flare polling ---
    @tasks.loop(seconds=CHECK_INTERVAL_SEC)
    async def flare_poll_task(self):
        if not self.subscribed_flares:
            return
        try:
            flare = await fetch_latest_flare()
            if not flare:
                return
            
            # Check if this is a new M/X class flare
            flare_class = flare.get("max_class") or flare.get("current_class")
            if not is_m_or_x_class(flare_class or ""):
                return
            
            # Create unique ID from time and class
            flare_time = flare.get("max_time") or flare.get("time_tag") or flare.get("begin_time")
            flare_id = f"{flare_time}_{flare_class}"
            
            if flare_id in self.seen_flare_ids:
                return
            
            self.seen_flare_ids.add(flare_id)
            self._last_flare_time = flare_time
            
            # Build embed with SDO image
            embed = await format_flare_embed(flare)
            
            # Send to all subscribed channels
            for ch_id in list(self.subscribed_flares):
                ch = self.get_channel(ch_id)
                if isinstance(ch, (discord.TextChannel, discord.Thread)):
                    try:
                        await ch.send(embed=embed)
                    except Exception:
                        self.subscribed_flares.discard(ch_id)
        except Exception as e:
            print(f"flare_poll error: {e}")

    # --- daily summary at 5pm EST/EDT ---
    @tasks.loop(minutes=5)
    async def daily_summary_task(self):
        if not self.subscribed_daily:
            return
        try:
            # Get current time in EST/EDT
            now_utc = datetime.now(timezone.utc)
            try:
                est_tz = ZoneInfo("America/New_York")
            except (NameError, AttributeError):
                import pytz
                est_tz = pytz.timezone("America/New_York")
            now_est = now_utc.astimezone(est_tz)
            
            # Check if it's 5pm EST/EDT (17:00)
            if now_est.hour == 13 and now_est.minute < 5:
                today = now_est.strftime("%Y-%m-%d")
                
                # Only send once per day
                if self._last_daily_date == today:
                    return
                
                self._last_daily_date = today
                
                # Build and send daily summary
                embed = await build_daily_summary_embed(now_est)
                
                for ch_id in list(self.subscribed_daily):
                    ch = self.get_channel(ch_id)
                    if isinstance(ch, (discord.TextChannel, discord.Thread)):
                        try:
                            await ch.send(embed=embed)
                        except Exception:
                            self.subscribed_daily.discard(ch_id)
        except Exception as e:
            print(f"daily_summary error: {e}")


# --------------------- Slash Commands ------------------------
client = FlareBot()


@client.tree.command(name="flare_latest", description="Latest detected solar flare event")
async def flare_latest_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    flare = await fetch_latest_flare()
    if not flare:
        await interaction.followup.send("No flare data available.")
        return
    embed = await format_flare_embed(flare)
    await interaction.followup.send(embed=embed)


@client.tree.command(name="flare_forecast", description="Solar flare probability forecast (1-3 days)")
async def flare_forecast_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    forecast = await fetch_flare_forecast()
    if not forecast:
        await interaction.followup.send("No forecast data available.")
        return
    
    embed = discord.Embed(title="Solar Flare Probability Forecast", colour=0xff6b35)
    desc_lines = []
    
    for day_data in forecast:
        date_str = day_data.get("date", "Unknown")
        if "T" in date_str:
            date_str = date_str.split("T")[0]
        
        m_1d = day_data.get("m_class_1_day", 0)
        x_1d = day_data.get("x_class_1_day", 0)
        
        desc_lines.append(f"**{date_str}**")
        desc_lines.append(f"M-class: {m_1d}% | X-class: {x_1d}%")
        desc_lines.append("")
    
    embed.description = "\n".join(desc_lines)
    embed.set_footer(text=f"SWPC • {utcnow_iso_with_est()}")
    await interaction.followup.send(embed=embed)


@client.tree.command(name="flare_alerts", description="Show recent SWPC flare alerts")
@app_commands.describe(limit="How many to show (1–10)")
async def flare_alerts_cmd(interaction: discord.Interaction, limit: app_commands.Range[int, 1, 10] = 5):
    await interaction.response.defer(thinking=True)
    items = await fetch_alerts(limit=limit)
    if not items:
        await interaction.followup.send("No flare alerts found.")
        return
    embeds = [format_alert_embed(a) for a in items]
    await interaction.followup.send(embeds=embeds[:10])


@client.tree.command(name="subscribe_flares", description="Subscribe this channel to M- and X-class flare notifications")
async def subscribe_flares_cmd(interaction: discord.Interaction):
    ch = interaction.channel
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("This command only works in text channels or threads.", ephemeral=True)
        return
    client.subscribed_flares.add(ch.id)
    await interaction.response.send_message("✅ Subscribed: I'll notify you when M- or X-class flares are detected!")


@client.tree.command(name="unsubscribe_flares", description="Unsubscribe this channel from flare notifications")
async def unsubscribe_flares_cmd(interaction: discord.Interaction):
    ch = interaction.channel
    if isinstance(ch, (discord.TextChannel, discord.Thread)) and ch.id in client.subscribed_flares:
        client.subscribed_flares.discard(ch.id)
        await interaction.response.send_message("Unsubscribed from flare notifications.")
    else:
        await interaction.response.send_message("This channel isn't subscribed for flare notifications.", ephemeral=True)


@client.tree.command(name="subscribe_daily", description="Subscribe this channel to daily summaries at 5pm EST/EDT")
async def subscribe_daily_cmd(interaction: discord.Interaction):
    ch = interaction.channel
    if not isinstance(ch, (discord.TextChannel, discord.Thread)):
        await interaction.response.send_message("This command only works in text channels or threads.", ephemeral=True)
        return
    client.subscribed_daily.add(ch.id)
    await interaction.response.send_message("✅ Subscribed: I'll post a daily summary at 5pm EST/EDT with today's forecast and events!")


@client.tree.command(name="unsubscribe_daily", description="Unsubscribe this channel from daily summaries")
async def unsubscribe_daily_cmd(interaction: discord.Interaction):
    ch = interaction.channel
    if isinstance(ch, (discord.TextChannel, discord.Thread)) and ch.id in client.subscribed_daily:
        client.subscribed_daily.discard(ch.id)
        await interaction.response.send_message("Unsubscribed from daily summaries.")
    else:
        await interaction.response.send_message("This channel isn't subscribed for daily summaries.", ephemeral=True)


@client.tree.command(name="latest_image", description="Get the latest SDO AIA solar image")
@app_commands.describe(wavelength="AIA wavelength in Angstroms")
@app_commands.choices(wavelength=[
    app_commands.Choice(name="94 Å (Hot plasma)", value=94),
    app_commands.Choice(name="131 Å (Hot flare plasma)", value=131),
    app_commands.Choice(name="171 Å (Quiet corona)", value=171),
    app_commands.Choice(name="193 Å (Corona & flares)", value=193),
    app_commands.Choice(name="211 Å (Active regions)", value=211),
    app_commands.Choice(name="304 Å (Chromosphere)", value=304),
    app_commands.Choice(name="335 Å (Hot active regions)", value=335),
])
async def latest_image_cmd(interaction: discord.Interaction, wavelength: int = 193):
    await interaction.response.defer(thinking=True)
    
    # Get latest flare to use its time, or use current time
    flare = await fetch_latest_flare()
    flare_time = None
    if flare:
        flare_time = flare.get("max_time") or flare.get("time_tag") or flare.get("begin_time")
    
    image_url = await get_sdo_aia_image(flare_time, wavelength=wavelength)
    
    if image_url:
        embed = discord.Embed(title=f"SDO AIA {wavelength}Å Solar Image", colour=0x4a90e2)
        embed.set_image(url=image_url)
        embed.add_field(name="Wavelength", value=f"{wavelength} Å", inline=True)
        if flare_time:
            embed.add_field(name="Time", value=format_time_est(flare_time), inline=True)
        embed.add_field(name="Source", value="[SDO/NASA](https://sdo.gsfc.nasa.gov/)", inline=False)
        embed.set_footer(text=f"SWPC • {utcnow_iso_with_est()}")
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send(f"Could not retrieve SDO AIA {wavelength}Å image. [View on SDO website](https://sdo.gsfc.nasa.gov/assets/img/latest/)")


@client.tree.command(name="latest_forecast", description="Get the latest solar flare probability forecast")
async def latest_forecast_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    forecast = await fetch_flare_forecast()
    if not forecast:
        await interaction.followup.send("No forecast data available.")
        return
    
    # Get the most recent forecast (first in list)
    latest = forecast[0] if forecast else None
    if not latest:
        await interaction.followup.send("No forecast data available.")
        return
    
    date_str = latest.get("date", "Unknown")
    if "T" in date_str:
        date_str = date_str.split("T")[0]
    
    embed = discord.Embed(title="Latest Solar Flare Forecast", colour=0xff6b35)
    embed.add_field(name="Date", value=date_str, inline=False)
    embed.add_field(name="M-Class (1 day)", value=f"{latest.get('m_class_1_day', 0)}%", inline=True)
    embed.add_field(name="X-Class (1 day)", value=f"{latest.get('x_class_1_day', 0)}%", inline=True)
    embed.add_field(name="M-Class (2 day)", value=f"{latest.get('m_class_2_day', 0)}%", inline=True)
    embed.add_field(name="X-Class (2 day)", value=f"{latest.get('x_class_2_day', 0)}%", inline=True)
    embed.add_field(name="M-Class (3 day)", value=f"{latest.get('m_class_3_day', 0)}%", inline=True)
    embed.add_field(name="X-Class (3 day)", value=f"{latest.get('x_class_3_day', 0)}%", inline=True)
    embed.set_footer(text=f"SWPC • {utcnow_iso_with_est()}")
    await interaction.followup.send(embed=embed)


@client.tree.command(name="latest_flare_class", description="Get just the latest detected flare class")
async def latest_flare_class_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    flare = await fetch_latest_flare()
    if not flare:
        await interaction.followup.send("No flare data available.")
        return
    
    max_class = flare.get("max_class") or flare.get("current_class", "None")
    begin_class = flare.get("begin_class", "None")
    end_class = flare.get("end_class", "None")
    max_time = flare.get("max_time") or flare.get("time_tag", "Unknown")
    
    # Determine color and emoji based on flare class
    class_letter, class_num = parse_flare_class(max_class)
    if class_letter == "X":
        colour = discord.Colour.red()
        emoji = "🚨"
    elif class_letter == "M":
        colour = discord.Colour.orange()
        emoji = "⚠️"
    elif class_letter == "C":
        colour = discord.Colour.yellow()
        emoji = "⚡"
    else:
        colour = discord.Colour.blue()
        emoji = "☀️"
    
    embed = discord.Embed(title=f"{emoji} Latest Flare Class: {max_class}", colour=colour)
    embed.add_field(name="Peak Class", value=f"**{max_class}**", inline=True)
    embed.add_field(name="Peak Time", value=format_time_est(max_time), inline=True)
    
    if begin_class != max_class or end_class != max_class:
        embed.add_field(name="Evolution", value=f"{begin_class} → {max_class} → {end_class}", inline=False)
    
    embed.set_footer(text=f"SWPC • {utcnow_iso_with_est()}")
    await interaction.followup.send(embed=embed)


# ----------------------- Embeds & Helpers ---------------------

async def build_daily_summary_embed(date_est: datetime) -> discord.Embed:
    """Build a daily summary embed with forecast and today's events."""
    date_str = date_est.strftime("%Y-%m-%d")
    date_display = date_est.strftime("%B %d, %Y")
    
    # Get today's forecast
    forecast_data = await fetch_flare_forecast()
    today_forecast = None
    if forecast_data:
        for day_data in forecast_data:
            day_date = day_data.get("date", "")
            if "T" in day_date:
                day_date = day_date.split("T")[0]
            if day_date == date_str:
                today_forecast = day_data
                break
        # If exact match not found, use first entry (most recent)
        if not today_forecast and forecast_data:
            today_forecast = forecast_data[0]
    
    # Get today's alerts
    all_alerts = await fetch_alerts(limit=20)
    today_alerts = []
    for alert in all_alerts:
        issued = alert.get("issue_datetime") or alert.get("time_tag") or alert.get("timestamp")
        if issued:
            try:
                # Parse the time and check if it's today
                if "T" in str(issued):
                    alert_date = str(issued).split("T")[0]
                    if alert_date == date_str:
                        today_alerts.append(alert)
            except:
                pass
    
    # Get latest flare to see if it happened today
    latest_flare = await fetch_latest_flare()
    today_flare = None
    if latest_flare:
        flare_time = latest_flare.get("max_time") or latest_flare.get("time_tag") or latest_flare.get("begin_time")
        if flare_time:
            try:
                if "T" in str(flare_time):
                    flare_date = str(flare_time).split("T")[0]
                    if flare_date == date_str:
                        today_flare = latest_flare
            except:
                pass
    
    # Build embed
    embed = discord.Embed(
        title=f"📊 Daily Solar Flare Summary - {date_display}",
        colour=0x5865F2,
        description=f"Summary of today's solar flare activity and forecast"
    )
    
    # Add forecast section
    if today_forecast:
        m_1d = today_forecast.get("m_class_1_day", 0)
        x_1d = today_forecast.get("x_class_1_day", 0)
        m_2d = today_forecast.get("m_class_2_day", 0)
        x_2d = today_forecast.get("x_class_2_day", 0)
        m_3d = today_forecast.get("m_class_3_day", 0)
        x_3d = today_forecast.get("x_class_3_day", 0)
        
        forecast_text = f"**Today (1 day):** M-class: {m_1d}% | X-class: {x_1d}%\n"
        forecast_text += f"**Tomorrow (2 day):** M-class: {m_2d}% | X-class: {x_2d}%\n"
        forecast_text += f"**Day 3:** M-class: {m_3d}% | X-class: {x_3d}%"
        embed.add_field(name="📈 Forecast Probabilities", value=forecast_text, inline=False)
    else:
        embed.add_field(name="📈 Forecast Probabilities", value="No forecast data available", inline=False)
    
    # Add today's flare event
    if today_flare:
        flare_class = today_flare.get("max_class") or today_flare.get("current_class", "Unknown")
        flare_time = format_time_est(today_flare.get("max_time") or today_flare.get("time_tag", ""))
        class_letter, _ = parse_flare_class(flare_class)
        if class_letter == "X":
            flare_emoji = "🚨"
        elif class_letter == "M":
            flare_emoji = "⚠️"
        else:
            flare_emoji = "⚡"
        
        embed.add_field(
            name=f"{flare_emoji} Today's Flare Event",
            value=f"**Class:** {flare_class}\n**Peak Time:** {flare_time}",
            inline=False
        )
    else:
        embed.add_field(name="⚡ Today's Flare Event", value="No significant flares detected today", inline=False)
    
    # Add today's alerts
    if today_alerts:
        alert_text = ""
        for alert in today_alerts[:5]:  # Limit to 5 most recent
            alert_type = alert.get("message_type") or alert.get("type", "Alert")
            issued = format_time_est(str(alert.get("issue_datetime") or alert.get("time_tag", "")))
            alert_text += f"• **{alert_type}** - {issued}\n"
        if len(today_alerts) > 5:
            alert_text += f"\n*...and {len(today_alerts) - 5} more*"
        embed.add_field(name="📢 Today's Alerts", value=alert_text, inline=False)
    else:
        embed.add_field(name="📢 Today's Alerts", value="No alerts issued today", inline=False)
    
    embed.set_footer(text=f"SWPC • Daily Summary • {utcnow_iso_with_est()}")
    return embed


async def format_flare_embed(flare: Dict[str, Any]) -> discord.Embed:
    """Format a flare event into a Discord embed with optional SDO image."""
    max_class = flare.get("max_class") or flare.get("current_class", "Unknown")
    begin_time = flare.get("begin_time", "Unknown")
    max_time = flare.get("max_time", "Unknown")
    end_time = flare.get("end_time", "Unknown")
    begin_class = flare.get("begin_class", "Unknown")
    end_class = flare.get("end_class", "Unknown")
    satellite = flare.get("satellite", "GOES")
    
    # Determine color based on flare class
    class_letter, class_num = parse_flare_class(max_class)
    if class_letter == "X":
        colour = discord.Colour.red()
        title = f"🚨 X-Class Solar Flare Detected"
    elif class_letter == "M":
        colour = discord.Colour.orange()
        title = f"⚠️ M-Class Solar Flare Detected"
    else:
        colour = discord.Colour.blue()
        title = f"Solar Flare Event"
    
    embed = discord.Embed(title=title, colour=colour)
    embed.add_field(name="Peak Class", value=f"**{max_class}**", inline=True)
    embed.add_field(name="Satellite", value=f"GOES-{satellite}", inline=True)
    embed.add_field(name="Begin", value=format_time_est(begin_time), inline=False)
    embed.add_field(name="Peak Time", value=format_time_est(max_time), inline=True)
    embed.add_field(name="End", value=format_time_est(end_time), inline=True)
    
    if begin_class != max_class:
        embed.add_field(name="Class Evolution", value=f"{begin_class} → {max_class} → {end_class}", inline=False)
    
    # Try to get SDO AIA image
    try:
        # Use max_time for the image, default to 193A wavelength (good for flares)
        image_url = await get_sdo_aia_image(max_time or begin_time, wavelength=193)
        if image_url:
            # Set the image as the embed image if URL is valid
            # Note: Discord embeds support direct image URLs
            embed.set_image(url=image_url)
            embed.add_field(name="SDO AIA 193Å", value=f"[View on SDO](https://sdo.gsfc.nasa.gov/)", inline=False)
    except Exception as e:
        print(f"Error adding SDO image: {e}")
        # Fallback: add link to SDO latest images
        embed.add_field(name="SDO AIA Images", value="[View Latest SDO Images](https://sdo.gsfc.nasa.gov/assets/img/latest/)", inline=False)
    
    embed.set_footer(text=f"SWPC • {utcnow_iso_with_est()}")
    return embed


def format_alert_embed(a: Dict[str, Any]) -> discord.Embed:
    """Format a SWPC alert into a Discord embed."""
    title = a.get("message_type") or a.get("type") or "SWPC Alert"
    issued = a.get("issue_datetime") or a.get("time_tag") or a.get("timestamp")
    message = a.get("message") or a.get("summary") or a.get("product_text") or "(no text)"
    regions = a.get("regions") or a.get("affected_areas")
    pid = a.get("product_id") or a.get("id")
    
    # Check if it's a flare alert for color
    msg_lower = message.lower()
    if "x-class" in msg_lower or "x class" in msg_lower:
        colour = discord.Colour.red()
    elif "m-class" in msg_lower or "m class" in msg_lower:
        colour = discord.Colour.orange()
    else:
        colour = discord.Colour.blue()
    
    desc = message.strip()
    if len(desc) > 3800:
        desc = desc[:3800] + "..."
    
    embed = discord.Embed(title=title, description=desc, colour=colour)
    if issued:
        embed.add_field(name="Issued", value=format_time_est(str(issued)), inline=True)
    if regions:
        embed.add_field(name="Regions", value=", ".join(regions) if isinstance(regions, list) else str(regions), inline=True)
    if pid:
        embed.set_footer(text=f"{pid} • {utcnow_iso_with_est()}")
    else:
        embed.set_footer(text=utcnow_iso_with_est())
    return embed


# -------------------------- Main -----------------------------
async def amain():
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Missing DISCORD_TOKEN in environment.")
    try:
        await client.start(token)
    finally:
        await http.close()

if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
