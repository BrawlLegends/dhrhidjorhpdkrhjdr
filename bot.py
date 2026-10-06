import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "nawron").strip().lstrip("@").lower()
ADMIN_ID_VALUE = os.environ.get("ADMIN_ID", "").strip()
ADMIN_ID = int(ADMIN_ID_VALUE) if ADMIN_ID_VALUE else None
DB_PATH = Path(os.environ.get("DATABASE_PATH", "support_bot.sqlite3"))
PAGE_SIZE = 8
HISTORY_SIZE = 12

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
)
log = logging.getLogger("support-bot")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class HealthCheckHandler(BaseHTTPRequestHandler):
    def _respond(self, include_body: bool) -> None:
        if self.path.split("?", 1)[0] not in {"/", "/healthz"}:
            body = b"Not found\n"
            self.send_response(404)
        else:
            body = b"Telegram support bot is running\n"
            self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def do_GET(self) -> None:
        self._respond(include_body=True)

    def do_HEAD(self) -> None:
        self._respond(include_body=False)

    def log_message(self, format: str, *args) -> None:
        # Avoid logging every platform health-check request.
        return


def start_health_server(port: int | None = None) -> ThreadingHTTPServer:
    """Bind Render's assigned PORT while Telegram polling runs in the main thread."""
    listen_port = int(port if port is not None else os.environ.get("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", listen_port), HealthCheckHandler)
    thread = Thread(target=server.serve_forever, name="health-http", daemon=True)
    thread.start()
    log.info("Health-check server listening on 0.0.0.0:%s", server.server_port)
    return server


@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                chat_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'closed', 'blocked')),
                pinned INTEGER NOT NULL DEFAULT 0,
                unread INTEGER NOT NULL DEFAULT 0,
                last_message_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                direction TEXT NOT NULL CHECK (direction IN ('user', 'admin')),
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_chat_id
                ON messages(chat_id, id DESC);
            """
        )


def is_admin_user(user) -> bool:
    if user is None:
        return False
    if ADMIN_ID is not None:
        return user.id == ADMIN_ID
    return (user.username or "").lower() == ADMIN_USERNAME


def remember_admin_id(user_id: int) -> None:
    if ADMIN_ID is not None:
        return
    with db() as connection:
        connection.execute(
            """
            INSERT INTO settings (key, value) VALUES ('admin_chat_id', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(user_id),),
        )


def get_admin_chat_id() -> int | None:
    if ADMIN_ID is not None:
        return ADMIN_ID
    with db() as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = 'admin_chat_id'"
        ).fetchone()
    return int(row["value"]) if row else None


async def is_admin_update(update: Update) -> bool:
    return bool(
        update.effective_user
        and is_admin_user(update.effective_user)
        and update.effective_chat
        and update.effective_chat.type == ChatType.PRIVATE
    )


def display_name(username: str | None, full_name: str) -> str:
    return f"@{username}" if username else full_name


def record_user_message(update: Update) -> str:
    user = update.effective_user
    message = update.effective_message
    chat_id = update.effective_chat.id
    username = user.username if user else None
    full_name = user.full_name if user else "Пользователь"
    timestamp = now()
    with db() as connection:
        row = connection.execute(
            "SELECT status FROM conversations WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        status = row["status"] if row else "open"
        connection.execute(
            """
            INSERT INTO conversations
                (chat_id, username, full_name, status, unread, last_message_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                unread = CASE WHEN conversations.status = 'open'
                    THEN conversations.unread + 1 ELSE conversations.unread END,
                last_message_at = excluded.last_message_at
            """,
            (chat_id, username, full_name, status, 1 if status == "open" else 0, timestamp),
        )
        connection.execute(
            """
            INSERT INTO messages
                (chat_id, direction, source_chat_id, source_message_id, created_at)
            VALUES (?, 'user', ?, ?, ?)
            """,
            (chat_id, chat_id, message.message_id, timestamp),
        )
    return status


def record_admin_message(chat_id: int, admin_chat_id: int, message_id: int) -> None:
    timestamp = now()
    with db() as connection:
        connection.execute(
            """
            INSERT INTO messages
                (chat_id, direction, source_chat_id, source_message_id, created_at)
            VALUES (?, 'admin', ?, ?, ?)
            """,
            (chat_id, admin_chat_id, message_id, timestamp),
        )
        connection.execute(
            "UPDATE conversations SET last_message_at = ? WHERE chat_id = ?",
            (timestamp, chat_id),
        )


def list_conversations(status: str, page: int):
    offset = page * PAGE_SIZE
    with db() as connection:
        total = connection.execute(
            "SELECT COUNT(*) FROM conversations WHERE status = ?", (status,)
        ).fetchone()[0]
        rows = connection.execute(
            """
            SELECT chat_id, username, full_name, pinned, unread, last_message_at
            FROM conversations
            WHERE status = ?
            ORDER BY pinned DESC, last_message_at DESC
            LIMIT ? OFFSET ?
            """,
            (status, PAGE_SIZE, offset),
        ).fetchall()
    return rows, total


def get_conversation(chat_id: int):
    with db() as connection:
        row = connection.execute(
            "SELECT * FROM conversations WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row


def status_label(status: str) -> str:
    return {"open": "🟢 Открытые", "closed": "✅ Закрытые", "blocked": "⛔ Заблокированные"}[status]


def panel_keyboard(status: str = "open", page: int = 0) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton("🟢 Открытые", callback_data="list:open:0"),
        InlineKeyboardButton("✅ Закрытые", callback_data="list:closed:0"),
        InlineKeyboardButton("⛔ Блок", callback_data="list:blocked:0"),
    ]
    return InlineKeyboardMarkup([buttons])


def conversation_keyboard(chat_id: int, status: str, pinned: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✍️ Ответить", callback_data=f"reply:{chat_id}")],
        [
            InlineKeyboardButton(
                "📌 Открепить" if pinned else "📌 Закрепить",
                callback_data=f"pin:{chat_id}",
            )
        ],
    ]
    if status == "open":
        rows.append(
            [
                InlineKeyboardButton("✅ Закрыть", callback_data=f"close:{chat_id}"),
                InlineKeyboardButton("⛔ Заблокировать", callback_data=f"ban:{chat_id}"),
            ]
        )
    elif status == "closed":
        rows.append(
            [
                InlineKeyboardButton("↩️ Открыть снова", callback_data=f"reopen:{chat_id}"),
                InlineKeyboardButton("⛔ Заблокировать", callback_data=f"ban:{chat_id}"),
            ]
        )
    else:
        rows.append([InlineKeyboardButton("✅ Разблокировать", callback_data=f"unban:{chat_id}")])
    rows.append([InlineKeyboardButton("⬅️ К списку чатов", callback_data="back")])
    return InlineKeyboardMarkup(rows)


async def send_panel(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    status: str = "open",
    page: int = 0,
) -> None:
    rows, total = list_conversations(status, page)
    keyboard_rows = []
    for row in rows:
        title = display_name(row["username"], row["full_name"])
        marker = "📌 " if row["pinned"] else ""
        unread = f" · новое: {row['unread']}" if row["unread"] else ""
        label = f"{marker}{title}{unread}"[:60]
        keyboard_rows.append(
            [InlineKeyboardButton(label, callback_data=f"open:{row['chat_id']}")]
        )

    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"list:{status}:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"list:{status}:{page + 1}"))
    keyboard_rows.append(nav)
    keyboard_rows.extend(panel_keyboard(status, page).inline_keyboard)

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"<b>Входящие · {escape(status_label(status))}</b>\n"
        f"Диалогов: {total}. Выберите чат:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard_rows),
    )


async def show_conversation(
    context: ContextTypes.DEFAULT_TYPE,
    admin_chat_id: int,
    chat_id: int,
) -> None:
    row = get_conversation(chat_id)
    if not row:
        await context.bot.send_message(admin_chat_id, "Диалог не найден.")
        return

    with db() as connection:
        connection.execute(
            "UPDATE conversations SET unread = 0 WHERE chat_id = ?", (chat_id,)
        )
        messages = connection.execute(
            """
            SELECT direction, source_chat_id, source_message_id
            FROM messages WHERE chat_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (chat_id, HISTORY_SIZE),
        ).fetchall()
    messages = list(reversed(messages))

    title = display_name(row["username"], row["full_name"])
    await context.bot.send_message(
        chat_id=admin_chat_id,
        text=(
            f"<b>{escape(title)}</b>\n"
            f"Статус: {escape(row['status'])} · "
            f"{'закреплён' if row['pinned'] else 'обычный'}\n"
            f"ID: <code>{chat_id}</code>\n"
            f"Последние сообщения (до {HISTORY_SIZE}):"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=conversation_keyboard(chat_id, row["status"], bool(row["pinned"])),
    )
    for item in messages:
        try:
            await context.bot.copy_message(
                chat_id=admin_chat_id,
                from_chat_id=item["source_chat_id"],
                message_id=item["source_message_id"],
            )
        except TelegramError as error:
            log.warning("Cannot copy history message %s: %s", item["source_message_id"], error)
            await context.bot.send_message(
                admin_chat_id,
                "Одно из старых сообщений недоступно для повторного показа.",
            )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await is_admin_update(update):
        remember_admin_id(update.effective_user.id)
        await update.effective_message.reply_text(
            "Панель поддержки готова. Команда /panel — список диалогов; /id — ваш Telegram ID."
        )
    else:
        await update.effective_message.reply_text(
            "Здравствуйте! Напишите сообщение — оно попадёт оператору поддержки. "
            "Можно отправить текст, фото, видео, голосовое или видеосообщение."
        )


async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin_update(update):
        await update.effective_message.reply_text("Команда доступна только администратору.")
        return
    remember_admin_id(update.effective_user.id)
    context.user_data.pop("reply_to", None)
    await send_panel(context, update.effective_chat.id)


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_admin_update(update):
        return
    remember_admin_id(update.effective_user.id)
    await update.effective_message.reply_text(
        f"Ваш Telegram ID: {update.effective_user.id}\n"
        "Для надёжной проверки доступа задайте его как ADMIN_ID в Secrets."
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_admin_user(query.from_user) or query.message.chat.type != ChatType.PRIVATE:
        await query.answer("Доступ запрещён.", show_alert=True)
        return
    await query.answer()
    data = query.data or ""

    if data == "noop":
        return
    if data == "back":
        context.user_data.pop("reply_to", None)
        await send_panel(context, query.message.chat.id)
        return
    if data.startswith("list:"):
        _, status, page_text = data.split(":", 2)
        if status not in {"open", "closed", "blocked"}:
            return
        await send_panel(context, query.message.chat.id, status, max(0, int(page_text)))
        return
    if ":" not in data:
        return

    action, raw_chat_id = data.split(":", 1)
    try:
        user_chat_id = int(raw_chat_id)
    except ValueError:
        return
    row = get_conversation(user_chat_id)
    if not row:
        await context.bot.send_message(query.message.chat.id, "Диалог не найден.")
        return
    if action == "open":
        context.user_data.pop("reply_to", None)
        await show_conversation(context, query.message.chat.id, user_chat_id)
        return

    with db() as connection:
        if action == "reply":
            if row["status"] != "open":
                await context.bot.send_message(
                    query.message.chat.id,
                    "Сначала откройте диалог повторно, чтобы отвечать.",
                )
                return
            context.user_data["reply_to"] = user_chat_id
            await context.bot.send_message(
                query.message.chat.id,
                f"Режим ответа для {display_name(row['username'], row['full_name'])}. "
                "Отправьте текст или медиа. /cancel — выйти из режима ответа.",
            )
            return
        if action == "pin":
            connection.execute(
                "UPDATE conversations SET pinned = ? WHERE chat_id = ?",
                (0 if row["pinned"] else 1, user_chat_id),
            )
        elif action == "close":
            connection.execute(
                "UPDATE conversations SET status = 'closed' WHERE chat_id = ?",
                (user_chat_id,),
            )
            context.user_data.pop("reply_to", None)
        elif action == "reopen":
            connection.execute(
                "UPDATE conversations SET status = 'open' WHERE chat_id = ?",
                (user_chat_id,),
            )
        elif action == "ban":
            connection.execute(
                "UPDATE conversations SET status = 'blocked' WHERE chat_id = ?",
                (user_chat_id,),
            )
            context.user_data.pop("reply_to", None)
        elif action == "unban":
            connection.execute(
                "UPDATE conversations SET status = 'open' WHERE chat_id = ?",
                (user_chat_id,),
            )
        else:
            return

    if action == "close":
        try:
            await context.bot.send_message(
                user_chat_id,
                "Эта переписка закрыта оператором. Новые сообщения не будут доставлены в поддержку.",
            )
        except TelegramError:
            pass
    elif action == "ban":
        try:
            await context.bot.send_message(
                user_chat_id,
                "Возможность отправлять сообщения в поддержку отключена.",
            )
        except TelegramError:
            pass

    await show_conversation(context, query.message.chat.id, user_chat_id)


async def cancel_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await is_admin_update(update):
        context.user_data.pop("reply_to", None)
        await update.effective_message.reply_text("Режим ответа завершён.")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or update.effective_chat is None:
        return

    if await is_admin_update(update):
        target_chat_id = context.user_data.get("reply_to")
        if not target_chat_id:
            await message.reply_text("Выберите диалог в /panel, затем нажмите «Ответить».")
            return
        conversation = get_conversation(target_chat_id)
        if not conversation or conversation["status"] != "open":
            context.user_data.pop("reply_to", None)
            await message.reply_text("Этот диалог закрыт или недоступен. Выберите другой в /panel.")
            return
        try:
            await context.bot.copy_message(
                chat_id=target_chat_id,
                from_chat_id=update.effective_chat.id,
                message_id=message.message_id,
            )
            record_admin_message(target_chat_id, update.effective_chat.id, message.message_id)
            await message.reply_text("Ответ отправлен.")
        except TelegramError as error:
            log.warning("Could not send admin reply: %s", error)
            await message.reply_text("Не удалось отправить сообщение пользователю.")
        return

    if update.effective_chat.type != ChatType.PRIVATE:
        return
    status = record_user_message(update)
    if status == "blocked":
        try:
            await message.reply_text("Этот диалог заблокирован.")
        except TelegramError:
            pass
        return
    if status == "closed":
        try:
            await message.reply_text(
                "Эта переписка закрыта. Новые сообщения не доставляются оператору."
            )
        except TelegramError:
            pass
        return

    admin_chat_id = get_admin_chat_id()
    if admin_chat_id is None:
        await message.reply_text(
            "Сообщение сохранено, но администратор ещё не запускал бота. "
            "Пусть @nawron откроет бота и отправит /start."
        )
        return
    try:
        title = display_name(
            update.effective_user.username,
            update.effective_user.full_name,
        )
        await context.bot.send_message(
            chat_id=admin_chat_id,
            text=(
                f"📩 Новое сообщение от <b>{escape(title)}</b>\n"
                "Откройте диалог из списка:"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Открыть диалог", callback_data=f"open:{update.effective_chat.id}")]]
            ),
        )
        await context.bot.copy_message(
            chat_id=admin_chat_id,
            from_chat_id=update.effective_chat.id,
            message_id=message.message_id,
        )
    except TelegramError as error:
        log.error("Could not notify admin. Configure ADMIN_ID: %s", error)
        try:
            await message.reply_text(
                "Сообщение принято, но оператор временно недоступен. Попробуйте позже."
            )
        except TelegramError:
            pass


def main() -> None:
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in the environment before starting.")
    init_db()
    start_health_server()
    application: Application = ApplicationBuilder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("panel", panel))
    application.add_handler(CommandHandler("id", show_id))
    application.add_handler(CommandHandler("cancel", cancel_reply))
    application.add_handler(CallbackQueryHandler(on_callback))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    log.info("Support bot is starting in polling mode.")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
