#!/usr/bin/env python3
"""
Solar Flare Slack Bot

Slash commands:
  /flare_latest        – Latest detected solar flare event (full details)
  /flare_forecast      – Solar flare probability forecast (1-3 days)
  /flare_alerts        – Show recent SWPC flare alerts
  /latest_image        – Get the latest SDO AIA solar image
  /latest_forecast     – Get the latest solar flare probability forecast
  /latest_flare_class  – Get just the latest detected flare class
  /subscribe_flares    – Subscribe this channel to M- and X‑class flare notifications
  /unsubscribe_flares  – Stop flare notifications
  /subscribe_daily     – Subscribe this channel to daily summaries at 5pm EST/EDT
  /unsubscribe_daily   – Stop daily summaries

Requirements (pip):
  slack_sdk>=3.27.0 aiohttp>=3.9.5 python-dotenv>=1.0.1

Environment:
  SLACK_BOT_TOKEN=xoxb-your-bot-token
  SLACK_APP_TOKEN=xapp-your-app-token (for Socket Mode)
  SLACK_SIGNING_SECRET=your-signing-secret (for HTTP mode)

Notes:
  • Uses public JSON feeds from SWPC (no API keys).
  • Monitors real-time flare events and sends notifications for M/X-class flares.
  • Includes SDO AIA images when available for flare events.
  • Can run in Socket Mode (recommended) or HTTP mode.
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
    try:
        from backports.zoneinfo import ZoneInfo
    except ImportError:
        import pytz
        ZoneInfo = pytz.timezone
from urllib.parse import urlencode

import aiohttp
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from dotenv import load_dotenv

# --------------------------- Config ---------------------------
ENDPOINTS = {
    "alerts": "https://services.swpc.noaa.gov/products/alerts.json",
    "flare_latest": "https://services.swpc.noaa.gov/json/goes/primary/xray-flares-latest.json",
    "flare_forecast": "https://services.swpc.noaa.gov/json/solar_probabilities.json",
}

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
        async with s.get(url, headers={"User-Agent": "Flare-SlackBot/1.0"}) as r:
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
        import pytz
        est_tz = pytz.timezone("America/New_York")
        now_est = pytz.UTC.localize(now_utc.replace(tzinfo=None)).astimezone(est_tz)
    utc_str = now_utc.strftime("%Y-%m-%d %H:%M UTC")
    est_str = now_est.strftime("%H:%M %Z")
    return f"{utc_str} / {est_str}"


def convert_to_est(utc_time_str: str) -> str:
    """Convert UTC time string to EST/EDT format."""
    try:
        if "Z" in utc_time_str:
            dt_utc = datetime.fromisoformat(utc_time_str.replace("Z", "+00:00"))
        else:
            dt_utc = datetime.fromisoformat(utc_time_str.replace("+00:00", "").replace("UTC", "").strip() + "+00:00")
        
        try:
            est_tz = ZoneInfo("America/New_York")
        except (NameError, AttributeError):
            import pytz
            est_tz = pytz.timezone("America/New_York")
            dt_utc = pytz.UTC.localize(dt_utc.replace(tzinfo=None))
        dt_est = dt_utc.astimezone(est_tz)
        
        utc_str = dt_utc.strftime("%Y-%m-%d %H:%M UTC")
        est_str = dt_est.strftime("%Y-%m-%d %H:%M %Z")
        
        return f"{utc_str} / {est_str}"
    except Exception:
        return f"{utc_time_str} (UTC)"


def format_time_est(utc_time_str: Optional[str]) -> str:
    """Format time string showing EST/EDT, with fallback."""
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
            return data[0]
        return None
    except Exception as e:
        print(f"Error fetching latest flare: {e}")
        return None


async def fetch_flare_forecast() -> List[Dict[str, Any]]:
    """Fetch solar flare probability forecast."""
    try:
        data = await http.get_json(ENDPOINTS["flare_forecast"])
        if isinstance(data, list):
            return data[:3]
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
    """Get SDO AIA image URL for a given time."""
    try:
        if time:
            try:
                dt = datetime.fromisoformat(time.replace("Z", "+00:00"))
            except:
                dt = datetime.now(timezone.utc)
        else:
            dt = datetime.now(timezone.utc)
        
        return f"https://sdo.gsfc.nasa.gov/assets/img/latest/latest_{wavelength}.jpg"
    except Exception as e:
        print(f"Error getting SDO image: {e}")
        return None


# ----------------------- Slack Bot -------------------------
class FlareSlackBot:
    def __init__(self):
        load_dotenv()
        self.bot_token = os.getenv("SLACK_BOT_TOKEN")
        self.app_token = os.getenv("SLACK_APP_TOKEN")
        
        if not self.bot_token:
            raise SystemExit("Missing SLACK_BOT_TOKEN in environment.")
        
        self.client = WebClient(token=self.bot_token)
        self.seen_flare_ids: set[str] = set()
        self.subscribed_channels: set[str] = set()  # Channel IDs
        self.subscribed_daily: set[str] = set()
        self._last_flare_time: Optional[str] = None
        self._last_daily_date: Optional[str] = None
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        
        # Setup Socket Mode if app token provided
        if self.app_token:
            self.socket_client = SocketModeClient(
                app_token=self.app_token,
                web_client=self.client
            )
            self._setup_socket_handlers()
        else:
            self.socket_client = None
            print("Warning: No SLACK_APP_TOKEN provided. Socket Mode disabled.")
            print("You'll need to set up HTTP endpoints for slash commands.")
    
    def _setup_socket_handlers(self):
        """Setup Socket Mode event handlers."""
        self.socket_client.socket_mode_request_listeners.append(self._handle_socket_request)
    
    def _handle_socket_request(self, client: SocketModeClient, req: SocketModeRequest):
        """Handle incoming Socket Mode requests."""
        if req.type == "slash_commands":
            response = SocketModeResponse(envelope_id=req.envelope_id)
            client.send_socket_mode_response(response)
            # Schedule async command handler
            # Socket mode runs in a separate thread, so we need to schedule in the main event loop
            if hasattr(self, '_main_loop') and self._main_loop:
                asyncio.run_coroutine_threadsafe(
                    self._handle_slash_command(req.payload),
                    self._main_loop
                )
            else:
                # Fallback: run in new event loop if main loop not available
                asyncio.run(self._handle_slash_command(req.payload))
        elif req.type == "events_api":
            response = SocketModeResponse(envelope_id=req.envelope_id)
            client.send_socket_mode_response(response)
            # Handle events if needed
    
    async def _handle_slash_command(self, payload: Dict[str, Any]):
        """Handle slash command execution."""
        command = payload.get("command", "")
        channel_id = payload.get("channel_id")
        user_id = payload.get("user_id")
        text = payload.get("text", "").strip()
        
        try:
            if command == "/flare_latest":
                await self._cmd_flare_latest(channel_id)
            elif command == "/flare_forecast":
                await self._cmd_flare_forecast(channel_id)
            elif command == "/flare_alerts":
                limit = int(text) if text.isdigit() and 1 <= int(text) <= 10 else 5
                await self._cmd_flare_alerts(channel_id, limit)
            elif command == "/latest_image":
                wavelength = int(text) if text.isdigit() and 94 <= int(text) <= 335 else 193
                await self._cmd_latest_image(channel_id, wavelength)
            elif command == "/latest_forecast":
                await self._cmd_latest_forecast(channel_id)
            elif command == "/latest_flare_class":
                await self._cmd_latest_flare_class(channel_id)
            elif command == "/subscribe_flares":
                await self._cmd_subscribe_flares(channel_id)
            elif command == "/unsubscribe_flares":
                await self._cmd_unsubscribe_flares(channel_id)
            elif command == "/subscribe_daily":
                await self._cmd_subscribe_daily(channel_id)
            elif command == "/unsubscribe_daily":
                await self._cmd_unsubscribe_daily(channel_id)
        except Exception as e:
            print(f"Error handling command {command}: {e}")
            await self._send_message(channel_id, f"Error: {str(e)}")
    
    async def _send_message(self, channel: str, text: str, blocks: Optional[List[Dict]] = None):
        """Send a message to a Slack channel."""
        try:
            self.client.chat_postMessage(
                channel=channel,
                text=text,
                blocks=blocks
            )
        except SlackApiError as e:
            print(f"Error sending message: {e}")
    
    # Command handlers
    async def _cmd_flare_latest(self, channel: str):
        flare = await fetch_latest_flare()
        if not flare:
            await self._send_message(channel, "No flare data available.")
            return
        blocks = await self._format_flare_blocks(flare)
        await self._send_message(channel, "Latest Solar Flare Event", blocks)
    
    async def _cmd_flare_forecast(self, channel: str):
        forecast = await fetch_flare_forecast()
        if not forecast:
            await self._send_message(channel, "No forecast data available.")
            return
        blocks = self._format_forecast_blocks(forecast)
        await self._send_message(channel, "Solar Flare Probability Forecast", blocks)
    
    async def _cmd_flare_alerts(self, channel: str, limit: int):
        items = await fetch_alerts(limit=limit)
        if not items:
            await self._send_message(channel, "No flare alerts found.")
            return
        for alert in items:
            blocks = self._format_alert_blocks(alert)
            await self._send_message(channel, "SWPC Flare Alert", blocks)
    
    async def _cmd_latest_image(self, channel: str, wavelength: int):
        flare = await fetch_latest_flare()
        flare_time = None
        if flare:
            flare_time = flare.get("max_time") or flare.get("time_tag") or flare.get("begin_time")
        image_url = await get_sdo_aia_image(flare_time, wavelength=wavelength)
        blocks = self._format_image_blocks(wavelength, image_url, flare_time)
        await self._send_message(channel, f"SDO AIA {wavelength}Å Solar Image", blocks)
    
    async def _cmd_latest_forecast(self, channel: str):
        forecast = await fetch_flare_forecast()
        if not forecast or len(forecast) == 0:
            await self._send_message(channel, "No forecast data available.")
            return
        latest = forecast[0]
        blocks = self._format_latest_forecast_blocks(latest)
        await self._send_message(channel, "Latest Solar Flare Forecast", blocks)
    
    async def _cmd_latest_flare_class(self, channel: str):
        flare = await fetch_latest_flare()
        if not flare:
            await self._send_message(channel, "No flare data available.")
            return
        blocks = self._format_flare_class_blocks(flare)
        await self._send_message(channel, "Latest Flare Class", blocks)
    
    async def _cmd_subscribe_flares(self, channel: str):
        self.subscribed_channels.add(channel)
        await self._send_message(channel, "✅ Subscribed: I'll notify you when M- or X-class flares are detected!")
    
    async def _cmd_unsubscribe_flares(self, channel: str):
        if channel in self.subscribed_channels:
            self.subscribed_channels.discard(channel)
            await self._send_message(channel, "Unsubscribed from flare notifications.")
        else:
            await self._send_message(channel, "This channel isn't subscribed for flare notifications.")
    
    async def _cmd_subscribe_daily(self, channel: str):
        self.subscribed_daily.add(channel)
        await self._send_message(channel, "✅ Subscribed: I'll post a daily summary at 5pm EST/EDT with today's forecast and events!")
    
    async def _cmd_unsubscribe_daily(self, channel: str):
        if channel in self.subscribed_daily:
            self.subscribed_daily.discard(channel)
            await self._send_message(channel, "Unsubscribed from daily summaries.")
        else:
            await self._send_message(channel, "This channel isn't subscribed for daily summaries.")
    
    # Background tasks
    async def _flare_poll_task(self):
        """Poll for new flares and notify subscribed channels."""
        while True:
            try:
                if self.subscribed_channels:
                    flare = await fetch_latest_flare()
                    if flare:
                        flare_class = flare.get("max_class") or flare.get("current_class")
                        if is_m_or_x_class(flare_class or ""):
                            flare_time = flare.get("max_time") or flare.get("time_tag") or flare.get("begin_time")
                            flare_id = f"{flare_time}_{flare_class}"
                            
                            if flare_id not in self.seen_flare_ids:
                                self.seen_flare_ids.add(flare_id)
                                blocks = await self._format_flare_blocks(flare)
                                
                                for ch_id in list(self.subscribed_channels):
                                    await self._send_message(ch_id, "🚨 New Flare Detected!", blocks)
                await asyncio.sleep(CHECK_INTERVAL_SEC)
            except Exception as e:
                print(f"flare_poll error: {e}")
                await asyncio.sleep(CHECK_INTERVAL_SEC)
    
    async def _daily_summary_task(self):
        """Send daily summary at 5pm EST/EDT."""
        while True:
            try:
                now_utc = datetime.now(timezone.utc)
                try:
                    est_tz = ZoneInfo("America/New_York")
                except (NameError, AttributeError):
                    import pytz
                    est_tz = pytz.timezone("America/New_York")
                now_est = now_utc.astimezone(est_tz)
                
                if now_est.hour == 17 and now_est.minute < 5:
                    today = now_est.strftime("%Y-%m-%d")
                    if self._last_daily_date != today and self.subscribed_daily:
                        self._last_daily_date = today
                        blocks = await self._build_daily_summary_blocks(now_est)
                        
                        for ch_id in list(self.subscribed_daily):
                            await self._send_message(ch_id, "📊 Daily Solar Flare Summary", blocks)
                
                await asyncio.sleep(300)  # Check every 5 minutes
            except Exception as e:
                print(f"daily_summary error: {e}")
                await asyncio.sleep(300)
    
    # Block formatters (Slack Block Kit)
    async def _format_flare_blocks(self, flare: Dict[str, Any]) -> List[Dict]:
        """Format flare event as Slack blocks."""
        max_class = flare.get("max_class") or flare.get("current_class", "Unknown")
        begin_time = format_time_est(flare.get("begin_time", "Unknown"))
        max_time = format_time_est(flare.get("max_time", "Unknown"))
        end_time = format_time_est(flare.get("end_time", "Unknown"))
        
        class_letter, _ = parse_flare_class(max_class)
        color = "#ff0000" if class_letter == "X" else "#ff8800" if class_letter == "M" else "#0066ff"
        
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{'🚨' if class_letter == 'X' else '⚠️' if class_letter == 'M' else '⚡'} {max_class} Solar Flare"
                }
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Peak Class:*\n{max_class}"},
                    {"type": "mrkdwn", "text": f"*Satellite:*\nGOES-{flare.get('satellite', 'Unknown')}"},
                    {"type": "mrkdwn", "text": f"*Begin:*\n{begin_time}"},
                    {"type": "mrkdwn", "text": f"*Peak Time:*\n{max_time}"},
                    {"type": "mrkdwn", "text": f"*End:*\n{end_time}"}
                ]
            }
        ]
        
        # Try to get SDO image, but make it optional since URLs may not be accessible
        try:
            image_url = await get_sdo_aia_image(max_time or flare.get("begin_time"), 193)
            if image_url:
                # Add image as a text link instead of image block to avoid download issues
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*SDO AIA 193Å Image:* <{image_url}|View Image> | <https://sdo.gsfc.nasa.gov/assets/img/latest/|View Latest SDO Images>"
                    }
                })
        except Exception as e:
            print(f"Error adding SDO image: {e}")
            # Add fallback link
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*SDO AIA Images:* <https://sdo.gsfc.nasa.gov/assets/img/latest/|View Latest SDO Images>"
                }
            })
        
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"SWPC • {utcnow_iso_with_est()}"}]
        })
        
        return blocks
    
    def _format_forecast_blocks(self, forecast: List[Dict]) -> List[Dict]:
        """Format forecast as Slack blocks."""
        blocks = [{"type": "header", "text": {"type": "plain_text", "text": "📈 Solar Flare Probability Forecast"}}]
        
        for day_data in forecast:
            date_str = day_data.get("date", "Unknown")
            if "T" in date_str:
                date_str = date_str.split("T")[0]
            
            m_1d = day_data.get("m_class_1_day", 0)
            x_1d = day_data.get("x_class_1_day", 0)
            
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*{date_str}*\nM-class: {m_1d}% | X-class: {x_1d}%"
                }
            })
        
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"SWPC • {utcnow_iso_with_est()}"}]
        })
        
        return blocks
    
    def _format_alert_blocks(self, alert: Dict[str, Any]) -> List[Dict]:
        """Format alert as Slack blocks."""
        title = alert.get("message_type") or alert.get("type") or "SWPC Alert"
        message = alert.get("message") or alert.get("summary") or "(no text)"
        issued = format_time_est(str(alert.get("issue_datetime") or alert.get("time_tag", "")))
        
        return [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": title}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": message[:2000]}
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Issued: {issued} • {utcnow_iso_with_est()}"}]
            }
        ]
    
    def _format_image_blocks(self, wavelength: int, image_url: Optional[str], time: Optional[str]) -> List[Dict]:
        """Format image as Slack blocks."""
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🖼️ SDO AIA {wavelength}Å Solar Image"}
            }
        ]
        
        if image_url:
            # Use text link instead of image block to avoid Slack download issues
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Image:* <{image_url}|View SDO AIA {wavelength}Å Image> | <https://sdo.gsfc.nasa.gov/assets/img/latest/|View All Latest Images>"
                }
            })
        else:
            # Fallback link if no image URL
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*SDO AIA Images:* <https://sdo.gsfc.nasa.gov/assets/img/latest/|View Latest SDO Images>"
                }
            })
        
        if time:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Time: {format_time_est(time)}"}]
            })
        
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"SWPC • {utcnow_iso_with_est()}"}]
        })
        
        return blocks
    
    def _format_latest_forecast_blocks(self, forecast: Dict) -> List[Dict]:
        """Format latest forecast as Slack blocks."""
        date_str = forecast.get("date", "Unknown")
        if "T" in date_str:
            date_str = date_str.split("T")[0]
        
        return [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "📈 Latest Solar Flare Forecast"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Date:*\n{date_str}"},
                    {"type": "mrkdwn", "text": f"*M-Class (1 day):*\n{forecast.get('m_class_1_day', 0)}%"},
                    {"type": "mrkdwn", "text": f"*X-Class (1 day):*\n{forecast.get('x_class_1_day', 0)}%"},
                    {"type": "mrkdwn", "text": f"*M-Class (2 day):*\n{forecast.get('m_class_2_day', 0)}%"},
                    {"type": "mrkdwn", "text": f"*X-Class (2 day):*\n{forecast.get('x_class_2_day', 0)}%"},
                    {"type": "mrkdwn", "text": f"*M-Class (3 day):*\n{forecast.get('m_class_3_day', 0)}%"},
                    {"type": "mrkdwn", "text": f"*X-Class (3 day):*\n{forecast.get('x_class_3_day', 0)}%"}
                ]
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"SWPC • {utcnow_iso_with_est()}"}]
            }
        ]
    
    def _format_flare_class_blocks(self, flare: Dict) -> List[Dict]:
        """Format flare class as Slack blocks."""
        max_class = flare.get("max_class") or flare.get("current_class", "None")
        max_time = format_time_est(flare.get("max_time") or flare.get("time_tag", "Unknown"))
        
        class_letter, _ = parse_flare_class(max_class)
        emoji = "🚨" if class_letter == "X" else "⚠️" if class_letter == "M" else "⚡" if class_letter == "C" else "☀️"
        
        return [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} Latest Flare Class: {max_class}"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Peak Class:*\n{max_class}"},
                    {"type": "mrkdwn", "text": f"*Peak Time:*\n{max_time}"}
                ]
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"SWPC • {utcnow_iso_with_est()}"}]
            }
        ]
    
    async def _build_daily_summary_blocks(self, date_est: datetime) -> List[Dict]:
        """Build daily summary blocks."""
        date_str = date_est.strftime("%Y-%m-%d")
        date_display = date_est.strftime("%B %d, %Y")
        
        # Get forecast
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
            if not today_forecast and forecast_data:
                today_forecast = forecast_data[0]
        
        # Get today's alerts
        all_alerts = await fetch_alerts(limit=20)
        today_alerts = []
        for alert in all_alerts:
            issued = alert.get("issue_datetime") or alert.get("time_tag") or alert.get("timestamp")
            if issued:
                try:
                    if "T" in str(issued):
                        alert_date = str(issued).split("T")[0]
                        if alert_date == date_str:
                            today_alerts.append(alert)
                except:
                    pass
        
        # Extract flare events
        import re
        today_flare_events = []
        for alert in today_alerts:
            msg = alert.get("message") or alert.get("summary") or ""
            issued = alert.get("issue_datetime") or alert.get("time_tag") or ""
            flare_match = re.search(r'\b([MXCB])\s*(\d+(?:\.\d+)?)\s*(?:class|flare|event)?', msg, re.IGNORECASE)
            if not flare_match:
                flare_match = re.search(r'\b([MXCB])(\d+(?:\.\d+)?)\b', msg, re.IGNORECASE)
            if flare_match:
                flare_class = flare_match.group(1).upper() + flare_match.group(2)
                if len(flare_class) > 1:
                    today_flare_events.append({
                        "class": flare_class,
                        "time": format_time_est(str(issued)) if issued else "Unknown"
                    })
        
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"📊 Daily Solar Flare Summary - {date_display}"}
            }
        ]
        
        # Forecast section
        if today_forecast:
            forecast_text = f"*Today (1 day):* M-class: {today_forecast.get('m_class_1_day', 0)}% | X-class: {today_forecast.get('x_class_1_day', 0)}%\n"
            forecast_text += f"*Tomorrow (2 day):* M-class: {today_forecast.get('m_class_2_day', 0)}% | X-class: {today_forecast.get('x_class_2_day', 0)}%\n"
            forecast_text += f"*Day 3:* M-class: {today_forecast.get('m_class_3_day', 0)}% | X-class: {today_forecast.get('x_class_3_day', 0)}%"
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*📈 Forecast Probabilities*\n{forecast_text}"}
            })
        
        # Flare events
        if today_flare_events:
            events_text = ""
            for event in today_flare_events[:10]:  # Limit to 10
                flare_class = event.get("class", "Unknown")
                event_time = event.get("time", "Unknown")
                class_letter, _ = parse_flare_class(flare_class)
                emoji = "🚨" if class_letter == "X" else "⚠️" if class_letter == "M" else "⚡"
                events_text += f"{emoji} *{flare_class}* - {event_time}\n"
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*⚡ Today's Flare Events ({len(today_flare_events)})*\n{events_text}"}
            })
        else:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*⚡ Today's Flare Events*\nNo flares detected today"}
            })
        
        # Alerts
        if today_alerts:
            alert_text = ""
            for alert in today_alerts[:5]:
                alert_type = alert.get("message_type") or alert.get("type", "Alert")
                issued = format_time_est(str(alert.get("issue_datetime") or alert.get("time_tag", "")))
                alert_text += f"• *{alert_type}* - {issued}\n"
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*📢 Today's Alerts*\n{alert_text}"}
            })
        
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"SWPC • Daily Summary • {utcnow_iso_with_est()}"}]
        })
        
        return blocks
    
    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        """Start the bot."""
        if loop:
            self._main_loop = loop
        else:
            try:
                self._main_loop = asyncio.get_event_loop()
            except RuntimeError:
                self._main_loop = None
        
        if self.socket_client:
            # Start background tasks in the event loop
            if self._main_loop:
                asyncio.create_task(self._flare_poll_task())
                asyncio.create_task(self._daily_summary_task())
            # Connect Socket Mode (runs in separate thread)
            self.socket_client.connect()
            print("Slack bot started in Socket Mode")
        else:
            print("Socket Mode not available. Set up HTTP endpoints for production use.")


# -------------------------- Main -----------------------------
async def amain():
    bot = FlareSlackBot()
    loop = asyncio.get_event_loop()
    bot.start(loop=loop)
    
    # Keep running
    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        await http.close()
        if bot.socket_client:
            bot.socket_client.close()

if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass

