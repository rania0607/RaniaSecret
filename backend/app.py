"""
Trippy — Guide Touristique du Maroc
Avec inscription/connexion par code email (vérification)
"""

from flask import Flask, render_template, abort, request, jsonify, session, send_from_directory
from flask_mail import Mail, Message
from werkzeug.exceptions import HTTPException
import pymysql
import requests
import traceback
import os
import hashlib
import random
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import unquote
from dotenv import load_dotenv

env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
load_dotenv(dotenv_path=env_path)

try:
    from rag_engine import rag_retrieve_and_augment
except ImportError:
    def rag_retrieve_and_augment(message, limit_info=None):
        return {"system_prompt": "Expert Maroc", "user_prompt": message,
                "has_context": False, "ville_detec": None, "intent_detec": None}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# app.py est dans /backend — frontend est au niveau parent (root)
frontend_dir = os.path.join(BASE_DIR, '..', 'frontend')
template_dir = os.path.join(BASE_DIR, '..', 'frontend', 'templates')
static_dir = os.path.join(BASE_DIR, '..', 'frontend', 'static')

from werkzeug.middleware.proxy_fix import ProxyFix
from flask_cors import CORS

app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)

# Fix pour Railway (Reverse Proxy) : assure que Flask comprend que l'on est en HTTPS
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Autorise les requêtes Cross-Origin et les cookies de session
CORS(app, supports_credentials=True)

app.secret_key = os.getenv('SECRET_KEY', 'trippy_maroc_secret_key_2026')
# Configurer les sessions pour qu'elles fonctionnent derrière le proxy HTTPS de Railway
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
# En local (HTTP), SESSION_COOKIE_SECURE doit être False, sinon le navigateur rejette les cookies de session.
app.config['SESSION_COOKIE_SECURE'] = os.getenv('FLASK_ENV') == 'production'

import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from admin_routes import admin_bp, _ensure_google_maps_link_column
app.register_blueprint(admin_bp)

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
GROQ_MODEL   = "llama-3.3-70b-versatile"

from db import DB_CONFIG

TIER_LIMITS = {
    'explorer': 10,
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

def hash_password(password):
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def call_groq(system_prompt, user_message):
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type":  "application/json"
    }
    payload = {
        "model":    GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message}
        ],
        "temperature": 0.7,
        "max_tokens":  800
    }
    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers, json=payload, timeout=20
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"Erreur Groq : {e}")
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
    if not conn: return 0, None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT msg_count, last_message_date FROM users WHERE id = %s", (user_id,))
            row = cur.fetchone()
            if not row: return 0, None
            
            count = row.get('msg_count', 0)
            last_date = row.get('last_message_date')
            
            # Reset if it's a new day
            today = datetime.now().date()
            if last_date != today:
                return 0, today
            return count, today
    except Exception as e:
        print(f"Error get_user_limit_data: {e}")
        return 0, None
    finally:
        conn.close()

def increment_user_message_count(user_id, current_count, today):
    """Increment message_count and update last_message_date"""
    conn = get_db()
    if not conn: return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users 
                SET msg_count = %s, last_message_date = %s 
                WHERE id = %s
            """, (current_count + 1, today, user_id))
            conn.commit()
    except Exception as e:
        print(f"Error increment_user_message_count: {e}")
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

            cur.execute("SELECT nom, description, type_attraction, image_path FROM attractions WHERE nom_ville = %s", (nom,))
            attractions = cur.fetchall()

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
        transports=transports, cdn_url=CDN_URL)

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

Votre code de vérification pour {purpose} est : {code}

Ce code expirera dans 10 minutes.

Si vous n'êtes pas à l'origine de cette demande, ignorez cet email.

Merci de votre confiance."""

    brevo_api_key = os.getenv('BREVO_API_KEY')
    if not brevo_api_key:
        print("[BREVO] Clé API manquante.")
        return False, "Configuration Email manquante sur le serveur."

    sender_email = os.getenv('MAIL_USERNAME', 'moroccosecrets201@gmail.com')

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
            error_msg += f" - {e.read().decode('utf-8')}"
        print(f"[BREVO] {error_msg}")
        return False, error_msg

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
    data = request.get_json()
    nom = data.get('nom', '').strip()
    password = data.get('password', '').strip()
    email = data.get('email', '').strip().lower()
    
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
            cur.execute("SELECT verified, purpose FROM verification_codes WHERE email = %s", (email,))
            row = cur.fetchone()
            if not row or not row['verified'] or row['purpose'] != 'register':
                return jsonify({'success': False, 'message': "Veuillez d'abord vérifier votre email."})
                
            cur.execute(
                "INSERT INTO users (nom, email, password_hash, tier, msg_count, email_verified) VALUES (%s,%s,%s,'explorer',0,TRUE)",
                (nom, email, hash_password(password))
            )
            conn.commit()
            user_id = cur.lastrowid
            
            cur.execute("DELETE FROM verification_codes WHERE email = %s", (email,))
            conn.commit()
    except Exception as e:
        print(f"Erreur inscription : {e}")
        return jsonify({'success': False, 'message': 'Erreur lors de l\'inscription.'})
    finally:
        conn.close()
    
    session.clear()  # Nettoyer toute session existante
    session['user_id'] = user_id
    session['user_nom'] = nom
    session['user_email'] = email
    session['user_tier'] = 'explorer'
    session.pop('anon_msg_count', None)
    
    return jsonify({'success': True, 'message': f'Bienvenue {nom} !', 'nom': nom, 'tier': 'explorer', 'email': email})

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
        session['user_tier'] = user['tier']
        session.pop('anon_msg_count', None)

        history = get_chat_history(user['id'], limit=30)
        msg_count = count_today_messages(user['id'])

        return jsonify({
            'success':   True,
            'message':   f"Bon retour {user['nom']} !",
            'nom':       user['nom'],
            'tier':      user['tier'],
            'email':     user['email'],
            'msg_count': msg_count,
            'history':   [{'role': m['role'], 'message': m['message']} for m in history]
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
    msg_count = count_today_messages(user_id)
    return jsonify({
        'logged':    True,
        'nom':       session.get('user_nom'),
        'email':     session.get('user_email'),
        'tier':      session.get('user_tier'),
        'msg_count': msg_count
    })

# ============================================================
# CHATBOT
# ============================================================
@app.route('/chat', methods=['POST'])
def chat():
    try:
        data    = request.get_json()
        message = data.get('message', '').strip()
        if not message:
            return jsonify({'response': "Veuillez ecrire un message !"})

        user_id   = session.get('user_id')
        user_tier = session.get('user_tier', 'explorer')
        
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
            if used >= 10:
                return jsonify({'response': "Désolé, vous avez épuisé vos 10 messages pour aujourd'hui. Revenez demain !", 'limit_reached': True})

        limit_info = None
        if not user_id:
            limit_info = f"L'utilisateur est en mode anonyme. Message {used}/{ANON_LIMIT}. Rappelle-lui de s'inscrire pour ne pas perdre l'accès."
        elif user_tier == 'explorer':
            remaining = 10 - used
            if remaining <= 2:
                limit_info = f"Il reste {remaining} messages à l'utilisateur aujourd'hui. Suggère-lui de passer au pack Voyageur Pro."

        rag = rag_retrieve_and_augment(message, limit_info)
        response_text = call_groq(rag['system_prompt'], rag['user_prompt'])
        if not response_text:
            response_text = "Désolé, erreur technique."

        if user_id:
            save_message(user_id, 'user', message, rag.get('ville_detec'))
            save_message(user_id, 'bot', response_text, rag.get('ville_detec'))
            if user_tier == 'explorer':
                increment_user_message_count(user_id, used, today)
                used += 1

        ville_detec = rag.get('ville_detec')
        intent_detec = rag.get('intent_detec')

        result = {'response': response_text, 'ville': ville_detec, 'intent': intent_detec}
        if not user_id:
            remaining = ANON_LIMIT - used
            result['anon_remaining'] = remaining
            if remaining <= 0:
                result['need_register'] = True
        elif user_tier == 'explorer':
            result['msg_count'] = used
        else:
            result['msg_count'] = used
        return jsonify(result)
    except Exception as e:
        print(f"CRITICAL CHAT ERROR: {e}")
        traceback.print_exc()
        return jsonify({'response': "Erreur interne. Veuillez réessayer plus tard."})

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
                cur.execute("DELETE FROM chat_messages WHERE user_id = %s", (user_id,))
            conn.commit()
        finally:
            conn.close()
    return jsonify({'success': True})

# ============================================================
# MEDIAS
# ============================================================
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
    if tier in ('explorer', 'standard', 'premium'):
        session['user_tier'] = tier
        user_id = session.get('user_id')
        if user_id:
            conn = get_db()
            if conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute("UPDATE users SET tier = %s WHERE id = %s", (tier, user_id))
                    conn.commit()
                finally:
                    conn.close()
        return jsonify({'status': 'success', 'tier': tier})
    return jsonify({'status': 'error'})

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