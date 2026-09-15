from datetime import datetime
import pymysql
from db import DB_CONFIG

def get_db_connection():
    """Crée une connexion sécurisée à la base de données MySQL."""
    try:
        return pymysql.connect(**DB_CONFIG)
    except Exception as e:
        print(f"[DB Recommendation Error] : {e}")
        return None

def get_context():
    """Détecte le contexte temporel (weekend, saison)."""
    now = datetime.now()
    month = now.month
    weekday = now.weekday()  # 4 = Vendredi, 5 = Samedi, 6 = Dimanche

    is_weekend = weekday in [4, 5, 6]
    season = "ete" if month in [6, 7, 8] else "hiver" if month in [12, 1, 2] else "mi-saison"

    return {
        "is_weekend": is_weekend,
        "season": season,
        "month": month
    }

def get_user_profile(user_id):
    """Récupère les informations de profil de l'utilisateur."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, nom, travel_type, children_count, age FROM users WHERE id = %s",
                (user_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            if isinstance(row, dict):
                return row
            return {
                "id": row[0],
                "nom": row[1],
                "travel_type": row[2] or "Solo",
                "children_count": row[3] or 0,
                "age": row[4]
            }
    finally:
        conn.close()

def get_user_preferred_type(user_id):
    """Identifie le type d'attraction le plus souvent consulté par l'utilisateur."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT a.type_attraction, COUNT(ui.id) AS total_views
                FROM user_interactions ui
                JOIN attractions a ON ui.item_id = a.id
                WHERE ui.user_id = %s AND ui.item_type = 'attraction'
                  AND a.type_attraction IS NOT NULL AND a.type_attraction != ''
                GROUP BY a.type_attraction
                ORDER BY total_views DESC
                LIMIT 1
            """, (user_id,))
            row = cur.fetchone()
            if not row:
                return None
            return row["type_attraction"] if isinstance(row, dict) else row[0]
    finally:
        conn.close()

def get_recommendations(user_id=None, limit=6):
    """Génère des recommandations personnalisées basées sur le profil, l'historique et le contexte."""
    profile = get_user_profile(user_id) if user_id else None
    travel_type = profile.get("travel_type", "Solo") if profile else "Solo"
    preferred_type = get_user_preferred_type(user_id) if user_id else None
    context = get_context()

    conn = get_db_connection()
    if not conn:
        return []

    try:
        with conn.cursor() as cur:
            query = "SELECT id, nom, nom_ville, description, type_attraction, image_path FROM attractions WHERE 1=1"
            params = []

            # 1. Priorité basée sur l'historique de navigation
            if preferred_type:
                query += " ORDER BY (type_attraction = %s) DESC, id DESC"
                params.append(preferred_type)
            # 2. Priorité basée sur le type de voyage
            elif travel_type == "Famille":
                query += " AND (type_attraction LIKE %s OR description LIKE %s) ORDER BY id DESC"
                params.extend(["%parc%", "%famille%"])
            elif travel_type == "Couple":
                query += " AND (type_attraction LIKE %s OR description LIKE %s) ORDER BY id DESC"
                params.extend(["%culture%", "%vue%"])
            elif travel_type == "Amis":
                query += " AND (type_attraction LIKE %s OR description LIKE %s) ORDER BY id DESC"
                params.extend(["%aventure%", "%loisir%"])
            else:
                # Voyageur Solo
                query += " AND (type_attraction LIKE %s OR type_attraction LIKE %s) ORDER BY id DESC"
                params.extend(["%monument%", "%culture%"])

            query += " LIMIT %s"
            params.append(limit)

            cur.execute(query, tuple(params))
            rows = cur.fetchall()

            # En cas de résultat insuffisant, compléter avec les attractions générales
            if not rows:
                cur.execute(
                    "SELECT id, nom, nom_ville, description, type_attraction, image_path FROM attractions ORDER BY id DESC LIMIT %s",
                    (limit,)
                )
                rows = cur.fetchall()

            results = []
            for r in rows:
                if isinstance(r, dict):
                    results.append(r)
                else:
                    results.append({
                        "id": r[0],
                        "nom": r[1],
                        "nom_ville": r[2],
                        "description": r[3],
                        "type_attraction": r[4],
                        "image_path": r[5]
                    })
            return results
    finally:
        conn.close()

def generate_personalized_notification(user_id):
    """Crée une notification contextuelle adaptée au profil et évite les doublons récents."""
    profile = get_user_profile(user_id)
    if not profile:
        return

    travel_type = profile.get("travel_type", "Solo")
    children_count = profile.get("children_count", 0)
    preferred_type = get_user_preferred_type(user_id)

    # Définition du contenu personnalisé
    if preferred_type and "plage" in preferred_type.lower():
        title = "Envie d'évasion côtière ?"
        message = "Découvrez de nouvelles plages et activités en bord de mer sélectionnées pour vous."
    elif travel_type == "Famille":
        title = "Sortie idéale en Famille"
        if children_count and children_count > 0:
            message = f"Découvrez nos activités et parcs recommandés pour vous et vos {children_count} enfants."
        else:
            message = "Découvrez les meilleures attractions sécurisées et divertissantes pour toute la famille."
    elif travel_type == "Couple":
        title = "Escapade Romantique"
        message = "Explorez des panoramas uniques et des lieux magiques pour un séjour inoubliable à deux."
    elif travel_type == "Amis":
        title = "Aventure entre Amis"
        message = "Activités sportives, sorties et découvertes : préparez votre prochaine aventure en groupe."
    else:
        title = "Exploration Solo"
        message = "Profitez d'un itinéraire culturel et de randonnées immersives à votre propre rythme."

    conn = get_db_connection()
    if not conn:
        return

    try:
        with conn.cursor() as cur:
            # Vérifier qu'une notification identique n'a pas déjà été envoyée ces dernières 48 heures
            cur.execute("""
                SELECT id FROM notifications 
                WHERE user_id = %s AND title = %s AND created_at >= NOW() - INTERVAL 2 DAY
                LIMIT 1
            """, (user_id, title))
            
            if not cur.fetchone():
                cur.execute("""
                    INSERT INTO notifications (user_id, title, message, is_read)
                    VALUES (%s, %s, %s, 0)
                """, (user_id, title, message))
                conn.commit()
    except Exception as e:
        print(f"[Notification Generation Error] : {e}")
    finally:
        conn.close()