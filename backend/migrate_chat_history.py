"""Create persistent chat tables and import existing chat messages."""

import logging

import pymysql
from db import DB_CONFIG

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chat_migration")


def migrate(connection):
    with connection.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT NOT NULL,
                title VARCHAR(120) NOT NULL DEFAULT 'Nouvelle conversation',
                is_legacy_import TINYINT(1) NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_conversations_user_updated (user_id, updated_at, id),
                CONSTRAINT fk_conversations_user
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                conversation_id INT NOT NULL,
                role ENUM('user', 'assistant') NOT NULL,
                content LONGTEXT NOT NULL,
                legacy_message_id BIGINT NULL UNIQUE,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_messages_conversation_id (conversation_id, id),
                CONSTRAINT fk_messages_conversation
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
                    ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """)
        connection.commit()

        cur.execute("SHOW TABLES LIKE 'chat_messages'")
        if not cur.fetchone():
            logger.info("No legacy chat_messages table found; schema migration complete.")
            return

        cur.execute("""
            SELECT id, user_id, role, message, created_at
            FROM chat_messages
            WHERE role IN ('user', 'bot', 'assistant')
            ORDER BY user_id, created_at, id
        """)
        legacy_messages = cur.fetchall()
        conversations = {}
        imported = 0

        for old_message in legacy_messages:
            user_id = old_message["user_id"]
            if user_id not in conversations:
                cur.execute(
                    """SELECT id FROM conversations
                       WHERE user_id = %s AND is_legacy_import = 1 LIMIT 1""",
                    (user_id,)
                )
                existing = cur.fetchone()
                if existing:
                    conversation_id = existing["id"]
                else:
                    cur.execute(
                        """INSERT INTO conversations (user_id, title, is_legacy_import)
                           VALUES (%s, %s, 1)""",
                        (user_id, "Historique précédent")
                    )
                    conversation_id = cur.lastrowid
                conversations[user_id] = conversation_id

            role = "assistant" if old_message["role"] in ("bot", "assistant") else "user"
            cur.execute(
                """INSERT IGNORE INTO messages
                   (conversation_id, role, content, legacy_message_id, created_at)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    conversations[user_id],
                    role,
                    old_message["message"],
                    old_message["id"],
                    old_message["created_at"],
                )
            )
            imported += cur.rowcount

        connection.commit()
        logger.info("Chat migration complete; imported %s existing messages.", imported)


def main():
    config = dict(DB_CONFIG)
    config["database"] = config.pop("db")
    connection = pymysql.connect(**config)
    try:
        migrate(connection)
    except Exception:
        connection.rollback()
        logger.exception("Chat migration failed")
        raise
    finally:
        connection.close()


if __name__ == "__main__":
    main()
