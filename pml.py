"""
Custom PM Logger Plugin for CatUserBot
=====================================

This plugin extends the private‐message logging functionality provided by
the default `logchats.py` module by allowing the owner to explicitly
choose which contacts to monitor and by notifying the owner whenever
monitored messages are deleted.  It also supports temporarily logging
messages from new contacts for a configurable duration.  To enable or
disable logging, use the commands documented below.

Commands
--------

```
{tr}pml on
    Enable custom PM logging.  This will overwrite the list of
    currently known dialogs in the internal database.  All messages
    from users on the monitored list (see ``{tr}pml add``) will be
    forwarded to the group specified by ``PM_LOGGER_GROUP_ID`` in your
    configuration.

{tr}pml off
    Disable custom PM logging.

{tr}pml add <username/userid/current chat>
    Add a user to the monitored list.  If no argument is supplied,
    the ID of the current chat will be used.

{tr}pml del <username/userid/current chat>
    Remove a user from the monitored list.  If no argument is
    supplied, the ID of the current chat will be used.

{tr}pml time <minutes>
    Specify how long (in minutes) new contacts should be logged.  A
    value of ``0`` disables temporary logging of unknown contacts.

{tr}pml list
    Show the list of users whose messages are being logged.  Temporary users
    will display how many minutes remain until they expire.

{tr}sdp on/off
    Enable or disable the self‑destructive media saver (SDP).  When
    enabled, any self‑destructive photos or videos you receive are
    automatically downloaded and re‑uploaded to the PM logger group with a
    spoiler.

{tr}sdp add <word>
    Add a trigger word.  When you reply with just this word to a
    self‑destructive media message, it will be saved regardless of whether
    SDP is currently on or off.

{tr}sdp del <word>
    Remove a trigger word from the list.

{tr}sdp list
    List all trigger words currently configured for SDP.
```

Note: this plugin relies on the SQLAlchemy session and base classes
provided by the core CatUserBot project.  It creates its own tables
for storing configuration and message mappings.  All state is stored
persistently in the bot's database.
"""

from datetime import datetime, timedelta
import os
import json
from pathlib import Path
from typing import List, Optional

from telethon import events
from telethon.tl.types import User
from telethon.tl.types import DocumentAttributeFilename
from telethon.errors import RPCError

from userbot import catub
from userbot.Config import Config
from userbot.core.managers import edit_delete, edit_or_reply
from userbot.core.logger import logging
from userbot.sql_helper import BASE, SESSION
from userbot.sql_helper.globals import addgvar, delgvar, gvarstatus

LOGS = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database models
#
# These tables store the set of monitored users, the list of current
# dialogues (used to determine whether a contact is "new"), a temporary
# list of users being monitored for only a limited time, and a mapping
# between original private messages and their logged counterparts.  The
# tables are created automatically when this plugin is loaded.

from sqlalchemy import Column, Integer, Numeric


class PMLUser(BASE):
    """Persistent table holding IDs of users explicitly monitored."""

    __tablename__ = "pml_users"
    user_id = Column(Numeric, primary_key=True)

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id


class PMLDialog(BASE):
    """Persistent table holding IDs of users currently in your dialogs.

    When PML logging is enabled with ``pml on``, this table is reset to
    reflect the current set of private chats.  New contacts are those
    whose IDs are not present in this table.
    """

    __tablename__ = "pml_dialogs"
    user_id = Column(Numeric, primary_key=True)

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id


class PMLTempUser(BASE):
    """Temporary logging entries for new contacts.

    When a new user sends a message, they are added to this table
    together with an expiry timestamp (POSIX seconds).  Once the
    timestamp passes, they will no longer be treated as monitored.
    """

    __tablename__ = "pml_temp_users"
    user_id = Column(Numeric, primary_key=True)
    expiry = Column(Integer, primary_key=True)

    def __init__(self, user_id: int, expiry: int) -> None:
        self.user_id = user_id
        self.expiry = expiry


class PMLMessageMap(BASE):
    """Mapping of original messages to logged messages in PM log group."""

    __tablename__ = "pml_message_map"
    chat_id = Column(Numeric, primary_key=True)
    message_id = Column(Integer, primary_key=True)
    logger_message_id = Column(Integer)

    def __init__(self, chat_id: int, message_id: int, logger_message_id: int) -> None:
        self.chat_id = chat_id
        self.message_id = message_id
        self.logger_message_id = logger_message_id


# Create tables if they do not already exist.
PMLUser.__table__.create(checkfirst=True)
PMLDialog.__table__.create(checkfirst=True)
PMLTempUser.__table__.create(checkfirst=True)
PMLMessageMap.__table__.create(checkfirst=True)


# ---------------------------------------------------------------------------
# Helper functions for interacting with the database


def get_all_monitored_users() -> List[int]:
    """Return a list of user IDs currently being monitored."""
    try:
        return [int(row.user_id) for row in SESSION.query(PMLUser).all()]
    finally:
        SESSION.close()


def add_monitored_user(user_id: int) -> None:
    if not SESSION.query(PMLUser).filter(PMLUser.user_id == user_id).first():
        SESSION.add(PMLUser(user_id))
        SESSION.commit()


def remove_monitored_user(user_id: int) -> None:
    if row := SESSION.query(PMLUser).filter(PMLUser.user_id == user_id).first():
        SESSION.delete(row)
        SESSION.commit()


def reset_dialogs(user_ids: List[int]) -> None:
    """Replace the list of current dialogs with the provided user IDs."""
    SESSION.query(PMLDialog).delete()
    SESSION.commit()
    for uid in user_ids:
        SESSION.add(PMLDialog(uid))
    SESSION.commit()


def is_known_dialog(user_id: int) -> bool:
    return (
        SESSION.query(PMLDialog)
        .filter(PMLDialog.user_id == user_id)
        .one_or_none()
        is not None
    )


def add_temp_user(user_id: int, expiry: int) -> None:
    # Remove any existing entry for this user
    SESSION.query(PMLTempUser).filter(PMLTempUser.user_id == user_id).delete()
    SESSION.add(PMLTempUser(user_id, expiry))
    SESSION.commit()


def is_temp_user(user_id: int) -> bool:
    """Return True if the user is temporarily monitored and not expired."""
    now = int(datetime.utcnow().timestamp())
    # Remove expired entries
    SESSION.query(PMLTempUser).filter(PMLTempUser.expiry < now).delete()
    SESSION.commit()
    return (
        SESSION.query(PMLTempUser)
        .filter(PMLTempUser.user_id == user_id)
        .one_or_none()
        is not None
    )


def add_message_mapping(chat_id: int, message_id: int, logger_id: int) -> None:
    SESSION.add(PMLMessageMap(chat_id, message_id, logger_id))
    SESSION.commit()


def get_logger_message_id(chat_id: int, message_id: int) -> Optional[int]:
    try:
        row = (
            SESSION.query(PMLMessageMap)
            .filter(
                PMLMessageMap.chat_id == chat_id,
                PMLMessageMap.message_id == message_id,
            )
            .one_or_none()
        )
        return int(row.logger_message_id) if row else None
    finally:
        SESSION.close()


def remove_message_mapping(chat_id: int, message_id: int) -> None:
    SESSION.query(PMLMessageMap).filter(
        PMLMessageMap.chat_id == chat_id,
        PMLMessageMap.message_id == message_id,
    ).delete()
    SESSION.commit()


# ---------------------------------------------------------------------------
# Additional helpers for PML and SDP functionality

def get_temp_expiry(user_id: int) -> Optional[int]:
    """Return the expiry timestamp for a temporary user or None."""
    try:
        row = (
            SESSION.query(PMLTempUser)
            .filter(PMLTempUser.user_id == user_id)
            .one_or_none()
        )
        return int(row.expiry) if row else None
    finally:
        SESSION.close()


# State management for the SDP (self‑destructive preservation) feature
def _is_sdp_enabled() -> bool:
    """Check whether the self‑destructive media saver is enabled."""
    val = gvarstatus("SDP")
    # Default is disabled if not set
    return val != "false" if val is not None else False


def _set_sdp_enabled(enabled: bool) -> None:
    """Set the SDP on/off state."""
    addgvar("SDP", "true" if enabled else "false")


def _get_sdp_words() -> List[str]:
    """Retrieve the list of trigger words for SDP from global variables."""
    val = gvarstatus("SDP_WORDS")
    if not val:
        return []
    try:
        # Stored as JSON list if possible
        words = json.loads(val)
        if isinstance(words, list):
            return [str(w) for w in words]
    except Exception:
        # Fallback: assume space‑separated string
        return [w for w in val.split()] if val else []
    return []


def _set_sdp_words(words: List[str]) -> None:
    """Persist the list of trigger words for SDP as JSON."""
    try:
        addgvar("SDP_WORDS", json.dumps(words))
    except Exception:
        # Fallback to space‑separated
        addgvar("SDP_WORDS", " ".join(words))


def _add_sdp_word(word: str) -> bool:
    """Add a word to the SDP trigger list; return True if added, False if already present."""
    word = word.strip()
    if not word:
        return False
    words = _get_sdp_words()
    if word in words:
        return False
    words.append(word)
    _set_sdp_words(words)
    return True


def _remove_sdp_word(word: str) -> bool:
    """Remove a word from the SDP trigger list; return True if removed."""
    word = word.strip()
    words = _get_sdp_words()
    if word not in words:
        return False
    words.remove(word)
    _set_sdp_words(words)
    return True


async def _save_self_destruct_media(message, client) -> Optional[str]:  # sourcery no-metrics
    """
    Download a self‑destructive media message and upload it to the PM log group with spoiler.

    Returns a string describing the result or None if not a self‑destructive media.
    """
    # Ensure the message actually contains TTL media
    media = message.media if hasattr(message, "media") else None
    ttl = None
    if media is not None:
        # Both Photo and Document may have .ttl_seconds attribute under .ttl_seconds or inside media
        ttl = getattr(media, "ttl_seconds", None)
        if ttl is None and hasattr(media, "photo"):
            ttl = getattr(media.photo, "ttl_seconds", None)
    if ttl is None:
        return None
    # Download to temporary directory
    try:
        downloads_dir = Path("/tmp/catuserbot_sdp_downloads")
        downloads_dir.mkdir(parents=True, exist_ok=True)
        file_path = await client.download_media(message, file=str(downloads_dir))
    except Exception as e:
        LOGS.warning(f"SDP: Failed to download media: {e}")
        return "Failed to download media."
    # Compose caption
    try:
        sender = await message.get_sender()
        # mention using tg://user
        first_name = sender.first_name or (sender.title if hasattr(sender, "title") else "Unknown")
        mention = f"[{first_name}](tg://user?id={sender.id})"
    except Exception:
        mention = f"ID {message.sender_id}"
    sent_at = message.date.strftime("%Y-%m-%d %H:%M:%S")
    orig_caption = message.message or ""
    caption = (
        "🔐 **Self‑destructive media saved**\n"
        f"**From:** {mention}\n"
        f"**Sent at:** `{sent_at}`\n"
    )
    if orig_caption:
        caption += f"**Original caption:** {orig_caption}"
    # Upload with spoiler
    try:
        await client.send_file(
            Config.PM_LOGGER_GROUP_ID,
            file_path,
            caption=caption,
            silent=True,
            spoiler=True,
        )
        return None
    except RPCError as e:
        LOGS.warning(f"SDP: Failed to send media: {e}")
        return "Failed to upload media."



# ---------------------------------------------------------------------------
# Plugin state management

def _is_pml_enabled() -> bool:
    """Check whether the PM logger is enabled via global variable."""
    val = gvarstatus("PML")
    return val != "false" if val is not None else False


def _set_pml_enabled(enabled: bool) -> None:
    if enabled:
        addgvar("PML", "true")
    else:
        addgvar("PML", "false")


def _get_pml_time() -> int:
    val = gvarstatus("PML_TIME")
    try:
        return int(val) if val is not None else 0
    except ValueError:
        return 0


def _set_pml_time(minutes: int) -> None:
    addgvar("PML_TIME", str(minutes))


async def _refresh_dialogs() -> None:
    """Fetch current private dialogs and update the PMLDialog table."""
    dialogs = []
    async for dialog in catub.iter_dialogs():
        # We only consider private chats (User) where the bot is a participant
        entity = dialog.entity
        if isinstance(entity, User):
            dialogs.append(entity.id)
    reset_dialogs(dialogs)


# ---------------------------------------------------------------------------
# Command handlers

plugin_category = "utils"


@catub.cat_cmd(
    pattern="pml (on|off)$",
    command=("pml", plugin_category),
    info={
        "header": "Enable or disable private‑message logging for selected users.",
        "description": (
            "When enabled, messages from monitored users are forwarded to the "
            "group set in `PM_LOGGER_GROUP_ID`.  Disabling stops monitoring."
        ),
        "usage": ["{tr}pml on", "{tr}pml off"],
    },
)
async def _(event):  # sourcery no-metrics
    """Toggle the PM logger on or off."""
    state = event.pattern_match.group(1)
    if state == "on":
        if _is_pml_enabled():
            return await edit_delete(event, "`PML logging is already enabled.`", 5)
        _set_pml_enabled(True)
        await _refresh_dialogs()
        return await edit_delete(
            event,
            "`PML logging enabled.  Current dialogues have been saved.`",
            5,
        )
    else:
        if not _is_pml_enabled():
            return await edit_delete(event, "`PML logging is already disabled.`", 5)
        _set_pml_enabled(False)
        return await edit_delete(event, "`PML logging disabled.`", 5)


@catub.cat_cmd(
    pattern="pml add(?:\s|$)(.*)",
    command=("pmladd", plugin_category),
    info={
        "header": "Add a user to the PML monitored list.",
        "description": "Forward messages from the specified user to the PM log group.",
        "usage": ["{tr}pml add <username|userid>", "{tr}pml add"],
    },
)
async def _(event):  # sourcery no-metrics
    """Add a user to the monitored list."""
    user_str = event.pattern_match.group(1).strip()
    if not user_str:
        # Default to current chat
        user_id = event.chat_id
    else:
        try:
            entity = await event.client.get_entity(user_str)
            user_id = entity.id
        except Exception:
            return await edit_delete(event, "`Unable to resolve user.`", 5)
    add_monitored_user(int(user_id))
    return await edit_delete(
        event,
        f"`User {user_id} added to PML monitored list.`",
        5,
    )


@catub.cat_cmd(
    pattern="pml del(?:\s|$)(.*)",
    command=("pmldel", plugin_category),
    info={
        "header": "Remove a user from the PML monitored list.",
        "description": "Stop logging messages for the specified user.",
        "usage": ["{tr}pml del <username|userid>", "{tr}pml del"],
    },
)
async def _(event):  # sourcery no-metrics
    """Remove a user from the monitored list."""
    user_str = event.pattern_match.group(1).strip()
    if not user_str:
        user_id = event.chat_id
    else:
        try:
            entity = await event.client.get_entity(user_str)
            user_id = entity.id
        except Exception:
            return await edit_delete(event, "`Unable to resolve user.`", 5)
    remove_monitored_user(int(user_id))
    return await edit_delete(
        event,
        f"`User {user_id} removed from PML monitored list.`",
        5,
    )


@catub.cat_cmd(
    pattern="pml time(?:\s|$)(\d+)$",
    command=("pmltime", plugin_category),
    info={
        "header": "Set temporary logging duration for new contacts.",
        "description": (
            "Define for how many minutes new contacts should be monitored.  A"
            " value of 0 disables temporary logging."
        ),
        "usage": ["{tr}pml time 60", "{tr}pml time 0"],
    },
)
async def _(event):  # sourcery no-metrics
    """Adjust the duration for temporary logging of new contacts."""
    minutes = int(event.pattern_match.group(1))
    _set_pml_time(minutes)
    if minutes == 0:
        return await edit_delete(
            event,
            "`New contacts will no longer be logged temporarily.`",
            5,
        )
    return await edit_delete(
        event,
        f"`New contacts will be logged for {minutes} minutes.`",
        5,
    )


@catub.cat_cmd(
    pattern="pml list$",
    command=("pmllist", plugin_category),
    info={
        "header": "Show the list of users monitored by PML.",
        "description": (
            "Display all users whose private messages are currently being logged. "
            "Temporary users will also display the remaining time."
        ),
        "usage": ["{tr}pml list"],
    },
)
async def _(event):  # sourcery no-metrics
    """List monitored users along with remaining temporary logging time."""
    users = get_all_monitored_users()
    if not users:
        return await edit_or_reply(event, "`No users are currently being monitored.`")
    lines = []
    now = int(datetime.utcnow().timestamp())
    for uid in users:
        try:
            entity = await event.client.get_entity(uid)
            name = entity.first_name or (entity.title if hasattr(entity, "title") else str(uid))
            mention = f"[{name}](tg://user?id={uid})"
        except Exception:
            mention = f"ID {uid}"
        expiry = get_temp_expiry(uid)
        if expiry and expiry > now:
            remaining = expiry - now
            # Show in minutes, rounding up
            mins = (remaining + 59) // 60
            lines.append(f"• {mention} (temporary, {mins}m left)")
        else:
            lines.append(f"• {mention}")
    message = "**PML monitored users:**\n" + "\n".join(lines)
    return await edit_or_reply(event, message)


@catub.cat_cmd(
    pattern="sdp (on|off)$",
    command=("sdp", plugin_category),
    info={
        "header": "Toggle saving of self‑destructive media.",
        "description": (
            "When enabled, any self‑destructive photos/videos you receive will be saved to the PM log group."
        ),
        "usage": ["{tr}sdp on", "{tr}sdp off"],
    },
)
async def _(event):  # sourcery no-metrics
    """Enable or disable saving of self‑destructive media."""
    state = event.pattern_match.group(1)
    if state == "on":
        if _is_sdp_enabled():
            return await edit_delete(event, "`SDP is already enabled.`", 5)
        _set_sdp_enabled(True)
        return await edit_delete(event, "`Self‑destructive media saving enabled.`", 5)
    else:
        if not _is_sdp_enabled():
            return await edit_delete(event, "`SDP is already disabled.`", 5)
        _set_sdp_enabled(False)
        return await edit_delete(event, "`Self‑destructive media saving disabled.`", 5)


@catub.cat_cmd(
    pattern="sdp add(?:\s|$)(.+)",
    command=("sdpadd", plugin_category),
    info={
        "header": "Add a trigger word for saving self‑destructive media.",
        "description": (
            "When you reply with just this word to a self‑destructive media, it will be saved even if SDP is off."
        ),
        "usage": ["{tr}sdp add wait"],
    },
)
async def _(event):  # sourcery no-metrics
    word = event.pattern_match.group(1).strip()
    if not word:
        return await edit_delete(event, "`Please specify a word to add.`", 5)
    if _add_sdp_word(word):
        return await edit_delete(event, f"`Added '{word}' to SDP trigger words.`", 5)
    else:
        return await edit_delete(event, f"`'{word}' is already in SDP trigger words.`", 5)


@catub.cat_cmd(
    pattern="sdp del(?:\s|$)(.+)",
    command=("sdpdel", plugin_category),
    info={
        "header": "Remove a trigger word for saving self‑destructive media.",
        "description": "Stop using this word to manually save self‑destructive media.",
        "usage": ["{tr}sdp del wait"],
    },
)
async def _(event):  # sourcery no-metrics
    word = event.pattern_match.group(1).strip()
    if not word:
        return await edit_delete(event, "`Please specify a word to remove.`", 5)
    if _remove_sdp_word(word):
        return await edit_delete(event, f"`Removed '{word}' from SDP trigger words.`", 5)
    else:
        return await edit_delete(event, f"`'{word}' was not found in SDP trigger words.`", 5)


@catub.cat_cmd(
    pattern="sdp list$",
    command=("sdplist", plugin_category),
    info={
        "header": "List SDP trigger words.",
        "description": "Show all words that trigger saving of self‑destructive media when replied.",
        "usage": ["{tr}sdp list"],
    },
)
async def _(event):  # sourcery no-metrics
    words = _get_sdp_words()
    if not words:
        return await edit_or_reply(event, "`No SDP trigger words have been set.`")
    items = "\n".join(f"• {w}" for w in words)
    return await edit_or_reply(event, f"**SDP trigger words:**\n{items}")


# ---------------------------------------------------------------------------
# Message handlers


@catub.on(events.NewMessage(incoming=True))
async def _pml_incoming_handler(event):  # sourcery no-metrics
    """Forward messages from monitored users or temporary users to log group."""
    # Only consider private messages
    if not event.is_private or event.sender_id is None:
        return
    # Check plugin state
    if not _is_pml_enabled() or Config.PM_LOGGER_GROUP_ID == -100:
        return
    user_id = event.sender_id
    monitored_users = get_all_monitored_users()
    pml_time = _get_pml_time()
    # Determine if user should be logged
    should_log = False
    # If explicitly monitored
    if user_id in monitored_users:
        should_log = True
    # If not known dialog and pml_time > 0 and not yet temporary
    elif pml_time > 0:
        if not is_known_dialog(user_id) and not is_temp_user(user_id):
            expiry = int((datetime.utcnow() + timedelta(minutes=pml_time)).timestamp())
            add_temp_user(user_id, expiry)
            should_log = True
        elif is_temp_user(user_id):
            should_log = True
    if not should_log:
        return
    try:
        # Forward the incoming message to the PM logger group
        fwd_msg = await event.client.forward_messages(
            Config.PM_LOGGER_GROUP_ID, event.message, silent=True
        )
        # fwd_msg may be a list or a single message
        if isinstance(fwd_msg, list):
            fwd_msg = fwd_msg[0]
        add_message_mapping(user_id, event.message.id, fwd_msg.id)
    except Exception as e:
        LOGS.warning(f"PML forward failed: {e}")


@catub.on(events.MessageDeleted())
async def _pml_deleted_handler(event):  # sourcery no-metrics
    """Notify owner when a monitored message gets deleted."""
    if not _is_pml_enabled() or Config.PM_LOGGER_GROUP_ID == -100:
        return
    # event.deleted_ids may contain multiple message IDs
    for msg_id in event.deleted_ids:
        logger_id = get_logger_message_id(event.chat_id, msg_id)
        if logger_id:
            # Compose a notification.  Mention the user using a telegra.ph link
            try:
                sender = await catub.get_entity(event.chat_id)
                mention = f"{sender.first_name} (ID: {sender.id})"
            except Exception:
                mention = f"ID {event.chat_id}"
            notif = (
                f"🗑️ A message from {mention} was deleted in your private chat."
            )
            try:
                # Reply to the forwarded message in the PM log group to
                # highlight which message was removed.
                await catub.send_message(
                    Config.PM_LOGGER_GROUP_ID,
                    notif,
                    reply_to=logger_id,
                )
            except Exception as e:
                LOGS.warning(f"PML delete notification failed: {e}")
            # Remove mapping to avoid duplicate notifications
            remove_message_mapping(event.chat_id, msg_id)


# ---------------------------------------------------------------------------
# Self‑destructive media handlers

@catub.on(events.NewMessage(incoming=True))
async def _sdp_auto_handler(event):  # sourcery no-metrics
    """Automatically save self‑destructive media when SDP is enabled."""
    # Skip messages from the PM log group itself
    if event.chat_id == Config.PM_LOGGER_GROUP_ID:
        return
    # Only act on incoming messages that contain media with a TTL
    if not _is_sdp_enabled():
        return
    msg = event.message
    if not msg:
        return
    # Ensure there is media and TTL
    media = getattr(msg, "media", None)
    ttl = None
    if media is not None:
        ttl = getattr(media, "ttl_seconds", None)
        if ttl is None and hasattr(media, "photo"):
            ttl = getattr(media.photo, "ttl_seconds", None)
    if ttl is None:
        return
    # Save the media
    await _save_self_destruct_media(msg, event.client)


@catub.on(events.NewMessage(outgoing=True))
async def _sdp_manual_handler(event):  # sourcery no-metrics
    """Manually trigger saving of self‑destructive media using a trigger word."""
    # We only handle simple messages (no commands) sent by the user
    if not event.is_reply:
        return
    text = (event.raw_text or "").strip()
    if not text:
        return
    # Message must not start with command prefixes
    if text.startswith(('.', '/', '!', '#')):
        return
    words = _get_sdp_words()
    if not words:
        return
    if text not in words:
        return
    # Fetch the message being replied to
    try:
        reply_msg = await event.get_reply_message()
    except Exception:
        return
    if not reply_msg:
        return
    # Save regardless of SDP state
    await _save_self_destruct_media(reply_msg, event.client)
