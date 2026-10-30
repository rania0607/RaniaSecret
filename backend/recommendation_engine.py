from datetime import datetime
import random
from urllib.parse import quote
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


def get_user_profile(user_id, conn=None):
    """Récupère les informations de profil de l'utilisateur."""
    if not user_id:
        return None
    own_conn = conn is None
    conn = conn or get_db_connection()
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
                row["travel_type"] = row.get("travel_type") or "Solo"
                row["children_count"] = row.get("children_count") or 0
                return row
            return {
                "id": row[0],
                "nom": row[1],
                "travel_type": row[2] or "Solo",
                "children_count": row[3] or 0,
                "age": row[4]
            }
    finally:
        if own_conn:
            conn.close()


def get_user_preferred_type(user_id, conn=None):
    """Identifie le type d'attraction le plus souvent consulté par l'utilisateur."""
    if not user_id:
        return None
    own_conn = conn is None
    conn = conn or get_db_connection()
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
    except Exception:
        return None
    finally:
        if own_conn:
            conn.close()


def get_user_recent_chat_cities(user_id, limit=3, conn=None):
    """Récupère les dernières villes mentionnées par l'utilisateur dans le chatbot."""
    if not user_id:
        return []
    own_conn = conn is None
    conn = conn or get_db_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ville_detec
                FROM chat_messages
                WHERE user_id = %s AND ville_detec IS NOT NULL AND ville_detec != ''
                ORDER BY id DESC
                LIMIT 12
            """, (user_id,))
            rows = cur.fetchall()
            cities = []
            for r in rows:
                v = r["ville_detec"] if isinstance(r, dict) else r[0]
                if v and v not in cities:
                    cities.append(v)
                if len(cities) >= limit:
                    break
            return cities
    except Exception:
        return []
    finally:
        if own_conn:
            conn.close()


def _shorten(text, max_len=95):
    if not text:
        return "Découvrez cette pépite incontournable du Maroc sur Morocco Secrets."
    cleaned = " ".join(str(text).split())
    if len(cleaned) <= max_len:
        return cleaned
    cut = cleaned[:max_len].rsplit(" ", 1)[0]
    return (cut or cleaned[:max_len]) + "…"


def _row_dict(row, keys):
    if isinstance(row, dict):
        return row
    return {k: row[i] for i, k in enumerate(keys)}


def _build_city_card(v, badge, mood_topic="general", custom_subtitle=None):
    nom_ville = v.get("nom_ville") or "Maroc"
    img = (v.get("image_path") or "").strip() or "villes/maroc_bg.png"
    subtitle = custom_subtitle or v.get("slogan") or v.get("description") or f"Explorez {nom_ville}"
    return {
        "key": f"ville:{nom_ville}",
        "item_type": "ville",
        "id": v.get("id"),
        "title": nom_ville,
        "ville": nom_ville,
        "category": v.get("type_ville") or "Destination",
        "badge": badge,
        "subtitle": _shorten(subtitle, 95),
        "image_path": img,
        "fallback_image": "villes/maroc_bg.png",
        "url": f"/ville/{quote(nom_ville)}",
        "trippy_city": nom_ville,
        "trippy_place": nom_ville,
        "trippy_topic": mood_topic,
    }


def _build_attraction_card(a, badge, mood_topic="general"):
    att_id = a.get("id")
    nom = a.get("nom") or "Attraction"
    nom_ville = a.get("nom_ville") or "Maroc"
    city_img = (a.get("city_image") or "").strip() or "villes/maroc_bg.png"
    img = (a.get("image_path") or "").strip() or city_img
    return {
        "key": f"attraction:{att_id}",
        "item_type": "attraction",
        "id": att_id,
        "title": nom,
        "ville": nom_ville,
        "category": a.get("type_attraction") or "Lieu à voir",
        "badge": badge,
        "subtitle": _shorten(a.get("description") or f"À découvrir absolument à {nom_ville}.", 95),
        "image_path": img,
        "fallback_image": city_img,
        "url": f"/attraction/{att_id}",
        "trippy_city": nom_ville,
        "trippy_place": nom,
        "trippy_topic": mood_topic,
    }


import re


def _profile_badge_and_regex(travel_type, ctx):
    if travel_type == "Amis":
        return (
            "🎉 Entre Amis",
            "LOWER(type_ville) REGEXP 'côtier|cotier|cotiere|sahar|mont'",
            "aventure|plage|surf|désert|desert|cascade|loisir|quad|randonnée",
        )
    if travel_type == "Famille":
        return (
            "👨‍👩‍👧 En Famille",
            "LOWER(type_ville) REGEXP 'côtier|cotier|cotiere|cultur'",
            "parc|jardin|zoo|plage|famille|nature|musée|musee",
        )
    if travel_type == "Couple":
        return (
            "💑 En Couple",
            "LOWER(type_ville) REGEXP 'cultur|côtier|cotier|sahar'",
            "jardin|palais|plage|coucher|panorama|kasbah|médina|medina",
        )
    if travel_type == "Solo":
        return (
            "🎒 Voyage Solo",
            "LOWER(type_ville) REGEXP 'cultur|mont|côtier|cotier'",
            "médina|medina|monument|culture|randonnée|musée|souk",
        )
    if ctx["season"] == "ete":
        return (
            "☀️ Top cet Été",
            "LOWER(type_ville) REGEXP 'côtier|cotier|cotiere|mont'",
            "plage|mer|cascade|jardin|fraîcheur",
        )
    if ctx["season"] == "hiver":
        return (
            "✨ Coup de Cœur Saison",
            "LOWER(type_ville) REGEXP 'sahar|cultur|mont'",
            "désert|desert|monument|palais|neige|soleil",
        )
    return (
        "🌟 Idée Week-end" if ctx["is_weekend"] else "🌟 Incontournable",
        "1=1",
        "monument|jardin|plage|kasbah|médina|cascade",
    )


def get_recommendations(
    user_id=None,
    mood="auto",
    limit=3,
    exclude_keys=None,
    return_profile=False,
    ville_filter=None,
    override_travel_type=None,
):
    """
    Génère des cartes visuelles de recommandations personnalisées (Villes & Attractions)
    filtrées par humeur ('auto', 'mer', 'aventure', 'culture', 'gastro'), ville optionnelle
    et type de voyage ('Solo', 'Couple', 'Famille', 'Amis').
    """
    mood = (mood or "auto").strip().lower()
    ville_clean = (ville_filter or "").strip()
    if ville_clean.lower() in ("all", "toutes", ""):
        ville_clean = ""
    exclude_set = set(exclude_keys or [])
    ctx = get_context()

    conn = get_db_connection()
    if not conn:
        return ([], None, []) if return_profile else []

    candidates = []
    secondary_candidates = []
    profile = None
    villes_names = []
    target_ville = None

    try:
        profile = get_user_profile(user_id, conn=conn) if user_id else None
        valid_types = ("Solo", "Couple", "Famille", "Amis")
        if override_travel_type in valid_types:
            travel_type = override_travel_type
            if profile:
                profile["travel_type"] = override_travel_type
        else:
            travel_type = profile.get("travel_type", "Solo") if profile else None

        preferred_type = get_user_preferred_type(user_id, conn=conn) if (user_id and mood == "auto" and not ville_clean) else None
        chat_cities = get_user_recent_chat_cities(user_id, conn=conn) if (user_id and mood == "auto" and not ville_clean) else []

        with conn.cursor() as cur:
            cur.execute("SELECT id, nom_ville, slogan, description, type_ville, image_path FROM villes ORDER BY nom_ville")
            all_villes_rows = [
                _row_dict(r, ["id", "nom_ville", "slogan", "description", "type_ville", "image_path"])
                for r in cur.fetchall()
            ]
            villes_names = [v["nom_ville"] for v in all_villes_rows if v.get("nom_ville")]

            if ville_clean:
                target_ville = next(
                    (v for v in all_villes_rows if (v.get("nom_ville") or "").lower() == ville_clean.lower()),
                    None,
                )

            # ══════════════════════════════════════════════════════════════════
            # CAS A : FILTRE SUR UNE VILLE SPÉCIFIQUE
            # ══════════════════════════════════════════════════════════════════
            if target_ville:
                v_name = target_ville["nom_ville"]
                city_img = (target_ville.get("image_path") or "").strip() or "villes/maroc_bg.png"

                if mood == "gastro":
                    cur.execute(
                        "SELECT nom, description, specialites, lien FROM restaurants WHERE nom_ville = %s LIMIT 10",
                        (v_name,),
                    )
                    restos = [_row_dict(r, ["nom", "description", "specialites", "lien"]) for r in cur.fetchall()]
                    cur.execute(
                        "SELECT nom_plat FROM specialites_ville WHERE nom_ville = %s LIMIT 10",
                        (v_name,),
                    )
                    plats = [(_row_dict(r, ["nom_plat"]).get("nom_plat") or "").strip() for r in cur.fetchall()]
                    plats = [p for p in plats if p]

                    if plats:
                        candidates.append({
                            "key": f"gastro_plats:{v_name}",
                            "item_type": "ville",
                            "id": target_ville.get("id"),
                            "title": f"Spécialités de {v_name}",
                            "ville": v_name,
                            "category": "Gastronomie locale",
                            "badge": "🥘 Saveurs Locales",
                            "subtitle": _shorten("À goûter absolument : " + ", ".join(plats), 95),
                            "image_path": city_img,
                            "fallback_image": "villes/maroc_bg.png",
                            "url": f"/ville/{quote(v_name)}",
                            "trippy_city": v_name,
                            "trippy_place": v_name,
                            "trippy_topic": "gastro",
                        })

                    for idx, r in enumerate(restos):
                        r_nom = r.get("nom") or f"Adresse gourmande {idx + 1}"
                        sp = f"Spécialités : {r['specialites']} — " if r.get("specialites") else ""
                        ds = r.get("description") or f"Table recommandée à {v_name}."
                        candidates.append({
                            "key": f"resto:{v_name}:{r_nom}",
                            "item_type": "ville",
                            "id": target_ville.get("id"),
                            "title": r_nom,
                            "ville": v_name,
                            "category": "Restaurant",
                            "badge": "🍽️ Table Recommandée",
                            "subtitle": _shorten(sp + ds, 95),
                            "image_path": city_img,
                            "fallback_image": "villes/maroc_bg.png",
                            "url": (r.get("lien") or "").strip() or f"/ville/{quote(v_name)}",
                            "trippy_city": v_name,
                            "trippy_place": r_nom,
                            "trippy_topic": "gastro",
                        })

                    if not candidates:
                        candidates.append(_build_city_card(target_ville, "🥘 Découverte Gourmande", "gastro"))
                else:
                    if mood == "mer":
                        mood_badge, mood_topic, mood_rx = "🌊 Mer & Détente", "mer", r"plage|mer|côte|cote|océan|ocean|surf|baie|lagune|corniche|port"
                    elif mood == "aventure":
                        mood_badge, mood_topic, mood_rx = "🏜️ Aventure & Nature", "aventure", r"désert|desert|aventure|montagne|nature|cascade|parc|sport|dune|randonnée|trek|quad|atlas|gorge"
                    elif mood == "culture":
                        mood_badge, mood_topic, mood_rx = "🕌 Culture & Médina", "culture", r"historique|monument|culture|musée|musee|médina|medina|palais|religieu|kasbah|unesco|artisanat|rempart"
                    else:
                        mood_badge, _, mood_rx = _profile_badge_and_regex(travel_type, ctx)
                        mood_topic = "general"

                    cur.execute(
                        "SELECT id, nom, nom_ville, description, type_attraction, image_path FROM attractions WHERE nom_ville = %s",
                        (v_name,),
                    )
                    city_atts = [
                        _row_dict(r, ["id", "nom", "nom_ville", "description", "type_attraction", "image_path"])
                        for r in cur.fetchall()
                    ]
                    rx_compiled = re.compile(mood_rx, re.IGNORECASE)
                    for a in city_atts:
                        a["city_image"] = city_img
                        haystack = f"{a.get('type_attraction') or ''} {a.get('description') or ''} {a.get('nom') or ''}"
                        if rx_compiled.search(haystack):
                            candidates.append(_build_attraction_card(a, mood_badge, mood_topic))
                        else:
                            secondary_candidates.append(_build_attraction_card(a, f"📍 À voir à {v_name}", mood_topic))

                    # Compléter avec la fiche ville et ses activités si la ville a peu d'attractions
                    secondary_candidates.append(_build_city_card(target_ville, mood_badge, mood_topic))
                    cur.execute("SELECT description FROM activites WHERE nom_ville = %s LIMIT 4", (v_name,))
                    for idx, r in enumerate(cur.fetchall()):
                        act_desc = (_row_dict(r, ["description"]).get("description") or "").strip()
                        if act_desc:
                            secondary_candidates.append({
                                "key": f"act:{v_name}:{idx}",
                                "item_type": "ville",
                                "id": target_ville.get("id"),
                                "title": f"Expérience à {v_name}",
                                "ville": v_name,
                                "category": "Activité",
                                "badge": mood_badge,
                                "subtitle": _shorten(act_desc, 95),
                                "image_path": city_img,
                                "fallback_image": "villes/maroc_bg.png",
                                "url": f"/ville/{quote(v_name)}",
                                "trippy_city": v_name,
                                "trippy_place": v_name,
                                "trippy_topic": mood_topic,
                            })

            # ══════════════════════════════════════════════════════════════════
            # CAS B : TOUTES LES VILLES DU MAROC
            # ══════════════════════════════════════════════════════════════════
            elif mood == "mer":
                for v in all_villes_rows:
                    tv = (v.get("type_ville") or "").lower()
                    if any(k in tv for k in ("côtier", "cotiere", "cotier", "côtière")):
                        candidates.append(_build_city_card(v, "🌊 Mer & Plages", "mer"))

                cur.execute("""
                    SELECT a.id, a.nom, a.nom_ville, a.description, a.type_attraction, a.image_path,
                           v.image_path AS city_image
                    FROM attractions a
                    LEFT JOIN villes v ON a.nom_ville = v.nom_ville
                    WHERE LOWER(COALESCE(a.type_attraction, '')) REGEXP 'plage|mer|côte|cote|océan|ocean|nature|baie|lagune'
                       OR LOWER(COALESCE(a.description, '')) REGEXP 'plage|océan|surf|mer|baie|corniche|lagune'
                    LIMIT 18
                """)
                for r in cur.fetchall():
                    a = _row_dict(r, ["id", "nom", "nom_ville", "description", "type_attraction", "image_path", "city_image"])
                    candidates.append(_build_attraction_card(a, "🏖️ Évasion Côtière", "mer"))

            elif mood == "aventure":
                for v in all_villes_rows:
                    tv = (v.get("type_ville") or "").lower()
                    if "sahar" in tv or "mont" in tv:
                        b = "🐪 Désert & Dunes" if "sahar" in tv else "🏔️ Montagne & Trek"
                        candidates.append(_build_city_card(v, b, "aventure"))

                cur.execute("""
                    SELECT a.id, a.nom, a.nom_ville, a.description, a.type_attraction, a.image_path,
                           v.image_path AS city_image
                    FROM attractions a
                    LEFT JOIN villes v ON a.nom_ville = v.nom_ville
                    WHERE LOWER(COALESCE(a.type_attraction, '')) REGEXP 'désert|desert|aventure|montagne|nature|cascade|parc|sport'
                       OR LOWER(COALESCE(a.description, '')) REGEXP 'dune|bivouac|randonnée|trek|cascade|quad|atlas|sahara|canyon|gorge'
                    LIMIT 18
                """)
                for r in cur.fetchall():
                    a = _row_dict(r, ["id", "nom", "nom_ville", "description", "type_attraction", "image_path", "city_image"])
                    candidates.append(_build_attraction_card(a, "🏜️ Aventure & Nature", "aventure"))

            elif mood == "culture":
                for v in all_villes_rows:
                    tv = (v.get("type_ville") or "").lower()
                    if "cultur" in tv or "histor" in tv:
                        candidates.append(_build_city_card(v, "🕌 Cité Historique", "culture"))

                cur.execute("""
                    SELECT a.id, a.nom, a.nom_ville, a.description, a.type_attraction, a.image_path,
                           v.image_path AS city_image
                    FROM attractions a
                    LEFT JOIN villes v ON a.nom_ville = v.nom_ville
                    WHERE LOWER(COALESCE(a.type_attraction, '')) REGEXP 'historique|monument|culture|musée|musee|médina|medina|palais|religieu'
                       OR LOWER(COALESCE(a.description, '')) REGEXP 'médina|kasbah|palais|histoire|unesco|artisanat|rempart'
                    LIMIT 20
                """)
                for r in cur.fetchall():
                    a = _row_dict(r, ["id", "nom", "nom_ville", "description", "type_attraction", "image_path", "city_image"])
                    candidates.append(_build_attraction_card(a, "🏛️ Patrimoine & Médina", "culture"))

            elif mood == "gastro":
                cur.execute("""
                    SELECT v.id, v.nom_ville, v.slogan, v.description, v.type_ville, v.image_path,
                           GROUP_CONCAT(DISTINCT sv.nom_plat SEPARATOR ', ') AS plats,
                           GROUP_CONCAT(DISTINCT r.nom SEPARATOR ', ') AS restos
                    FROM villes v
                    LEFT JOIN specialites_ville sv ON sv.nom_ville = v.nom_ville
                    LEFT JOIN restaurants r ON r.nom_ville = v.nom_ville
                    GROUP BY v.id, v.nom_ville, v.slogan, v.description, v.type_ville, v.image_path
                    HAVING plats IS NOT NULL OR restos IS NOT NULL
                """)
                for r in cur.fetchall():
                    v = _row_dict(r, ["id", "nom_ville", "slogan", "description", "type_ville", "image_path", "plats", "restos"])
                    plats = v.get("plats") or ""
                    restos = v.get("restos") or ""
                    if plats and restos:
                        sub = f"Spécialités : {plats} · Adresses : {restos}"
                    elif plats:
                        sub = f"À déguster absolument : {plats}"
                    else:
                        sub = f"Tables recommandées : {restos}"
                    candidates.append(_build_city_card(v, "🥘 Saveurs Locales", "gastro", custom_subtitle=sub))

            else:
                if chat_cities and not override_travel_type:
                    placeholders = ",".join(["%s"] * len(chat_cities))
                    cur.execute(f"""
                        SELECT a.id, a.nom, a.nom_ville, a.description, a.type_attraction, a.image_path,
                               v.image_path AS city_image
                        FROM attractions a
                        LEFT JOIN villes v ON a.nom_ville = v.nom_ville
                        WHERE a.nom_ville IN ({placeholders})
                        LIMIT 6
                    """, tuple(chat_cities))
                    for r in cur.fetchall():
                        a = _row_dict(r, ["id", "nom", "nom_ville", "description", "type_attraction", "image_path", "city_image"])
                        candidates.append(_build_attraction_card(a, f"💬 Autour de {a['nom_ville']}", "general"))

                if preferred_type and not override_travel_type:
                    cur.execute("""
                        SELECT a.id, a.nom, a.nom_ville, a.description, a.type_attraction, a.image_path,
                               v.image_path AS city_image
                        FROM attractions a
                        LEFT JOIN villes v ON a.nom_ville = v.nom_ville
                        WHERE a.type_attraction = %s
                        LIMIT 6
                    """, (preferred_type,))
                    for r in cur.fetchall():
                        a = _row_dict(r, ["id", "nom", "nom_ville", "description", "type_attraction", "image_path", "city_image"])
                        candidates.append(_build_attraction_card(a, "❤️ Selon vos visites", "general"))

                badge_label, city_filter, att_regex = _profile_badge_and_regex(travel_type, ctx)

                cur.execute(f"""
                    SELECT id, nom_ville, slogan, description, type_ville, image_path
                    FROM villes
                    WHERE {city_filter}
                    LIMIT 14
                """)
                for r in cur.fetchall():
                    v = _row_dict(r, ["id", "nom_ville", "slogan", "description", "type_ville", "image_path"])
                    candidates.append(_build_city_card(v, badge_label, "general"))

                cur.execute("""
                    SELECT a.id, a.nom, a.nom_ville, a.description, a.type_attraction, a.image_path,
                           v.image_path AS city_image
                    FROM attractions a
                    LEFT JOIN villes v ON a.nom_ville = v.nom_ville
                    WHERE LOWER(COALESCE(a.type_attraction, '')) REGEXP %s
                       OR LOWER(COALESCE(a.description, '')) REGEXP %s
                    LIMIT 16
                """, (att_regex, att_regex))
                for r in cur.fetchall():
                    a = _row_dict(r, ["id", "nom", "nom_ville", "description", "type_attraction", "image_path", "city_image"])
                    candidates.append(_build_attraction_card(a, badge_label, "general"))

            if len(candidates) + len(secondary_candidates) < limit:
                for v in all_villes_rows[:10]:
                    secondary_candidates.append(_build_city_card(v, "✨ Découverte Maroc", "general"))

    except Exception as e:
        print(f"[Recommendation Query Error] : {e}")
    finally:
        conn.close()

    # Déduplication par clé en préservant l'ordre (prioritaires d'abord, secondaires ensuite)
    random.shuffle(candidates)
    random.shuffle(secondary_candidates)
    unique_items = []
    seen_keys = set()
    for item in candidates + secondary_candidates:
        if item["key"] not in seen_keys:
            seen_keys.add(item["key"])
            unique_items.append(item)

    filtered = [it for it in unique_items if it["key"] not in exclude_set]
    pool = filtered if len(filtered) >= limit else unique_items

    if not pool:
        return ([], profile, villes_names) if return_profile else []

    if target_ville:
        final_list = pool[:limit]
        return (final_list, profile, villes_names) if return_profile else final_list

    priority_items = [it for it in pool if it["badge"].startswith(("💬", "❤️"))]
    other_items = [it for it in pool if it not in priority_items]

    selected = []
    used_cities = set()

    for source in (priority_items[:1], other_items, priority_items[1:]):
        for it in source:
            if len(selected) >= limit:
                break
            if it["ville"] not in used_cities:
                selected.append(it)
                used_cities.add(it["ville"])

    if len(selected) < limit:
        for it in pool:
            if it not in selected:
                selected.append(it)
            if len(selected) >= limit:
                break

    final_list = selected[:limit]
    return (final_list, profile, villes_names) if return_profile else final_list


def generate_personalized_notification(user_id):
    """Crée une notification contextuelle adaptée au profil et évite les doublons récents."""
    profile = get_user_profile(user_id)
    if not profile:
        return

    travel_type = profile.get("travel_type", "Solo")
    children_count = profile.get("children_count", 0)
    preferred_type = get_user_preferred_type(user_id)

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
            cur.execute("""
                SELECT id FROM notifications
                WHERE user_id = %s AND title = %s AND created_at >= NOW() - INTERVAL 7 DAY
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
