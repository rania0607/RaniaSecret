import os
from dotenv import load_dotenv
import pymysql

env_path = os.path.join(os.getcwd(), 'backend', '.env')
load_dotenv(env_path)

conn = pymysql.connect(
    host=os.getenv('DB_HOST'),
    user=os.getenv('DB_USER'),
    password=os.getenv('DB_PASSWORD'),
    database=os.getenv('DB_NAME'),
    port=int(os.getenv('DB_PORT', 3306)),
    ssl={'verify_cert': False}
)

try:
    with conn.cursor() as cur:
        # 1. Add image_path to evenements
        cur.execute("SHOW COLUMNS FROM evenements LIKE 'image_path'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE evenements ADD COLUMN image_path VARCHAR(255) NULL")
            print("Added image_path to evenements")

        # 2. Resolve users table duplication
        cur.execute("SHOW COLUMNS FROM users LIKE 'message_count'")
        if cur.fetchone():
            cur.execute("UPDATE users SET msg_count = message_count WHERE msg_count = 0 AND message_count > 0")
            cur.execute("ALTER TABLE users DROP COLUMN message_count")
            print("Merged message_count into msg_count and dropped column")

        # 3. Ensure pack related columns exist
        columns_to_add = {
            'payment_status': "VARCHAR(50) DEFAULT 'Unpaid'",
            'pack_activation_date': "DATETIME NULL",
            'pack_expiry_date': "DATETIME NULL",
            'payment_ref': "VARCHAR(255) NULL",
            'admin_notes': "TEXT NULL",
            'is_active': "BOOLEAN DEFAULT TRUE"
        }
        
        for col, definition in columns_to_add.items():
            cur.execute(f"SHOW COLUMNS FROM users LIKE '{col}'")
            if not cur.fetchone():
                cur.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
                print(f"Added column {col} to users")

        # 4. Add type_attraction to attractions
        cur.execute("SHOW COLUMNS FROM attractions LIKE 'type_attraction'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE attractions ADD COLUMN type_attraction VARCHAR(60) DEFAULT NULL")
            print("Added type_attraction to attractions")

        # ========================================================
        # 5. NEW: Profile fields for recommendations (SAFE ADD)
        # ========================================================
        rec_columns = {
            'age': "INT NULL DEFAULT NULL",
            'travel_type': "VARCHAR(50) NULL DEFAULT 'Solo'",
            'children_count': "INT NULL DEFAULT 0"
        }
        for col, definition in rec_columns.items():
            cur.execute(f"SHOW COLUMNS FROM users LIKE '{col}'")
            if not cur.fetchone():
                cur.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
                print(f"Added column {col} to users")

        # ========================================================
        # 6. NEW: user_interactions table
        # ========================================================
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_interactions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT NOT NULL,
                item_type VARCHAR(50) NOT NULL,
                item_id INT NOT NULL,
                interaction_type VARCHAR(50) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_user_item (user_id, item_type, item_id),
                INDEX idx_user_interaction_time (user_id, created_at),
                CONSTRAINT fk_interactions_user 
                    FOREIGN KEY (user_id) REFERENCES users(id) 
                    ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """)
        print("Ensured user_interactions table exists")

        # ========================================================
        # 7. NEW: notifications table
        # ========================================================
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT NOT NULL,
                title VARCHAR(255) NOT NULL,
                message TEXT NOT NULL,
                item_type VARCHAR(50) DEFAULT NULL,
                item_id INT DEFAULT NULL,
                is_read TINYINT(1) DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_user_unread (user_id, is_read),
                CONSTRAINT fk_notifications_user 
                    FOREIGN KEY (user_id) REFERENCES users(id) 
                    ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """)
        print("Ensured notifications table exists")

    conn.commit()
    print("Database normalization and recommendation setup complete.")
except Exception as e:
    print(f"Error during migration: {e}")
finally:
    conn.close()