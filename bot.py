#!/usr/bin/env python3
"""ClaudeGram - Control Claude Code CLI from Telegram.

Uses raw HTTP long-polling with async subprocess calls to Claude CLI.
Supports session continuity via --session-id, image/file uploads, and
working directory switching.
"""

import asyncio
import logging
import os
import sys
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
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))

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
# State
# ---------------------------------------------------------------------------
session_id: str | None = None
cwd: str = DEFAULT_CWD
current_process: asyncio.subprocess.Process | None = None
is_processing: bool = False


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


# ---------------------------------------------------------------------------
# Claude CLI
# ---------------------------------------------------------------------------
async def run_claude(prompt: str, notify_chat=None, notify_client=None) -> str:
    """Execute Claude CLI with the given prompt and return its output."""
    global session_id, current_process

    cmd = [CLAUDE_PATH, "-p", prompt, "--dangerously-skip-permissions"]

    if session_id is None:
        session_id = str(uuid.uuid4())
        cmd += ["--session-id", session_id]
    else:
        cmd += ["--resume", session_id]

    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)

    log.info("Claude: cwd=%s session=%s", cwd, session_id[:8])

    current_process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
    )

    async def progress_notifier():
        """Notify user that Claude is still working."""
        await asyncio.sleep(30)
        elapsed = 30
        if notify_chat and notify_client:
            try:
                await send_message(notify_client, notify_chat, f"Still working... ({elapsed}s elapsed)")
            except Exception:
                pass
        while True:
            await asyncio.sleep(300)
            elapsed += 300
            if notify_chat and notify_client:
                try:
                    await send_message(notify_client, notify_chat, f"Still working... ({elapsed}s elapsed)")
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
            current_process.kill()
            await current_process.wait()
        except ProcessLookupError:
            pass
        return f"Timeout ({CLAUDE_TIMEOUT}s). Use /stop or try a simpler request."
    finally:
        notifier_task.cancel()
        current_process = None

    out = stdout.decode().strip() if stdout else ""
    err = stderr.decode().strip() if stderr else ""

    log.info("Claude finished: exit=%s stdout=%d bytes, stderr=%d bytes", exit_code, len(out), len(err))
    if err:
        log.warning("Claude stderr: %s", err[:500])

    if not out and err:
        return f"Error:\n{err[:3000]}"
    if out and err:
        return out
    return out or "(empty response)"


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------
async def handle_command(client: httpx.AsyncClient, chat_id: int, cmd: str, args: str):
    """Handle bot slash commands."""
    global session_id, cwd, current_process, is_processing

    if cmd == "/new":
        session_id = None
        await send_message(client, chat_id, "Session cleared. Next message starts new conversation.")

    elif cmd == "/status":
        sid = session_id[:8] + "..." if session_id else "None"
        await send_message(client, chat_id, (
            f"Processing: {is_processing}\n"
            f"Session: {sid}\n"
            f"CWD: {cwd}\n"
            f"Timeout: {CLAUDE_TIMEOUT}s"
        ))

    elif cmd == "/stop":
        killed = False
        if current_process:
            try:
                current_process.kill()
                killed = True
            except ProcessLookupError:
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
        await send_message(client, chat_id, f"CWD: {cwd}")

    elif cmd == "/help":
        await send_message(client, chat_id, (
            "Commands:\n"
            "/new - Start new session\n"
            "/status - Bot status\n"
            "/stop - Kill running task\n"
            "/cd <path> - Change working dir\n"
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
    global is_processing

    async with httpx.AsyncClient() as client:
        await client.get(f"{API}/deleteWebhook?drop_pending_updates=true", timeout=10)
        await client.post(f"{API}/getUpdates", json={"offset": -1, "timeout": 0}, timeout=10)

        log.info("Bot started. Polling...")
        await client.post(
            f"{API}/sendMessage",
            json={"chat_id": ALLOWED_USER_ID, "text": "Bot started."},
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
                    await send_message(client, chat_id, "Still processing previous request... please wait.")
                    continue

                is_processing = True
                await send_message(client, chat_id, "Processing...")
                try:
                    response = await run_claude(prompt, notify_chat=chat_id, notify_client=client)
                    await send_message(client, chat_id, response)
                except Exception as e:
                    log.error("Claude error: %s", e, exc_info=True)
                    await send_message(client, chat_id, f"Error: {e}")
                finally:
                    is_processing = False


def main():
    asyncio.run(poll())


if __name__ == "__main__":
    main()
