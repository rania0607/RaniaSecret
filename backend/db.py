"""Shared DB config so app.py and admin_routes.py don't duplicate credentials."""

import os

import pymysql
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
DB_CONFIG = {
    "host":        os.getenv("DB_HOST", "localhost"),
    "port":        int(os.getenv("DB_PORT", 3306)),
    "user":        os.getenv("DB_USER", "root"),
    "password":    os.getenv("DB_PASSWORD", "123456789"),
    "db":          os.getenv("DB_NAME", "tourism_maroc"),
    "cursorclass": pymysql.cursors.DictCursor,
    "charset":     "utf8mb4",
    "ssl":         {"verify_cert": False},
}


def get_connection():
    return pymysql.connect(**DB_CONFIG)
