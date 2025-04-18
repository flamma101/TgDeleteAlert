#!/usr/bin/env python3
"""
Telegram Message Logger Script
- Logs all incoming messages to a SQLite database
- Captures deletion events, records and displays message text
- Periodically checks for your own messages deleted by others in private chats
- Optionally sends a webhook and/or Telegram DM notification on deletion

Requirements:
- Python 3.7+
- telethon
- requests (only if using webhook)

Setup:
1. pip install telethon requests
2. Define the following environment variables: API_ID, API_HASH, STRING_SESSION, OWN_USER_ID, LOG_CHAT_ID, WEBHOOK_URL (optional)
3. Run: python3 telegram_logger.py
"""
import os
import sqlite3
import logging
from datetime import datetime
import asyncio
import re
import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User

# -- Configuration from environment --
API_ID        = int(os.getenv("API_ID"))                             # required
API_HASH      = os.getenv("API_HASH")                               # required
STRING_SESSION = os.getenv("STRING_SESSION", "")                  # required for user session login
OWN_USER_ID   = int(os.getenv("OWN_USER_ID"))                        # your Telegram user ID
LOG_CHAT_ID   = int(os.getenv("LOG_CHAT_ID"))                        # chat ID to log into (e.g., Saved Messages)
WEBHOOK_URL   = os.getenv("WEBHOOK_URL", "")                       # optional webhook URL

# Initialize client with string session
client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

# SQLite database path
db_path = os.path.join(os.path.dirname(__file__), 'telegram_messages.db')

# Enable logging to console
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -- Database Setup --
conn = sqlite3.connect(db_path, check_same_thread=False)
cursor = conn.cursor()

cursor.execute('''
CREATE TABLE IF NOT EXISTS messages (
    msg_id INTEGER PRIMARY KEY,
    chat_id INTEGER,
    from_id INTEGER,
    message TEXT,
    date TEXT,
    deleted INTEGER DEFAULT 0,
    detected_urls TEXT DEFAULT ''
);
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS deletions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id INTEGER,
    chat_id INTEGER,
    deleted_at TEXT,
    message TEXT DEFAULT '',
    reason TEXT DEFAULT ''
);
''')

# ensure schema is up-to-date
try:
    cursor.execute("ALTER TABLE deletions ADD COLUMN reason TEXT DEFAULT ''")
except sqlite3.OperationalError:
    pass

conn.commit()

# -- Utility Functions --
def extract_urls(text):
    return "\n".join(re.findall(r'https?://\S+', text)) if text else ''

def send_webhook(data):
    if not WEBHOOK_URL:
        return
    try:
        requests.post(WEBHOOK_URL, json=data, timeout=5)
    except Exception as e:
        logger.warning(f"Failed to send webhook: {e}")

async def send_telegram_alert(text):
    try:
        await client.send_message(OWN_USER_ID, f"🚨 Deleted Message Alert:\n{text}")
    except Exception as e:
        logger.warning(f"Failed to send Telegram alert: {e}")

# -- Event Handlers --
@client.on(events.NewMessage())
async def new_message_handler(event):
    msg = event.message
    text = msg.text or ''
    urls = extract_urls(text)
    chat_id = event.chat_id or (await event.get_chat()).id
    cursor.execute(
        'INSERT OR IGNORE INTO messages (msg_id, chat_id, from_id, message, date, detected_urls) VALUES (?, ?, ?, ?, datetime("now"), ?)',
        (msg.id, chat_id, msg.sender_id, text, urls)
    )
    conn.commit()
    logger.info(f"Logged message {msg.id} in chat {chat_id}: '{text}'")

@client.on(events.MessageDeleted())
async def deleted_message_handler(event):
    for msg_id in event.deleted_ids:
        row = cursor.execute(
            'SELECT message, chat_id FROM messages WHERE msg_id = ?', (msg_id,)
        ).fetchone()
        original_text = row[0] if row else '<text not available>'
        chat_id = row[1] if row else getattr(event, 'chat_id', 'unknown')

        cursor.execute('UPDATE messages SET deleted = 1 WHERE msg_id = ?', (msg_id,))
        cursor.execute(
            'INSERT INTO deletions (msg_id, chat_id, message, deleted_at, reason) VALUES (?, ?, ?, ?, ?)',
            (msg_id, chat_id, original_text, datetime.now().isoformat(), 'deleted_by_owner')
        )
        conn.commit()

        payload = {
            'msg_id': msg_id,
            'chat_id': chat_id,
            'message': original_text,
            'deleted_at': datetime.now().isoformat(),
            'reason': 'deleted_by_owner'
        }
        send_webhook(payload)
        try:
            entity = await client.get_entity(chat_id)
            username = f"@{entity.username}" if entity.username else entity.first_name or "Unknown"
        except Exception:
            username = chat_id
        await send_telegram_alert(
            f"Chat ID: {username}\nMessage ID: {msg_id}\nReason: deleted_by_owner\nContent:\n{original_text}"
        )
        logger.warning(f"Message {msg_id} was deleted in chat with {username}. Content: '{original_text}'")

# -- Watchdog for detecting your messages deleted by others --
async def watchdog_deleted_by_others():
    while True:
        try:
            chats = cursor.execute('SELECT DISTINCT chat_id FROM messages WHERE from_id = ?', (OWN_USER_ID,)).fetchall()
            for (chat_id,) in chats:
                try:
                    entity = await client.get_entity(chat_id)
                    if not isinstance(entity, User):
                        continue

                    db_msgs = cursor.execute(
                        'SELECT msg_id FROM messages WHERE chat_id = ? AND from_id = ? AND deleted = 0',
                        (chat_id, OWN_USER_ID)
                    ).fetchall()
                    db_msg_ids = set(r[0] for r in db_msgs)

                    actual_msg_ids = set()
                    async for msg in client.iter_messages(chat_id, from_user=OWN_USER_ID):
                        actual_msg_ids.add(msg.id)

                    deleted_ids = db_msg_ids - actual_msg_ids
                    for missing_id in deleted_ids:
                        row = cursor.execute('SELECT message FROM messages WHERE msg_id = ?', (missing_id,)).fetchone()
                        deleted_text = row[0] if row else '<unknown>'
                        cursor.execute('UPDATE messages SET deleted = 1 WHERE msg_id = ?', (missing_id,))
                        cursor.execute(
                            'INSERT INTO deletions (msg_id, chat_id, message, deleted_at, reason) VALUES (?, ?, ?, ?, ?)',
                            (missing_id, chat_id, deleted_text, datetime.now().isoformat(), 'deleted_by_other_party')
                        )
                        conn.commit()

                        payload = {
                            'msg_id': missing_id,
                            'chat_id': chat_id,
                            'message': deleted_text,
                            'deleted_at': datetime.now().isoformat(),
                            'reason': 'deleted_by_other_party'
                        }
                        send_webhook(payload)
                        try:
                            entity = await client.get_entity(chat_id)
                            username = f"@{entity.username}" if entity.username else entity.first_name or "Unknown"
                        except Exception:
                            username = chat_id
                        await send_telegram_alert(
                            f"Chat ID: {username}\nMessage ID: {missing_id}\nReason: deleted_by_other_party\nContent:\n{deleted_text}"
                        )
                        logger.warning(f"[Watchdog] Your message {missing_id} was likely deleted in chat with {username}. Content: '{deleted_text}'")
                except Exception as inner:
                    logger.error(f"Watchdog failed in chat {chat_id}: {inner}")
        except Exception as e:
            logger.error(f"Watchdog error: {e}")
        await asyncio.sleep(300)

# -- Main --
async def main():
    await client.start()
    me = await client.get_me()
    logger.info(f"Signed in as {me.first_name} (user_id={me.id}). Starting logger...")

    asyncio.create_task(watchdog_deleted_by_others())

    logger.info("Client started. Listening for messages and deletions...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
