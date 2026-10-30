"""
Trippy — Guide Touristique du Maroc
Avec inscription/connexion par code email (vérification)
"""

from flask import Flask, render_template, abort, request, jsonify, session, send_from_directory
from flask_mail import Mail, Message
from werkzeug.exceptions import HTTPException
import pymysql
import requests
import os
import hashlib
import random
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import unquote
from dotenv import load_dotenv
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from recommendation_engine import (
    get_recommendations,
    get_user_profile,
    generate_personalized_notification
)

env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
load_dotenv(dotenv_path=env_path)

try:
    from rag_engine import rag_retrieve_and_augment, localize_fallback_response
except ImportError:
    def rag_retrieve_and_augment(message, limit_info=None, history=None, ui_lang=None):
        return {"system_prompt": "Expert Maroc", "user_prompt": message,
                "has_context": False, "ville_detec": None, "intent_detec": None}
    def localize_fallback_response(text, message, ui_lang=None):
        return text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# app.py est dans /backend — frontend est au niveau parent (root)
frontend_dir = os.path.join(BASE_DIR, '..', 'frontend')
template_dir = os.path.join(BASE_DIR, '..', 'frontend', 'templates')
static_dir = os.path.abspath(
    os.path.join(BASE_DIR, '..', 'frontend', 'static')
)


from werkzeug.middleware.proxy_fix import ProxyFix
from flask_cors import CORS


app = Flask(
    __name__,
    template_folder=template_dir,
    static_folder=static_dir,
    static_url_path='/static'
)
app.logger.setLevel("INFO")


# Fix pour Railway (Reverse Proxy) : assure que Flask comprend que l'on est en HTTPS
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Autorise les requêtes Cross-Origin et les cookies de session
CORS(app, supports_credentials=True)

app.secret_key = os.getenv('SECRET_KEY', 'trippy_maroc_secret_key_2026')
# Configurer les sessions pour qu'elles fonctionnent derrière le proxy HTTPS de Railway
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# En local (HTTP), SESSION_COOKIE_SECURE doit être False, sinon le navigateur rejette les cookies de session.
app.config['SESSION_COOKIE_SECURE'] = os.getenv('FLASK_ENV') == 'production'
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from admin_routes import admin_bp, _ensure_google_maps_link_column
app.register_blueprint(admin_bp)

# ============================================================
# CONFIGURATION EMAIL
# ============================================================
app.config['MAIL_SERVER'] = os.getenv('MAIL_SERVER')
app.config['MAIL_PORT'] = int(os.getenv('MAIL_PORT', 587))
app.config['MAIL_USERNAME'] = os.getenv('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.getenv('MAIL_PASSWORD')
app.config['MAIL_USE_TLS'] = os.getenv('MAIL_USE_TLS', 'True').lower() == 'true'
app.config['MAIL_USE_SSL'] = os.getenv('MAIL_USE_SSL', 'False').lower() == 'true'
app.config['MAIL_DEFAULT_SENDER'] = app.config['MAIL_USERNAME'] or 'noreply@morocco-secrets.com'

mail = Mail(app)

# ============================================================
# CLOUDINARY CDN
# ============================================================
CLOUDINARY_CLOUD_NAME = os.getenv('CLOUDINARY_CLOUD_NAME', 'darytb39v')
CLOUDINARY_BASE = f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/image/upload/f_auto,q_auto/"
CLOUDINARY_VIDEO_BASE = f"https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/video/upload/f_auto,q_auto/"
CDN_URL = CLOUDINARY_BASE
LOGO_URL = f"{CLOUDINARY_BASE}morocco-secrets/isLogo"
VIDEO_URL = f"{CLOUDINARY_VIDEO_BASE}morocco-secrets/maroc-hero.mp4"

@app.context_processor
def inject_cloudinary():
    return dict(cdn_url=CDN_URL, logo_url=LOGO_URL, video_url=VIDEO_URL)


GROQ_API_KEY = (os.getenv('GROQ_API_KEY') or '').strip()
GROQ_MODEL   = "openai/gpt-oss-120b"

from db import DB_CONFIG

TIER_LIMITS = {
    'explorer': 50,
    'standard': float('inf'),
    'premium':  float('inf')
}

ANON_LIMIT = 3

# ============================================================
# HELPERS DB
# ============================================================
def get_db():
    try:
        return pymysql.connect(**DB_CONFIG)
    except Exception as e:
        print(f"Erreur DB : {e}")
        return None


# ── Migrations légeres au démarrage (après définition de get_db) ────────────
with app.app_context():
    try:
        _ensure_google_maps_link_column()
    except Exception as e:
        print(f"[startup] migration check failed: {e}")

    try:
        conn = get_db()
        if conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS verification_codes (
                        email VARCHAR(255) PRIMARY KEY,
                        code VARCHAR(10) NOT NULL,
                        purpose VARCHAR(50) NOT NULL,
                        expires_at DATETIME NOT NULL,
                        verified BOOLEAN DEFAULT FALSE,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.commit()
            conn.close()
    except Exception as e:
        print(f"[startup] verification_codes table check failed: {e}")

def hash_password(password):
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def call_groq(system_prompt, user_message, history=None):
    api_key = (os.getenv('GROQ_API_KEY') or GROQ_API_KEY or '').strip()
    if not api_key:
        app.logger.error("chatbot configuration error: GROQ_API_KEY is not set")
        return None

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json"
    }
    models_to_try = list(dict.fromkeys([GROQ_MODEL, "llama-3.3-70b-versatile"]))

    for model_name in models_to_try:
        payload = {
            "model":    model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                *(history or []),
                {"role": "user",   "content": user_message}
            ],
            "temperature": 0.3,
            "max_tokens":  900
        }
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers=headers, json=payload, timeout=20
            )
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"]
                if content and content.strip():
                    return content.strip()
            app.logger.error("Groq API (%s) returned HTTP %s: %s", model_name, resp.status_code, resp.text[:300])
            if resp.status_code == 401:
                break
        except requests.Timeout:
            app.logger.exception("Groq API (%s) request timed out", model_name)
        except requests.RequestException:
            app.logger.exception("Groq API (%s) request failed", model_name)
        except (KeyError, IndexError, TypeError, ValueError):
            app.logger.exception("Groq API (%s) returned an invalid response", model_name)
    return None

def save_message(user_id, role, message, ville=None):
    conn = get_db()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_messages (user_id, role, message, ville_detec) VALUES (%s,%s,%s,%s)",
                (user_id, role, message, ville)
            )
        conn.commit()
    except Exception as e:
        print(f"Erreur save_message : {e}")
    finally:
        conn.close()

def get_chat_history(user_id, limit=30):
    conn = get_db()
    if not conn: return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT role, message, ville_detec, created_at
                FROM chat_messages
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s
            """, (user_id, limit))
            return list(reversed(cur.fetchall()))
    except Exception as e:
        print(f"Erreur get_history : {e}")
        return []
    finally:
        conn.close()

def count_today_messages(user_id):
    """Uses get_user_limit_data to ensure frontend and backend have the exact same count"""
    count, _ = get_user_limit_data(user_id)
    return count

def get_user_limit_data(user_id):
    """Fetch daily limit data from users table"""
    conn = get_db()
    if not conn:
        raise RuntimeError("Database is unavailable while reading daily quota")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT msg_count, last_message_date FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                raise LookupError("User not found while reading daily quota")
            
            count = row.get('msg_count') or 0
            last_date = row.get('last_message_date')
            
            # Reset if it's a new day
            today = datetime.now().date()
            if isinstance(last_date, datetime):
                last_date = last_date.date()
            if last_date != today:
                cur.execute(
                    """UPDATE users SET msg_count = 0, last_message_date = %s
                       WHERE id = %s""",
                    (today, user_id)
                )
                conn.commit()
                return 0, today
            return count, today
    except Exception:
        app.logger.exception("Failed to read daily quota for user %s", user_id)
        raise
    finally:
        conn.close()


def get_user_tier(user_id):
    conn = get_db()
    if not conn:
        raise RuntimeError("Database is unavailable while reading account tier")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT tier FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            return (row.get('tier') or 'explorer') if row else None
    except Exception:
        app.logger.exception("Failed to read account tier for user %s", user_id)
        raise
    finally:
        conn.close()


def increment_user_message_count(user_id, current_count, today):
    """Increment message_count and update last_message_date"""
    conn = get_db()
    if not conn:
        raise RuntimeError("Database is unavailable while updating daily quota")
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users 
                SET msg_count = %s, last_message_date = %s 
                WHERE id = %s
            """, (current_count + 1, today, user_id))
            conn.commit()
    except Exception:
        conn.rollback()
        app.logger.exception("Failed to update daily quota for user %s", user_id)
        raise
    finally:
        conn.close()


def create_conversation(user_id, title='Nouvelle conversation'):
    conn = get_db()
    if not conn:
        raise RuntimeError("Database is unavailable")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversations (user_id, title) VALUES (%s, %s)",
                (user_id, title[:120])
            )
            conversation_id = cur.lastrowid
        conn.commit()
        return conversation_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_owned_conversation(user_id, conversation_id, include_messages=False):
    conn = get_db()
    if not conn:
        raise RuntimeError("Database is unavailable")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, title, created_at, updated_at
                   FROM conversations WHERE id = %s AND user_id = %s""",
                (conversation_id, user_id)
            )
            conversation = cur.fetchone()
            if conversation and include_messages:
                cur.execute(
                    """SELECT id, role, content, created_at FROM messages
                       WHERE conversation_id = %s ORDER BY id ASC""",
                    (conversation_id,)
                )
                conversation['messages'] = cur.fetchall()
            return conversation
    finally:
        conn.close()


def store_conversation_message(user_id, conversation_id, role, content):
    conn = get_db()
    if not conn:
        raise RuntimeError("Database is unavailable")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM conversations WHERE id = %s AND user_id = %s FOR UPDATE",
                (conversation_id, user_id)
            )
            if not cur.fetchone():
                raise LookupError("Conversation not found for this user")
            if role == 'user':
                cur.execute(
                    """SELECT id FROM messages
                       WHERE conversation_id = %s AND role = 'user'
                       ORDER BY id ASC LIMIT 1""",
                    (conversation_id,)
                )
                is_first_user_message = cur.fetchone() is None
            else:
                is_first_user_message = False
            cur.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s)",
                (conversation_id, role, content)
            )
            if is_first_user_message:
                title = content.strip().replace('\r', ' ').replace('\n', ' ')
                title = ' '.join(title.split())[:120] or 'Nouvelle conversation'
                cur.execute(
                    """UPDATE conversations SET title = %s
                       WHERE id = %s AND user_id = %s""",
                    (title, conversation_id, user_id)
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
def log_interaction(user_id, item_type, item_id, interaction_type="view"):
    """
    Enregistre l'interaction de l'utilisateur avec un filtre anti-doublon (5 minutes).
    """
    if not user_id:
        return
    conn = get_db()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            # Vérifier si la même interaction a eu lieu dans les 5 dernières minutes
            cur.execute("""
                SELECT id FROM user_interactions
                WHERE user_id = %s 
                  AND item_type = %s 
                  AND item_id = %s 
                  AND interaction_type = %s 
                  AND created_at >= NOW() - INTERVAL 5 MINUTE
                LIMIT 1
            """, (user_id, item_type, item_id, interaction_type))
            
            if not cur.fetchone():
                cur.execute("""
                    INSERT INTO user_interactions (user_id, item_type, item_id, interaction_type)
                    VALUES (%s, %s, %s, %s)
                """, (user_id, item_type, item_id, interaction_type))
                conn.commit()
    except Exception as e:
        print(f"[Interaction Log Error] : {e}")
    finally:
        conn.close()


def get_conversation_context(user_id, conversation_id, limit=10):
    conn = get_db()
    if not conn:
        raise RuntimeError("Database is unavailable")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT m.role, m.content FROM messages AS m
                   INNER JOIN conversations AS c ON c.id = m.conversation_id
                   WHERE c.id = %s AND c.user_id = %s
                   ORDER BY m.id DESC LIMIT %s""",
                (conversation_id, user_id, limit)
            )
            return list(reversed(cur.fetchall()))
    except Exception:
        app.logger.exception("Failed to read conversation context for user %s", user_id)
        raise
    finally:
        conn.close()

# ============================================================
# EMAIL MASKING
# ============================================================
def mask_email(email):
    if not email or '@' not in email:
        return email
    local, domain = email.split('@', 1)
    if len(local) <= 2:
        masked_local = local[0] + '***'
    else:
        masked_local = local[0] + '***' + local[-1]
    return f"{masked_local}@{domain}"

@app.context_processor
def utility_processor():
    return dict(mask_email=mask_email)

# ============================================================
# ROUTES PAGES
# ============================================================
@app.route('/')
def home():
    return render_template('home.html', cdn_url=CDN_URL)

@app.route('/villes')
def index():
    conn = None
    villes = []
    try:
        conn = pymysql.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nom_ville, slogan, description, type_ville, image_path "
                "FROM villes ORDER BY nom_ville"
            )
            villes = cur.fetchall()
    except Exception as e:
        print(f"Erreur villes : {e}")
    finally:
        if conn: conn.close()
    return render_template('villes.html', villes=villes, titre_filter="Toutes les villes du Maroc", cdn_url=CDN_URL)

FILTER_PATTERNS = {
    'COTIERES':    ['%côtière%', '%côtières%', '%cotiere%', '%cotieres%', '%côtier%', '%cotier%'],
    'MONTAGNE':    ['%montagne%', '%montagneuse%', '%mont%'],
    'CULTURELLES': ['%culturelle%', '%culturelles%', '%cultur%'],
    'SAHARIENNES': ['%saharienne%', '%sahariennes%', '%sahar%'],
    'AGRICOLES':   ['%agricole%', '%agricoles%', '%agric%'],
}

FILTER_TITLES = {
    'COTIERES':    'Villes côtières',
    'MONTAGNE':    'Villes de montagne',
    'CULTURELLES': 'Villes culturelles',
    'SAHARIENNES': 'Villes sahariennes',
    'AGRICOLES':   'Villes agricoles',
}

@app.route('/filter/<path:type_ville>')
def filter_villes(type_ville):
    type_ville = unquote(type_ville)
    if '%' in type_ville:
        type_ville = unquote(type_ville)
    tv_upper = type_ville.upper()
    matched_key = None
    for key, pats in FILTER_PATTERNS.items():
        if key in tv_upper or any(p.strip('%').upper() in tv_upper for p in pats):
            matched_key = key
            break
    patterns = FILTER_PATTERNS[matched_key] if matched_key else None

    conn = None
    villes = []
    try:
        conn = pymysql.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            if patterns:
                where = " OR ".join(["LOWER(type_ville) LIKE %s"] * len(patterns))
                cur.execute(
                    "SELECT nom_ville, slogan, description, type_ville, image_path "
                    f"FROM villes WHERE {where} ORDER BY nom_ville",
                    patterns,
                )
            else:
                cur.execute(
                    "SELECT nom_ville, slogan, description, type_ville, image_path "
                    "FROM villes WHERE type_ville = %s ORDER BY nom_ville",
                    (type_ville,),
                )
            villes = cur.fetchall()
    except Exception as e:
        print(f"Erreur filtre : {e}")
    finally:
        if conn: conn.close()
    titre = FILTER_TITLES.get(matched_key, type_ville)
    return render_template('villes.html', villes=villes, titre_filter=titre, cdn_url=CDN_URL)

@app.route('/ville/<path:nom>')
def details_ville(nom):
    # ── Robust URL decoding for Vercel ──────────────────────────────────
    # Vercel can pass PATH_INFO still percent-encoded. Decode up to twice
    # to handle both single-encoded (%20) and double-encoded (%2520) cases.
    nom = unquote(nom)
    if '%' in nom:          # still has encoded chars → double-encoded
        nom = unquote(nom)
    nom = nom.strip('/')    # <path:> may capture a leading/trailing slash

    conn = None
    try:
        conn = pymysql.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            # ── Primary lookup: exact match ──────────────────────────────
            cur.execute("SELECT * FROM villes WHERE nom_ville = %s", (nom,))
            ville = cur.fetchone()

            # ── Fallback 1: Case-insensitive match ───────────────────────
            if not ville:
                cur.execute(
                    "SELECT * FROM villes WHERE LOWER(nom_ville) = LOWER(%s)",
                    (nom,)
                )
                ville = cur.fetchone()

            # ── Fallback 2: Slug support (replace - and _ with spaces) ───
            if not ville and ('-' in nom or '_' in nom):
                nom_slug = nom.replace('-', ' ').replace('_', ' ')
                cur.execute(
                    "SELECT * FROM villes WHERE LOWER(nom_ville) = LOWER(%s)",
                    (nom_slug,)
                )
                ville = cur.fetchone()

            if not ville:
                abort(404)

            # Use the canonical name from the DB for all sub-queries
            nom = ville['nom_ville']

            cur.execute("SELECT id, nom, description, type_attraction, image_path FROM attractions WHERE nom_ville = %s", (nom,))
            attractions = cur.fetchall()
            for a in attractions:
                if not a.get('type_attraction'):
                    a['type_attraction'] = 'Non spécifié'

            cur.execute("SELECT description FROM activites WHERE nom_ville = %s", (nom,))
            activites = cur.fetchall()

            cur.execute("SELECT nom, description, specialites, lien FROM restaurants WHERE nom_ville = %s", (nom,))
            restaurants = cur.fetchall()

            cur.execute("SELECT nom_plat FROM specialites_ville WHERE nom_ville = %s", (nom,))
            specialites = cur.fetchall()

            cur.execute("SELECT nom_artisanat as nom, description, image_path FROM artisanat WHERE nom_ville = %s", (nom,))
            artisanat = cur.fetchall()

            cur.execute("SELECT nom, etoiles, avis, lien FROM hebergements WHERE nom_ville = %s", (nom,))
            hebergements = cur.fetchall()

            cur.execute("SELECT nom, description, image_path FROM patrimoine_vestimentaire WHERE nom_ville = %s", (nom,))
            vetements = cur.fetchall()

            cur.execute("SELECT nom, periode, description FROM evenements WHERE nom_ville = %s", (nom,))
            evenements = cur.fetchall()

            cur.execute("SELECT type FROM transports WHERE nom_ville = %s", (nom,))
            transports = cur.fetchall()

            cur.execute("SELECT type FROM transports WHERE nom_ville = %s", (nom,))
            transports = cur.fetchall()

            cur.execute("SELECT * FROM textile_crafts WHERE ville_id = %s", (ville['id'],))
            textile_crafts = cur.fetchall()

    except HTTPException:
        raise
    except Exception as e:
        print(f"Erreur details : {e}")
        abort(500)
    finally:
        if conn: conn.close()

    return render_template('details.html',
        ville=ville, attractions=attractions, activites=activites,
        restaurants=restaurants, specialites=specialites,
        artisanat=artisanat, hebergements=hebergements,
        vetements=vetements, evenements=evenements,
        transports=transports, textile_crafts=textile_crafts, cdn_url=CDN_URL)

def _guess_type_icon(type_name):
    """Devine une icône FontAwesome pertinente à partir du nom du type (dynamique, venant de la DB)."""
    t = (type_name or '').strip().lower()
    # Ordre important : les correspondances spécifiques doivent être testées
    # avant les correspondances génériques (ex: "parc de jeux" avant "parc").
    keywords = [
        (['zoo', 'animalier', 'safari'], 'fa-paw'),
        (['parc de jeux', 'jeux pour enfants', 'aire de jeux', 'enfant'], 'fa-child-reaching'),
        (['plage', 'mer', 'côte', 'ocean', 'océan'], 'fa-umbrella-beach'),
        (['désert', 'desert', 'dune', 'sahara'], 'fa-sun'),
        (['montagne', 'atlas', 'randonnée', 'rando', 'trek'], 'fa-mountain'),
        (['médina', 'medina', 'souk', 'marché'], 'fa-store'),
        (['historique', 'monument', 'palais', 'kasbah', 'fort', 'remparts'], 'fa-landmark'),
        (['religieu', 'mosqu', 'zaouia', 'spirituel'], 'fa-mosque'),
        (['culturelle', 'culture', 'musée', 'musee', 'art', 'galerie'], 'fa-palette'),
        (['naturelle', 'nature', 'jardin', 'cascade', 'forêt', 'foret'], 'fa-leaf'),
        (['parc'], 'fa-tree'),
        (['gastronomie', 'culinaire', 'food', 'restaurant'], 'fa-utensils'),
        (['aventure', 'sport', 'activité', 'activite'], 'fa-person-hiking'),
        (['village', 'ville', 'quartier'], 'fa-city'),
        (['spa', 'bien-être', 'bien-etre', 'hammam', 'détente', 'detente'], 'fa-spa'),
    ]
    for kws, icon in keywords:
        if any(k in t for k in kws):
            return icon
    return 'fa-gem'


def _is_unspecified_type(type_name):
    """Filtre les valeurs 'placeholder' type Non spécifié, N/A, etc. hors de la galerie."""
    t = (type_name or '').strip().lower()
    return t in ('non spécifié', 'non specifie', 'non spécifiée', 'non specifiee',
                 'non défini', 'non defini', 'n/a', 'na', 'undefined', '')


@app.route('/attractions')
def attractions_gallery():
    """Page galerie : une grande carte par type d'attraction (dynamique depuis la DB)."""
    conn = None
    type_cards = []
    total_count = 0
    try:
        conn = pymysql.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT type_attraction, COUNT(*) AS cnt, MIN(image_path) AS sample_image
                FROM attractions
                WHERE type_attraction IS NOT NULL AND type_attraction != ''
                GROUP BY type_attraction
                ORDER BY type_attraction
            """)
            rows = cur.fetchall()
            for row in rows:
                if _is_unspecified_type(row['type_attraction']):
                    total_count += row['cnt']
                    continue
                type_cards.append({
                    'nom': row['type_attraction'],
                    'count': row['cnt'],
                    'image': row['sample_image'],
                    'icon': _guess_type_icon(row['type_attraction']),
                })
                total_count += row['cnt']
    except Exception as e:
        print(f"Erreur attractions_gallery : {e}")
    finally:
        if conn: conn.close()

    return render_template('attractions_gallery.html',
                           type_cards=type_cards,
                           total_count=total_count,
                           cdn_url=CDN_URL)


@app.route('/attractions/type/<path:type_name>')
def attractions_list(type_name):
    """Page liste : attractions filtrées par type (et optionnellement par ville), avec possibilité de changer de filtre sur place."""
    type_filter = unquote(type_name).strip()
    ville_filter = request.args.get('ville', '').strip()
    conn = None
    attractions = []
    types = []
    try:
        conn = pymysql.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            # Fetch distinct types (pour la barre de filtre)
            cur.execute("SELECT DISTINCT type_attraction FROM attractions WHERE type_attraction IS NOT NULL AND type_attraction != ''")
            types = [row['type_attraction'] for row in cur.fetchall()]

            # Construction dynamique de la requête (type + ville optionnels)
            conditions = []
            params = []
            if type_filter and type_filter.lower() != 'all':
                conditions.append("type_attraction = %s")
                params.append(type_filter)
            if ville_filter:
                conditions.append("nom_ville = %s")
                params.append(ville_filter)

            base_query = ("SELECT id, nom, nom_ville, description, type_attraction, image_path "
                          "FROM attractions")
            if conditions:
                base_query += " WHERE " + " AND ".join(conditions)
            base_query += " ORDER BY nom"

            cur.execute(base_query, tuple(params))
            attractions = cur.fetchall()
    except Exception as e:
        print(f"Erreur attractions_list : {e}")
    finally:
        if conn: conn.close()

    return render_template('attractions_list.html',
                           attractions=attractions,
                           types=types,
                           current_type=type_filter or 'All',
                           current_ville=ville_filter,
                           cdn_url=CDN_URL)

@app.route('/attraction/<int:att_id>')
def attraction_detail(att_id):
    conn = None
    attraction = None
    try:
        conn = pymysql.connect(**DB_CONFIG)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM attractions WHERE id = %s",
                (att_id,)
            )
            attraction = cur.fetchone()
            
            if not attraction:
                abort(404)
    except HTTPException:
        raise
    except Exception as e:
        print(f"Erreur attraction_detail : {e}")
        abort(500)
    finally:
        if conn: conn.close()

    return render_template('attraction_detail.html', attraction=attraction, cdn_url=CDN_URL)

@app.route('/pricing')
def pricing():
    return render_template('pricing.html', cdn_url=CDN_URL)

# ============================================================
# AUTHENTIFICATION (avec code email)
# ============================================================

import socket

import urllib.request
import json

def send_code_email(email, code, purpose="inscription"):
    subject = f"🔐 Code de vérification - Morocco Secrets ({purpose})"
    body = f"""Bonjour,

Votre code de vérification pour {purpose} sur Morocco Secrets est :

{code}

Ce code expirera dans 10 minutes.

Si vous n'êtes pas à l'origine de cette demande, ignorez simplement cet email.

Merci de votre confiance,
L'équipe Morocco Secrets"""

    sender_email = os.getenv('MAIL_USERNAME') or 'moroccosecrets201@gmail.com'
    brevo_api_key = (os.getenv('BREVO_API_KEY') or '').strip()
    errors = []

    if brevo_api_key:
        payload = {
            "sender": {"name": "Morocco Secrets", "email": sender_email},
            "to": [{"email": email}],
            "subject": subject,
            "textContent": body
        }
        req = urllib.request.Request(
            'https://api.brevo.com/v3/smtp/email',
            data=json.dumps(payload).encode('utf-8'),
            headers={'api-key': brevo_api_key, 'Content-Type': 'application/json', 'Accept': 'application/json'},
            method='POST'
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                print(f"[BREVO] Email envoyé à {email} (purpose: {purpose})")
                return True, "Code envoyé."
        except Exception as e:
            error_msg = f"Erreur Brevo API: {str(e)}"
            if hasattr(e, 'read'):
                try:
                    error_msg += f" - {e.read().decode('utf-8')}"
                except Exception:
                    pass
            print(f"[BREVO] {error_msg}")
            errors.append(error_msg)

    # Fallback SMTP (Gmail / Flask-Mail)
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.header import Header
        smtp_server = os.getenv('MAIL_SERVER') or 'smtp.gmail.com'
        smtp_port = int(os.getenv('MAIL_PORT') or 587)
        smtp_user = sender_email
        smtp_pass = (os.getenv('MAIL_PASSWORD') or 'vobvbhumbzpvcmhu').strip()
        if smtp_user and smtp_pass:
            msg = MIMEText(body, 'plain', 'utf-8')
            msg['Subject'] = Header(subject, 'utf-8')
            msg['From'] = f"Morocco Secrets <{smtp_user}>"
            msg['To'] = email
            with smtplib.SMTP(smtp_server, smtp_port, timeout=12) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_user, [email], msg.as_bytes())
            print(f"[SMTP] Email envoyé à {email} (purpose: {purpose})")
            return True, "Code envoyé."
    except Exception as smtp_err:
        print(f"[SMTP] Erreur: {smtp_err}")
        errors.append(f"SMTP: {smtp_err}")

    return False, (" ; ".join(errors) if errors else "Configuration Email manquante sur le serveur.")

# ---------- Étape 1 : Envoyer code pour inscription ----------
@app.route('/send-verification', methods=['POST'])
def send_verification():
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    
    if not email:
        return jsonify({'success': False, 'message': 'Email requis.'})
    
    conn = get_db()
    if not conn:
        return jsonify({'success': False, 'message': 'Erreur de connexion à la base de données.'})
        
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                return jsonify({'success': False, 'message': 'Cet email est déjà utilisé.'})
            
            # Générer code 6 chiffres
            code = f"{random.randint(100000, 999999)}"
            expiry = datetime.now() + timedelta(minutes=10)
            
            # Ensure table exists
            cur.execute("""
                CREATE TABLE IF NOT EXISTS verification_codes (
                    email VARCHAR(255) PRIMARY KEY,
                    code VARCHAR(10) NOT NULL,
                    purpose VARCHAR(50) NOT NULL,
                    expires_at DATETIME NOT NULL,
                    verified BOOLEAN DEFAULT FALSE,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cur.execute("""
                INSERT INTO verification_codes (email, code, purpose, expires_at, verified)
                VALUES (%s, %s, %s, %s, FALSE)
                ON DUPLICATE KEY UPDATE code=%s, purpose=%s, expires_at=%s, verified=FALSE
            """, (email, code, 'register', expiry, code, 'register', expiry))
            conn.commit()
    except Exception as e:
        print(f"Erreur DB verification_codes: {e}")
        return jsonify({'success': False, 'message': f'Erreur interne DB: {str(e)}'})
    finally:
        if conn: conn.close()
    
    success, err_msg = send_code_email(email, code, "inscription")
    if success:
        return jsonify({'success': True, 'message': 'Code envoyé par email.'})
    else:
        # Nettoyer la base de données pour permettre une nouvelle tentative
        conn = get_db()
        if conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM verification_codes WHERE email = %s", (email,))
                conn.commit()
            conn.close()
        return jsonify({'success': False, 'message': err_msg})

# ---------- Étape 2 : Vérifier code (inscription ou reset) ----------
@app.route('/verify-code', methods=['POST'])
def verify_code():
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    user_code = data.get('code', '').strip()
    
    if not email or not user_code:
        return jsonify({'success': False, 'message': 'Email et code requis.'})
        
    conn = get_db()
    if not conn:
        return jsonify({'success': False, 'message': 'Erreur serveur.'})
        
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT code, expires_at, purpose FROM verification_codes WHERE email = %s", (email,))
            row = cur.fetchone()
            
            if not row:
                return jsonify({'success': False, 'message': 'Aucune demande de vérification en cours.'})
                
            if datetime.now() > row['expires_at']:
                cur.execute("DELETE FROM verification_codes WHERE email = %s", (email,))
                conn.commit()
                return jsonify({'success': False, 'message': 'Code expiré. Recommencez.'})
                
            if user_code != row['code']:
                return jsonify({'success': False, 'message': 'Code incorrect.'})
                
            # Code valide
            cur.execute("UPDATE verification_codes SET verified = TRUE WHERE email = %s", (email,))
            conn.commit()
            return jsonify({'success': True, 'message': 'Email vérifié. Vous pouvez continuer.'})
    except Exception as e:
        print(f"Erreur verify-code: {e}")
        return jsonify({'success': False, 'message': 'Erreur serveur.'})
    finally:
        if conn: conn.close()

# ---------- Étape 3 : Finaliser inscription ----------
@app.route('/register', methods=['POST'])
def register():
    data = request.get_json(silent=True) or request.form
    nom = (data.get('nom') or data.get('name') or '').strip()
    password = (data.get('password') or data.get('mot_de_passe') or '').strip()
    email = data.get('email', '').strip().lower()

    # --- Nouveaux champs de profil (optionnels) ---
    age_raw = data.get('age')
    try:
        age = int(age_raw) if age_raw and str(age_raw).strip().isdigit() else None
    except (ValueError, TypeError):
        age = None

    travel_type = (data.get('travel_type') or 'Solo').strip()
    # Normalize valid values
    valid_types = {'Solo': 'Solo', 'Couple': 'Couple', 'Marié': 'Couple', 'Marie': 'Couple', 'Marié(e)': 'Couple', 'Married': 'Couple', 'Famille': 'Famille', 'Amis': 'Amis', 'Family': 'Famille', 'Friends': 'Amis'}
    travel_type = valid_types.get(travel_type, 'Solo')

    children_raw = data.get('children_count')
    try:
        children_count = int(children_raw) if children_raw and str(children_raw).strip().isdigit() else 0
    except (ValueError, TypeError):
        children_count = 0
    
    if not email:
        return jsonify({'success': False, 'message': 'Email requis.'})
        
    if not nom or not password:
        return jsonify({'success': False, 'message': 'Nom et mot de passe requis.'})
    if len(password) < 6:
        return jsonify({'success': False, 'message': 'Mot de passe trop court.'})
    
    conn = get_db()
    if not conn:
        return jsonify({'success': False, 'message': 'Erreur serveur.'})
        
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cur.fetchone():
                return jsonify({'success': False, 'message': "Cet email est déjà utilisé. Veuillez vous connecter."})

            cur.execute(
                """INSERT INTO users 
                   (nom, email, password_hash, tier, msg_count, email_verified, age, travel_type, children_count) 
                   VALUES (%s, %s, %s, 'explorer', 0, TRUE, %s, %s, %s)""",
                (nom, email, hash_password(password), age, travel_type, children_count)
            )
            conn.commit()
            user_id = cur.lastrowid

            cur.execute("DELETE FROM verification_codes WHERE email = %s", (email,))
            conn.commit()
    except Exception as e:
        print(f"Erreur inscription : {e}")
        return jsonify({'success': False, 'message': "Erreur lors de l'inscription."})
    finally:
        conn.close()

    try:
        generate_personalized_notification(user_id)
    except Exception as e:
        print(f"Erreur notification initiale : {e}")
    
    session.clear()  # Nettoyer toute session existante
    session['user_id'] = user_id
    session['user_nom'] = nom
    session['user_email'] = email
    session['user_tier'] = 'explorer'
    session.pop('anon_msg_count', None)
    
    return jsonify({
        'success': True, 'message': f'Bienvenue {nom} !', 'nom': nom,
        'tier': 'explorer', 'email': email, 'msg_count': 0, 'remaining_queries': 50
    })

# ---------- Connexion classique ----------
@app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    password = data.get('password', '').strip()

    if not email or not password:
        return jsonify({'success': False, 'message': 'Email et mot de passe requis.'})

    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE email = %s AND password_hash = %s",
                (email, hash_password(password))
            )
            user = cur.fetchone()

        if not user:
            return jsonify({'success': False, 'message': 'Email ou mot de passe incorrect.'})

        session.clear()
        session['user_id'] = user['id']
        session['user_nom'] = user['nom']
        session['user_email'] = user['email']
        user_tier = user.get('tier') or 'explorer'
        session['user_tier'] = user_tier
        session.pop('anon_msg_count', None)

        msg_count = count_today_messages(user['id']) if user_tier == 'explorer' else 0
        history = get_chat_history(user['id'], limit=30)

        return jsonify({
            'success':   True,
            'message':   f"Bon retour {user['nom']} !",
            'nom':       user['nom'],
            'tier':      user_tier,
            'email':     user['email'],
            'travel_type': user.get('travel_type') or 'Solo',
            'msg_count': msg_count,
            'history':   [{'role': m['role'], 'message': m['message']} for m in history],
            'remaining_queries': max(0, TIER_LIMITS[user_tier] - msg_count)
                if user_tier == 'explorer' else 'Illimité'
        })
    except Exception as e:
        print(f"Erreur login : {e}")
        return jsonify({'success': False, 'message': 'Erreur serveur.'})
    finally:
        conn.close()

# ---------- Mot de passe oublié : envoyer code ----------
@app.route('/forgot-password', methods=['POST'])
def forgot_password():
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    
    if not email:
        return jsonify({'success': False, 'message': 'Email requis.'})
    
    conn = get_db()
    if not conn:
        return jsonify({'success': False, 'message': 'Erreur de connexion à la base de données.'})
        
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email = %s", (email,))
            user = cur.fetchone()
            if not user:
                return jsonify({'success': False, 'message': 'Aucun compte avec cet email.'})
                
            code = f"{random.randint(100000, 999999)}"
            expiry = datetime.now() + timedelta(minutes=10)
            
            # Ensure table exists
            cur.execute("""
                CREATE TABLE IF NOT EXISTS verification_codes (
                    email VARCHAR(255) PRIMARY KEY,
                    code VARCHAR(10) NOT NULL,
                    purpose VARCHAR(50) NOT NULL,
                    expires_at DATETIME NOT NULL,
                    verified BOOLEAN DEFAULT FALSE,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            cur.execute("""
                INSERT INTO verification_codes (email, code, purpose, expires_at, verified)
                VALUES (%s, %s, %s, %s, FALSE)
                ON DUPLICATE KEY UPDATE code=%s, purpose=%s, expires_at=%s, verified=FALSE
            """, (email, code, 'reset', expiry, code, 'reset', expiry))
            conn.commit()
    except Exception as e:
        print(f"Erreur DB forgot_password: {e}")
        return jsonify({'success': False, 'message': f'Erreur interne DB: {str(e)}'})
    finally:
        if conn: conn.close()
    
    success, err_msg = send_code_email(email, code, "réinitialisation du mot de passe")
    if success:
        return jsonify({'success': True, 'message': 'Code envoyé par email.'})
    else:
        # Nettoyer la base de données pour permettre une nouvelle tentative
        conn = get_db()
        if conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM verification_codes WHERE email = %s AND purpose = 'reset'", (email,))
                conn.commit()
            conn.close()
        return jsonify({'success': False, 'message': err_msg})

# ---------- Réinitialiser mot de passe ----------
@app.route('/reset-password', methods=['POST'])
def reset_password():
    data = request.get_json()
    email = data.get('email', '').strip().lower()
    new_password = data.get('password', '').strip()
    
    if not email:
        return jsonify({'success': False, 'message': 'Email requis.'})
    
    if len(new_password) < 6:
        return jsonify({'success': False, 'message': 'Mot de passe trop court.'})
    
    conn = get_db()
    if not conn:
        return jsonify({'success': False, 'message': 'Erreur serveur.'})
        
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT verified, purpose FROM verification_codes WHERE email = %s", (email,))
            row = cur.fetchone()
            if not row or not row['verified'] or row['purpose'] != 'reset':
                return jsonify({'success': False, 'message': "Aucune demande de réinitialisation vérifiée."})
                
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE email = %s",
                (hash_password(new_password), email)
            )
            
            cur.execute("DELETE FROM verification_codes WHERE email = %s", (email,))
            conn.commit()
    except Exception as e:
        print(f"Erreur reset : {e}")
        return jsonify({'success': False, 'message': 'Erreur lors de la réinitialisation.'})
    finally:
        if conn: conn.close()
    
    return jsonify({'success': True, 'message': 'Mot de passe mis à jour. Connectez-vous.'})

# ---------- Déconnexion ----------
@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})

@app.route('/me')
def me():
    if 'user_id' not in session:
        return jsonify({'logged': False, 'anon_count': session.get('anon_msg_count', 0)})
    user_id = session.get('user_id')
    user_tier = get_user_tier(user_id)
    if not user_tier:
        session.clear()
        return jsonify({'logged': False})
    session['user_tier'] = user_tier
    msg_count = count_today_messages(user_id) if user_tier == 'explorer' else 0
    profile = get_user_profile(user_id)
    return jsonify({
        'logged':    True,
        'nom':       session.get('user_nom'),
        'email':     session.get('user_email'),
        'tier':      user_tier,
        'travel_type': (profile.get('travel_type') if profile else None) or 'Solo',
        'msg_count': msg_count,
        'remaining_queries': max(0, TIER_LIMITS[user_tier] - msg_count)
            if user_tier == 'explorer' else 'Illimité'
    })

# ============================================================
# CHATBOT
# ============================================================
@app.route('/api/conversations', methods=['GET', 'POST'])
def api_conversations():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Connexion requise pour accéder aux conversations.'}), 401

    if request.method == 'POST':
        try:
            conversation_id = create_conversation(user_id)
            conversation = get_owned_conversation(user_id, conversation_id)
            return jsonify({'conversation': conversation}), 201
        except Exception:
            app.logger.exception("Failed to create conversation for user %s", user_id)
            return jsonify({'error': 'Impossible de créer la conversation.'}), 500

    conn = get_db()
    if not conn:
        return jsonify({'error': 'La base de données est indisponible.'}), 503
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, title, created_at, updated_at FROM conversations
                   WHERE user_id = %s ORDER BY updated_at DESC, id DESC""",
                (user_id,)
            )
            conversations = cur.fetchall()
            return jsonify({'conversations': conversations})
    except Exception:
        app.logger.exception("Failed to list or create conversations for user %s", user_id)
        return jsonify({'error': 'Impossible de charger les conversations.'}), 500
    finally:
        conn.close()


@app.route('/api/conversations/import', methods=['POST'])
def api_import_guest_conversation():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Connexion requise pour sauvegarder cette conversation.'}), 401

    data = request.get_json(silent=True)
    raw_messages = data.get('messages') if isinstance(data, dict) else None
    if not isinstance(raw_messages, list) or not raw_messages or len(raw_messages) > 100:
        return jsonify({'error': 'La conversation à sauvegarder est invalide.'}), 400

    messages = []
    for item in raw_messages:
        if not isinstance(item, dict):
            return jsonify({'error': 'Un message de la conversation est invalide.'}), 400
        role = item.get('role')
        content = item.get('content')
        if role not in ('user', 'assistant') or not isinstance(content, str) or not content.strip():
            return jsonify({'error': 'Un message de la conversation est invalide.'}), 400
        messages.append((role, content.strip()[:20000]))

    first_user_message = next((content for role, content in messages if role == 'user'), None)
    title = ' '.join((first_user_message or 'Conversation sauvegardée').split())[:120]
    conn = get_db()
    if not conn:
        return jsonify({'error': 'La base de données est indisponible.'}), 503
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO conversations (user_id, title) VALUES (%s, %s)",
                (user_id, title)
            )
            conversation_id = cur.lastrowid
            cur.executemany(
                """INSERT INTO messages (conversation_id, role, content)
                   VALUES (%s, %s, %s)""",
                [(conversation_id, role, content) for role, content in messages]
            )
        conn.commit()
        return jsonify({'conversation_id': conversation_id}), 201
    except Exception:
        conn.rollback()
        app.logger.exception("Failed to import guest conversation for user %s", user_id)
        return jsonify({'error': 'Impossible de sauvegarder la conversation.'}), 500
    finally:
        conn.close()


@app.route('/api/conversations/<int:conversation_id>', methods=['GET', 'PATCH', 'DELETE'])
def api_conversation(conversation_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Connexion requise pour accéder aux conversations.'}), 401

    try:
        if request.method == 'GET':
            conversation = get_owned_conversation(user_id, conversation_id, include_messages=True)
            if not conversation:
                return jsonify({'error': 'Conversation introuvable.'}), 404
            return jsonify({'conversation': conversation})

        conn = get_db()
        if not conn:
            return jsonify({'error': 'La base de données est indisponible.'}), 503
        try:
            with conn.cursor() as cur:
                if request.method == 'PATCH':
                    data = request.get_json(silent=True)
                    title = data.get('title') if isinstance(data, dict) else None
                    if not isinstance(title, str) or not title.strip():
                        return jsonify({'error': 'Le titre est requis.'}), 400
                    title = ' '.join(title.split())[:120]
                    cur.execute(
                        """UPDATE conversations SET title = %s
                           WHERE id = %s AND user_id = %s""",
                        (title, conversation_id, user_id)
                    )
                    if cur.rowcount == 0:
                        return jsonify({'error': 'Conversation introuvable.'}), 404
                    conn.commit()
                    return jsonify({'success': True, 'title': title})

                cur.execute(
                    "DELETE FROM conversations WHERE id = %s AND user_id = %s",
                    (conversation_id, user_id)
                )
                if cur.rowcount == 0:
                    return jsonify({'error': 'Conversation introuvable.'}), 404
                conn.commit()
                return jsonify({'success': True})
        finally:
            conn.close()
    except Exception:
        app.logger.exception("Failed to update conversation %s for user %s", conversation_id, user_id)
        return jsonify({'error': 'Impossible de modifier la conversation.'}), 500


@app.route('/chat', methods=['POST'])
def chat():
    app.logger.info("chat request received")
    try:
        data    = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'Le corps JSON est invalide.'}), 400
        message = data.get('message')
        if not isinstance(message, str) or not message.strip():
            return jsonify({'error': 'Veuillez écrire un message.'}), 400
        message = message.strip()

        user_id   = session.get('user_id')
        user_tier = 'explorer'
        if user_id:
            user_tier = get_user_tier(user_id)
            if not user_tier:
                session.clear()
                return jsonify({'error': 'Session invalide. Veuillez vous reconnecter.'}), 401
            session['user_tier'] = user_tier
        conversation_id = data.get('conversation_id')
        history = []

        if not user_id:
            try:
                history = validate_guest_history(data.get('history'))
            except ValueError as error:
                return jsonify({'error': str(error)}), 400
        
        used = 0
        today = datetime.now().date()

        if not user_id:
            anon_count = session.get('anon_msg_count', 0)
            if anon_count >= ANON_LIMIT:
                return jsonify({'response': None, 'need_register': True})
            session['anon_msg_count'] = anon_count + 1
            used = session['anon_msg_count']
        elif user_tier == 'explorer':
            used, today = get_user_limit_data(user_id)
            if used >= TIER_LIMITS['explorer']:
                return jsonify({
                    'response': "Vous avez atteint vos 50 requêtes quotidiennes du pack Explorateur. La limite sera réinitialisée demain.",
                    'limit_reached': True
                })

        if user_id:
            if conversation_id is None:
                conversation_id = create_conversation(user_id)
            elif not isinstance(conversation_id, int) or not get_owned_conversation(user_id, conversation_id):
                return jsonify({'error': 'Conversation introuvable.'}), 404
            history = get_conversation_context(user_id, conversation_id, limit=10)
            store_conversation_message(user_id, conversation_id, 'user', message)

        limit_info = None
        if not user_id:
            limit_info = f"L'utilisateur est en mode anonyme. Message {used}/{ANON_LIMIT}. Rappelle-lui de s'inscrire pour ne pas perdre l'accès."
        elif user_tier == 'explorer':
            remaining = TIER_LIMITS['explorer'] - used
            if remaining <= 2:
                limit_info = f"Il reste {remaining} requêtes gratuites à l'utilisateur aujourd'hui. Propose-lui de consulter les offres."

        ui_lang = (data.get('lang') or '').strip()[:10] if isinstance(data.get('lang'), str) else ''
        rag = rag_retrieve_and_augment(message, limit_info, history=history, ui_lang=ui_lang)
        response_text = call_groq(rag['system_prompt'], rag['user_prompt'], history)
        if not response_text:
            response_text = localize_fallback_response(rag.get('fallback_response'), message, ui_lang)
        if not response_text:
            return jsonify({
                'error': "Trippy ne peut pas répondre pour le moment. Réessayez plus tard."
            }), 503

        if user_id:
            store_conversation_message(user_id, conversation_id, 'assistant', response_text)
            save_message(user_id, 'user', message, rag.get('ville_detec'))
            save_message(user_id, 'bot', response_text, rag.get('ville_detec'))
            if user_tier == 'explorer':
                increment_user_message_count(user_id, used, today)
                used += 1

        ville_detec = rag.get('ville_detec')
        intent_detec = rag.get('intent_detec')

        result = {'response': response_text, 'ville': ville_detec, 'intent': intent_detec}
        if user_id:
            result['conversation_id'] = conversation_id
        if not user_id:
            remaining = ANON_LIMIT - used
            result['anon_remaining'] = remaining
            if remaining <= 0:
                result['need_register'] = True
        elif user_tier == 'explorer':
            result['msg_count'] = used
            result['remaining_queries'] = max(0, TIER_LIMITS['explorer'] - used)
        else:
            result['msg_count'] = used
            result['remaining_queries'] = 'Illimité'
        return jsonify(result)
    except Exception:
        app.logger.exception("Unexpected chatbot request failure")
        return jsonify({
            'error': "Erreur interne. Veuillez réessayer plus tard."
        }), 500

@app.route('/chat/history')
def chat_history():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'history': [], 'logged': False})
    history = get_chat_history(user_id, limit=30)
    return jsonify({'logged': True, 'history': [{'role': m['role'], 'message': m['message']} for m in history]})

@app.route('/chat/clear', methods=['POST'])
def clear_chat_history():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False})
    conn = get_db()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM conversations WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM chat_messages WHERE user_id = %s", (user_id,))
            conn.commit()
        finally:
            conn.close()
    return jsonify({'success': True})


def validate_guest_history(raw_history):
    if raw_history is None:
        return []
    if not isinstance(raw_history, list):
        raise ValueError("history must be a list")

    history = []
    for item in raw_history[-10:]:
        if not isinstance(item, dict):
            raise ValueError("history entries must be objects")
        role = item.get('role')
        content = item.get('content')
        if role not in ('user', 'assistant') or not isinstance(content, str):
            raise ValueError("history entries must contain a valid role and content")
        history.append({'role': role, 'content': content[:5000]})
    return history


# ============================================================
# MEDIAS
# ============================================================

@app.route('/static/sounds/<path:filename>')
def serve_sound(filename):
    return send_from_directory(
        os.path.join(frontend_dir, 'static', 'sounds'),
        filename
    )

@app.route('/static/videos/<path:filename>')
def serve_video(filename):
    return send_from_directory(os.path.join(frontend_dir, 'static', 'videos'), filename, mimetype='video/mp4')

@app.route('/static/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(os.path.join(frontend_dir, 'static', 'images'), filename)

@app.route('/static/logo/<path:filename>')
def serve_logo(filename):
    return send_from_directory(os.path.join(frontend_dir, 'static', 'logo'), filename)

# ============================================================
# TIERS & TESTS
# ============================================================
@app.route('/set-tier/<tier>')
def set_tier(tier):
    if tier != 'explorer':
        return jsonify({'status': 'error', 'message': 'Les packs payants doivent être activés par le support.'}), 403

    user_id = session.get('user_id')
    if user_id:
        conn = get_db()
        if not conn:
            return jsonify({'status': 'error', 'message': 'Base de données indisponible.'}), 503
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET tier = %s WHERE id = %s", (tier, user_id))
            conn.commit()
        except Exception:
            conn.rollback()
            app.logger.exception("Failed to update tier for user %s", user_id)
            return jsonify({'status': 'error', 'message': 'Impossible de modifier le pack.'}), 500
        finally:
            conn.close()

    session['user_tier'] = tier
    return jsonify({'status': 'success', 'tier': tier})

# ============================================================
# API RECOMMANDATIONS & NOTIFICATIONS
# ============================================================

# Valeurs acceptées pour le mode de voyage ("Marié" est stocké en interne sous 'Couple')
TRAVEL_TYPE_ALIASES = {
    'Solo': 'Solo',
    'Couple': 'Couple',
    'Marié': 'Couple',
    'Marie': 'Couple',
    'Marié(e)': 'Couple',
    'Famille': 'Famille',
    'Amis': 'Amis',
}

@app.route('/api/profile/travel-type', methods=['POST'])
def api_update_travel_type():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'unauthorized', 'logged': False, 'message': 'Connexion requise.'}), 401

    data = request.get_json(silent=True) or {}
    travel_type = TRAVEL_TYPE_ALIASES.get((data.get('travel_type') or '').strip())
    if not travel_type:
        return jsonify({'status': 'error', 'message': 'Type de voyage invalide.'}), 400

    try:
        children_count = max(0, min(15, int(data.get('children_count', 0) or 0)))
    except (TypeError, ValueError):
        children_count = 0
    if travel_type != 'Famille':
        children_count = 0

    conn = get_db()
    if not conn:
        return jsonify({'status': 'error', 'message': 'Base de données indisponible.'}), 503

    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET travel_type = %s, children_count = %s WHERE id = %s",
                (travel_type, children_count, user_id)
            )
            conn.commit()
        return jsonify({
            'status': 'success',
            'logged': True,
            'travel_type': travel_type,
            'children_count': children_count
        })
    except Exception as e:
        print(f"[API Update Travel Type Error] : {e}")
        return jsonify({'status': 'error', 'message': 'Impossible de mettre à jour le profil.'}), 500
    finally:
        conn.close()

@app.route('/api/recommendations', methods=['GET'])
def api_recommendations():
    # Recommandations réservées aux membres connectés
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({
            'status': 'unauthorized',
            'logged': False,
            'message': 'Connectez-vous ou créez un compte pour accéder à vos recommandations personnalisées.',
            'recommendations': []
        }), 401

    mood = (request.args.get('mood') or 'auto').strip().lower()
    ville = (request.args.get('ville') or '').strip()
    override_travel_type = TRAVEL_TYPE_ALIASES.get((request.args.get('travel_type') or '').strip(), '')
    try:
        limit = min(max(int(request.args.get('limit', 3) or 3), 1), 8)
    except ValueError:
        limit = 3
    raw_exclude = request.args.get('exclude', '')
    exclude_keys = [k.strip() for k in raw_exclude.split(',') if k.strip()][:30]

    recs, profile, villes_list = get_recommendations(
        user_id=user_id,
        mood=mood,
        limit=limit,
        exclude_keys=exclude_keys,
        return_profile=True,
        ville_filter=ville,
        override_travel_type=override_travel_type,
    )
    if not profile:
        return jsonify({
            'status': 'unauthorized',
            'logged': False,
            'message': 'Compte introuvable. Veuillez vous reconnecter.',
            'recommendations': []
        }), 401

    effective_type = override_travel_type or profile.get('travel_type') or 'Solo'
    return jsonify({
        'status': 'success',
        'mood': mood,
        'ville': ville,
        'villes': villes_list,
        'cdn_url': CDN_URL,
        'user': {
            'logged': True,
            'nom': profile.get('nom'),
            'travel_type': effective_type,
        },
        'recommendations': recs
    })

@app.route('/api/notifications', methods=['GET'])
def api_notifications():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'unauthorized', 'notifications': []}), 401

    # Generer automatiquement une notification selon le profil si besoin
    generate_personalized_notification(user_id)

    conn = get_db()
    if not conn:
        return jsonify({'status': 'error', 'notifications': []}), 500

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, message, is_read, created_at 
                FROM notifications 
                WHERE user_id = %s 
                ORDER BY created_at DESC 
                LIMIT 10
            """, (user_id,))
            rows = cur.fetchall()
            
            notifications = []
            for r in rows:
                if isinstance(r, dict):
                    notifications.append({
                        "id": r["id"],
                        "title": r["title"],
                        "message": r["message"],
                        "is_read": bool(r["is_read"]),
                        "created_at": r["created_at"].strftime("%Y-%m-%d %H:%M") if r.get("created_at") else ""
                    })
                else:
                    notifications.append({
                        "id": r[0],
                        "title": r[1],
                        "message": r[2],
                        "is_read": bool(r[3]),
                        "created_at": str(r[4])
                    })
            return jsonify({'status': 'success', 'notifications': notifications})
    except Exception as e:
        print(f"[API Notifications Error] : {e}")
        return jsonify({'status': 'error', 'notifications': []}), 500
    finally:
        conn.close()

@app.route('/api/notifications/<int:notif_id>/read', methods=['POST'])
def api_mark_notification_read(notif_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'unauthorized'}), 401

    conn = get_db()
    if not conn:
        return jsonify({'status': 'error'}), 500

    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE notifications SET is_read = 1 WHERE id = %s AND user_id = %s",
                (notif_id, user_id)
            )
            conn.commit()
        return jsonify({'status': 'success'})
    except Exception as e:
        print(f"[API Read Notification Error] : {e}")
        return jsonify({'status': 'error'}), 500
    finally:
        conn.close()

@app.route('/api/notifications/read-all', methods=['POST'])
def api_mark_all_notifications_read():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'unauthorized'}), 401

    conn = get_db()
    if not conn:
        return jsonify({'status': 'error'}), 500

    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE notifications SET is_read = 1 WHERE user_id = %s AND is_read = 0", (user_id,))
            conn.commit()
        return jsonify({'status': 'success'})
    except Exception as e:
        print(f"[API Read All Notifications Error] : {e}")
        return jsonify({'status': 'error'}), 500
    finally:
        conn.close()

@app.route('/api/search')
def global_search():
    query = request.args.get('q', '').strip()
    if not query or len(query) < 2:
        return jsonify({'results': []})
    
    conn = get_db()
    if not conn:
        return jsonify({'results': [], 'error': 'Database connection failed'})
        
    results = []
    try:
        with conn.cursor() as cur:
            # 1. Search Villes (Cities)
            cur.execute(
                "SELECT nom_ville as title, 'ville' as type, slogan as subtitle, image_path "
                "FROM villes WHERE nom_ville LIKE %s OR type_ville LIKE %s OR slogan LIKE %s LIMIT 5",
                (f"%{query}%", f"%{query}%", f"%{query}%")
            )
            results.extend(cur.fetchall())
            
            # 2. Search Hotels (Hebergements)
            cur.execute(
                "SELECT h.nom as title, 'hotel' as type, v.nom_ville as subtitle, v.image_path "
                "FROM hebergements h JOIN villes v ON h.nom_ville = v.nom_ville "
                "WHERE h.nom LIKE %s LIMIT 5",
                (f"%{query}%",)
            )
            results.extend(cur.fetchall())

            # 3. Search Attractions
            cur.execute(
                "SELECT a.nom as title, 'attraction' as type, v.nom_ville as subtitle, v.image_path "
                "FROM attractions a JOIN villes v ON a.nom_ville = v.nom_ville "
                "WHERE a.nom LIKE %s LIMIT 5",
                (f"%{query}%",)
            )
            results.extend(cur.fetchall())
    except Exception as e:
        print(f"Global search error: {e}")
    finally:
        conn.close()
    
    return jsonify({'results': results})

@app.route('/test-db')
def test_db():
    result = {'status': 'error', 'tables': []}
    conn = get_db()
    if not conn:
        result['message'] = "Connexion impossible"
        return jsonify(result)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            result['tables'] = [list(t.values())[0] for t in cur.fetchall()]
            cur.execute("SELECT COUNT(*) as c FROM villes")
            result['nb_villes'] = cur.fetchone()['c']
            cur.execute("SELECT COUNT(*) as c FROM users")
            result['nb_users'] = cur.fetchone()['c']
            cur.execute("SELECT COUNT(*) as c FROM chat_messages")
            result['nb_messages'] = cur.fetchone()['c']
            result['status'] = 'success'
            result['message'] = "Connexion MySQL réussie"
    except Exception as e:
        result['message'] = str(e)
    finally:
        conn.close()
    return jsonify(result)

if __name__ == '__main__':
    # Mode de développement local
    port = int(os.getenv('PORT', 5001))
    app.run(debug=False, host='0.0.0.0', port=port)