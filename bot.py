#!/usr/bin/env python3
"""ClaudeGram - Control Claude Code CLI from Telegram.

Uses raw HTTP long-polling with async subprocess calls to Claude CLI.
Supports session persistence, model switching, time estimates, and
non-blocking task execution.
"""

import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

import httpx
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
CLAUDE_PATH = os.getenv("CLAUDE_PATH", "claude")
DEFAULT_CWD = os.getenv("DEFAULT_CWD", str(Path.home()))
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "7200"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "bot.log")

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), file_handler],
)
log = logging.getLogger("claudegram")

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json")


def load_state():
    """Load persisted state (session_id, cwd, model) from file."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_state():
    """Persist current state to file."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"session_id": session_id, "cwd": cwd, "model": model}, f)
    except Exception as e:
        log.error("Failed to save state: %s", e)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_state = load_state()
session_id: str | None = _state.get("session_id")
cwd: str = _state.get("cwd", DEFAULT_CWD)
model: str = _state.get("model", "sonnet")
current_process: asyncio.subprocess.Process | None = None
is_processing: bool = False
task_estimate_minutes: float | None = None
task_start_time: float | None = None


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------
async def send_message(client: httpx.AsyncClient, chat_id: int | str, text: str):
    """Send a message, splitting if too long for Telegram."""
    for chunk in split_message(text):
        try:
            await client.post(
                f"{API}/sendMessage",
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"},
                timeout=15,
            )
        except Exception:
            # Retry without Markdown if it fails (e.g. unmatched formatting)
            await client.post(
                f"{API}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
                timeout=15,
            )


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Split text into chunks that fit Telegram's message size limit."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        idx = text.rfind("\n", 0, limit)
        if idx == -1:
            idx = text.rfind(" ", 0, limit)
        if idx == -1:
            idx = limit
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")
    return chunks


def detect_rate_limit(text: str) -> str | None:
    """Check if output indicates a rate limit / quota error."""
    lower = text.lower()
    keywords = ["rate limit", "rate_limit", "too many requests", "quota", "overloaded",
                 "capacity", "429", "insufficient_quota", "billing"]
    for kw in keywords:
        if kw in lower:
            return "Rate limit reached. Please wait for quota to refresh or check your subscription plan."
    return None


# ---------------------------------------------------------------------------
# Claude CLI
# ---------------------------------------------------------------------------
def _model_flag() -> list[str]:
    """Return model flag for claude CLI."""
    if model == "opus":
        return ["--model", "claude-opus-4-6"]
    return []


async def run_claude(prompt: str, notify_chat=None, notify_client=None) -> str:
    """Execute Claude CLI with the given prompt and return its output."""
    global session_id, current_process, task_start_time

    cmd = [CLAUDE_PATH, "-p", prompt, "--dangerously-skip-permissions"] + _model_flag()

    is_new_session = session_id is None
    if is_new_session:
        session_id = str(uuid.uuid4())
        cmd += ["--session-id", session_id]
        save_state()
    else:
        cmd += ["--resume", session_id]

    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)

    log.info("Claude: cwd=%s session=%s model=%s", cwd, session_id[:8], model)

    current_process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,  # own process group so we can kill all children
    )

    task_start_time = time.time()

    # Notify new session ID
    if is_new_session and notify_chat and notify_client:
        try:
            await send_message(notify_client, notify_chat, f"New session: `{session_id[:8]}`")
        except Exception:
            pass

    # Progress notifier: first at 30s, then every 15min with remaining time estimate
    async def progress_notifier():
        await asyncio.sleep(30)
        if notify_chat and notify_client:
            try:
                await send_message(notify_client, notify_chat, "Still working... (30s elapsed)")
            except Exception:
                pass
        while True:
            await asyncio.sleep(900)  # 15 minutes
            elapsed = time.time() - task_start_time
            elapsed_min = elapsed / 60
            if task_estimate_minutes and task_estimate_minutes > elapsed_min:
                remaining = task_estimate_minutes - elapsed_min
                msg = f"Still working... ({elapsed_min:.0f}min elapsed, ~{remaining:.0f}min remaining)"
            else:
                msg = f"Still working... ({elapsed_min:.0f}min elapsed)"
            if notify_chat and notify_client:
                try:
                    await send_message(notify_client, notify_chat, msg)
                except Exception:
                    pass

    notifier_task = asyncio.create_task(progress_notifier())

    try:
        stdout, stderr = await asyncio.wait_for(
            current_process.communicate(), timeout=CLAUDE_TIMEOUT
        )
        exit_code = current_process.returncode
    except asyncio.TimeoutError:
        try:
            os.killpg(current_process.pid, signal.SIGKILL)
            await current_process.wait()
        except (ProcessLookupError, OSError):
            pass
        return f"Timeout ({CLAUDE_TIMEOUT // 60}min). Use /stop or try a simpler request."
    finally:
        notifier_task.cancel()
        current_process = None
        task_start_time = None

    out = stdout.decode().strip() if stdout else ""
    err = stderr.decode().strip() if stderr else ""

    log.info("Claude finished: exit=%s stdout=%d bytes, stderr=%d bytes", exit_code, len(out), len(err))
    if err:
        log.warning("Claude stderr: %s", err[:500])

    # Check for rate limit errors
    for text in [out, err]:
        rate_msg = detect_rate_limit(text)
        if rate_msg:
            return rate_msg

    if not out and err:
        return f"Error:\n{err[:3000]}"
    if out and err:
        return out
    return out or "(empty response)"


async def run_claude_oneshot(prompt: str) -> str:
    """Run a standalone Claude query (no session, no interference with main task)."""
    cmd = [CLAUDE_PATH, "-p", prompt, "--dangerously-skip-permissions"] + _model_flag()
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        except (ProcessLookupError, OSError):
            pass
        return "Timeout (oneshot)."

    out = stdout.decode().strip() if stdout else ""
    err = stderr.decode().strip() if stderr else ""

    # Check for rate limit
    for text in [out, err]:
        rate_msg = detect_rate_limit(text)
        if rate_msg:
            return rate_msg

    if not out and err:
        return f"Error:\n{err[:3000]}"
    return out or "(empty response)"


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------
async def handle_command(client: httpx.AsyncClient, chat_id: int, cmd: str, args: str):
    """Handle bot slash commands."""
    global session_id, cwd, current_process, is_processing, model

    if cmd == "/new":
        old_sid = session_id[:8] if session_id else "None"
        session_id = None
        save_state()
        await send_message(client, chat_id, f"Session cleared (old: `{old_sid}`). Next message starts new conversation.")

    elif cmd == "/resume":
        if not args:
            await send_message(client, chat_id, "Usage: /resume <session-id prefix or full id>")
            return
        target = args.strip()
        session_id = target
        save_state()
        await send_message(client, chat_id, f"Session set to: `{target[:8]}`")

    elif cmd == "/status":
        sid = session_id[:8] + "..." if session_id else "None"
        model_display = "Opus" if model == "opus" else "Sonnet"
        await send_message(client, chat_id, (
            f"Processing: {is_processing}\n"
            f"Session: `{sid}`\n"
            f"Model: {model_display}\n"
            f"CWD: {cwd}\n"
            f"Timeout: {CLAUDE_TIMEOUT // 60}min"
        ))

    elif cmd == "/stop":
        killed = False
        if current_process:
            try:
                os.killpg(current_process.pid, signal.SIGKILL)
                killed = True
            except (ProcessLookupError, OSError):
                pass
        msg = "Killed running process." if killed else "Nothing running."
        await send_message(client, chat_id, msg)

    elif cmd == "/cd":
        if not args:
            await send_message(client, chat_id, f"Current: {cwd}\nUsage: /cd <path>")
            return
        path = os.path.expanduser(args)
        if not os.path.isabs(path):
            path = os.path.join(cwd, path)
        path = os.path.normpath(path)
        if not os.path.isdir(path):
            await send_message(client, chat_id, f"Not a directory: {path}")
            return
        cwd = path
        save_state()
        await send_message(client, chat_id, f"CWD: {cwd}")

    elif cmd == "/btw":
        if not args:
            await send_message(client, chat_id, "Usage: /btw <question>")
            return
        await send_message(client, chat_id, "Side question...")
        try:
            reply = await run_claude_oneshot(args)
            await send_message(client, chat_id, reply)
        except Exception as e:
            log.error("btw error: %s", e, exc_info=True)
            await send_message(client, chat_id, f"Error: {e}")

    elif cmd == "/opus":
        model = "opus"
        save_state()
        await send_message(client, chat_id, "Switched to Opus model.")

    elif cmd == "/sonnet":
        model = "sonnet"
        save_state()
        await send_message(client, chat_id, "Switched to Sonnet model.")

    elif cmd == "/help":
        await send_message(client, chat_id, (
            "Commands:\n"
            "/new - New session (shows old session ID)\n"
            "/resume <id> - Resume old session\n"
            "/status - Bot status\n"
            "/stop - Kill running task\n"
            "/cd <path> - Change working dir\n"
            "/btw <question> - Ask without interrupting task\n"
            "/opus - Switch to Opus model\n"
            "/sonnet - Switch to Sonnet model\n"
            "/help - This message\n\n"
            "Send any text to chat with Claude.\n"
            "You can also send images and files."
        ))


# ---------------------------------------------------------------------------
# File handling
# ---------------------------------------------------------------------------
FILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(FILE_DIR, exist_ok=True)


async def download_file(client: httpx.AsyncClient, file_id: str, file_name: str | None = None) -> str:
    """Download a Telegram file and return the local path."""
    resp = await client.post(f"{API}/getFile", json={"file_id": file_id}, timeout=10)
    file_path = resp.json()["result"]["file_path"]

    if file_name:
        local_name = f"{uuid.uuid4().hex[:8]}_{file_name}"
    else:
        ext = os.path.splitext(file_path)[1] or ".jpg"
        local_name = f"{uuid.uuid4().hex[:12]}{ext}"
    local_path = os.path.join(FILE_DIR, local_name)

    dl_resp = await client.get(
        f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}", timeout=30
    )
    with open(local_path, "wb") as f:
        f.write(dl_resp.content)

    log.info("Downloaded file: %s (%d bytes)", local_name, len(dl_resp.content))
    return local_path


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------
async def poll():
    """Long-poll Telegram for updates and dispatch to Claude."""
    global is_processing, task_estimate_minutes

    async with httpx.AsyncClient() as client:
        await client.get(f"{API}/deleteWebhook?drop_pending_updates=true", timeout=10)
        await client.post(f"{API}/getUpdates", json={"offset": -1, "timeout": 0}, timeout=10)

        log.info("Bot started. Polling...")
        sid_info = f" session=`{session_id[:8]}`" if session_id else ""
        model_info = "Opus" if model == "opus" else "Sonnet"
        await client.post(
            f"{API}/sendMessage",
            json={"chat_id": ALLOWED_USER_ID, "text": f"Bot started. Model: {model_info}{sid_info}"},
            timeout=10,
        )

        offset = 0
        conflict_wait = 0
        while True:
            try:
                resp = await client.post(
                    f"{API}/getUpdates",
                    json={"offset": offset, "timeout": 30},
                    timeout=35,
                )
                data = resp.json()
                if resp.status_code == 409 or not data.get("ok"):
                    conflict_wait = min(conflict_wait + 10, 60)
                    log.warning("Conflict/error, waiting %ds...", conflict_wait)
                    await asyncio.sleep(conflict_wait)
                    continue
                conflict_wait = 0
                updates = data.get("result", [])
            except httpx.TimeoutException:
                continue
            except httpx.ConnectError as e:
                log.warning("Network error: %s", e)
                await asyncio.sleep(5)
                continue
            except Exception as e:
                log.error("Polling error: %s", e, exc_info=True)
                await asyncio.sleep(3)
                continue

            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                user_id = msg.get("from", {}).get("id")
                chat_id = msg.get("chat", {}).get("id")

                if user_id != ALLOWED_USER_ID:
                    continue

                text = msg.get("text", "").strip()
                caption = msg.get("caption", "").strip()
                photos = msg.get("photo")
                document = msg.get("document")

                if not text and not caption and not photos and not document:
                    continue

                # Command
                if text and text.startswith("/"):
                    parts = text.split(None, 1)
                    cmd = parts[0].lower().split("@")[0]
                    args = parts[1] if len(parts) > 1 else ""
                    await handle_command(client, chat_id, cmd, args)
                    continue

                # Build prompt
                prompt = text or caption or ""
                image_paths = []
                file_paths = []

                try:
                    if photos:
                        file_id = photos[-1]["file_id"]
                        path = await download_file(client, file_id)
                        image_paths.append(path)

                    if document:
                        mime = document.get("mime_type", "")
                        doc_name = document.get("file_name")
                        path = await download_file(client, document["file_id"], file_name=doc_name)
                        if mime.startswith("image/"):
                            image_paths.append(path)
                        else:
                            file_paths.append(path)
                except Exception as e:
                    log.error("Failed to download file: %s", e)
                    await send_message(client, chat_id, f"Failed to download file: {e}")
                    continue

                if image_paths:
                    img_refs = "\n".join(f"[Image: {p}]" for p in image_paths)
                    if prompt:
                        prompt = f"{prompt}\n\nUser sent the following image(s), read them with your Read tool:\n{img_refs}"
                    else:
                        prompt = f"User sent the following image(s), read and analyze them with your Read tool:\n{img_refs}"

                if file_paths:
                    file_refs = "\n".join(f"[File: {p}]" for p in file_paths)
                    if prompt:
                        prompt = f"{prompt}\n\nUser sent the following file(s), read them with your Read tool:\n{file_refs}"
                    else:
                        prompt = f"User sent the following file(s), read and analyze them with your Read tool:\n{file_refs}"

                if not prompt:
                    continue

                if is_processing:
                    await send_message(client, chat_id, "Still processing previous request... /stop to cancel.")
                    continue

                is_processing = True
                await send_message(client, chat_id, "Processing...")

                # Run claude task in background so polling loop stays responsive
                _prompt = prompt

                async def _run_task(p, cid, cl):
                    global is_processing, task_estimate_minutes

                    # Estimate: only send if task takes > 30s
                    async def send_estimate():
                        try:
                            est = await run_claude_oneshot(
                                f"Estimate how long this task would take. Reply with ONLY the estimated time and a brief reason. "
                                f"Do NOT perform the task itself:\n{p[:1000]}"
                            )
                            if est:
                                # Wait until 30s have passed since task start
                                while task_start_time and (time.time() - task_start_time) < 30:
                                    await asyncio.sleep(1)
                                # Only send if task is still running
                                if is_processing:
                                    await send_message(cl, cid, f"Estimate: {est}")
                                    # Try to parse minutes from estimate
                                    nums = re.findall(r'(\d+)\s*(?:min|minute)', est, re.IGNORECASE)
                                    if nums:
                                        task_estimate_minutes = float(nums[0])
                                    else:
                                        hrs = re.findall(r'(\d+)\s*(?:hour|hr)', est, re.IGNORECASE)
                                        if hrs:
                                            task_estimate_minutes = float(hrs[0]) * 60
                        except Exception:
                            pass

                    estimate_task = asyncio.create_task(send_estimate())
                    try:
                        response = await run_claude(p, notify_chat=cid, notify_client=cl)
                        estimate_task.cancel()
                        await send_message(cl, cid, response)
                    except Exception as e:
                        log.error("Claude error: %s", e, exc_info=True)
                        await send_message(cl, cid, f"Error: {e}")
                    finally:
                        is_processing = False
                        task_estimate_minutes = None

                asyncio.create_task(_run_task(_prompt, chat_id, client))


def main():
    asyncio.run(poll())


if __name__ == "__main__":
    main()
