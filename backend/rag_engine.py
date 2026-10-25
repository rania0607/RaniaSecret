"""
RAG Engine pour Trippy — Guide Touristique Maroc
Retrieval-Augmented Generation depuis MySQL

Fonctionnement :
  1. retrieve_context()           → interroge MySQL selon le message
  2. build_augmented_prompt()     → injecte les données dans le system prompt
  3. generate_fallback_response() → génère une réponse directe depuis MySQL si le LLM externe est indisponible
  4. rag_retrieve_and_augment()   → point d'entrée appelé par app.py
"""

import pymysql
import re
import os
import traceback
import unicodedata
from difflib import SequenceMatcher
from db import DB_CONFIG


def _strip_accents(text: str) -> str:
    if not text:
        return ""
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    ).lower()


# ============================================================
# CONNEXION DB
# ============================================================

def get_db_connection():
    try:
        return pymysql.connect(**DB_CONFIG)
    except Exception as e:
        print(f"RAG DB Connection Error: {e}")
        return None


# ============================================================
# DÉTECTION D'INTENTION
# ============================================================

INTENT_KEYWORDS = {
    'restaurants':  ['restaurant', 'manger', 'resto', 'cuisine', 'diner', 'dîner',
                     'dejeuner', 'déjeuner', 'nourriture', 'gastronomie', 'food', 'eat', 'makla', 'مطعم', 'أكل'],
    'attractions':  ['attraction', 'visiter', 'monument', 'voir', 'touriste',
                     'site', 'musee', 'musée', 'palais', 'mosquee', 'mosquée', 'medina', 'médina', 'visite', 'معالم', 'زيارة'],
    'hebergements': ['hebergement', 'hébergement', 'hotel', 'hôtel', 'dormir', 'riad',
                     'auberge', 'chambre', 'nuit', 'sejour', 'séjour', 'loger', 'فندق', 'رياض'],
    'activites':    ['activite', 'activité', 'activites', 'activités', 'faire', 'sport', 'balade', 'randonnee', 'randonnée',
                     'excursion', 'loisir', 'promenade', 'aventure', 'أنشطة'],
    'specialites':  ['specialite', 'spécialité', 'plat', 'tajine', 'couscous', 'pastilla', 'tanjia',
                     'gastronomie', 'local', 'typique', 'cuisine locale', 'طاجين', 'كسكس', 'طبخ'],
    'evenements':   ['evenement', 'événement', 'festival', 'fete', 'fête', 'celebration', 'célébration',
                     'agenda', 'manifestation', 'concert', 'مهرجان'],
    'transports':   ['transport', 'deplacer', 'déplacer', 'aller', 'train', 'bus', 'avion',
                     'taxi', 'voiture', 'comment arriver', 'route', 'tramway', 'tgv', 'al boraq', 'نقل', 'مواصلات'],
    'vetements':    ['vetement', 'vêtement', 'habit', 'tenue', 'djellaba', 'caftan', 'takchita',
                     'babouche', 'habit traditionnel', 'costume', 'جلابة', 'قفطان'],
    'artisanat':    ['artisanat', 'souk', 'poterie', 'zellige', 'tapis', 'ceramique', 'céramique',
                     'cuir', 'bijoux', 'craft', 'marche', 'marché', 'bois', 'صناعة تقليدية'],
    'general':      []
}


def detect_intent(message: str) -> str:
    """Retourne l'intention principale du message."""
    msg = _strip_accents(message)
    scores = {
        intent: sum(1 for kw in keywords if _strip_accents(kw) in msg)
        for intent, keywords in INTENT_KEYWORDS.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else 'general'


# ============================================================
# DÉTECTION DE VILLE (exact + sans accents + fuzzy)
# ============================================================

CITY_ALIASES = {
    'marakech': 'Marrakech',
    'marrakesh': 'Marrakech',
    'fez': 'Fès',
    'fes': 'Fès',
    'meknes': 'Meknès',
    'tetouan': 'Tétouan',
    'laayoune': 'Laâyoune',
    'layoune': 'Laâyoune',
    'saidia': 'Saïdia',
    'kenitra': 'Kénitra',
    'oukaimeden': 'Oukaïmeden',
    'casa': 'Casablanca',
    'chaouen': 'Chefchaouen',
    'taghazout': 'Agadir',
}


def detect_ville(message: str, villes_list: list) -> str | None:
    """
    Détecte une ville dans le message.
    1. Correspondance exacte ou sans accents
    2. Alias courants
    3. Fuzzy matching (tolère les fautes de frappe)
    """
    msg_lower = message.lower()
    msg_norm = _strip_accents(message)

    # 1. Correspondance directe (avec ou sans accents)
    for ville in sorted(villes_list, key=len, reverse=True):
        v_norm = _strip_accents(ville)
        if ville.lower() in msg_lower or (len(v_norm) >= 3 and re.search(r'\b' + re.escape(v_norm) + r'\b', msg_norm)):
            return ville

    # 2. Alias
    words_norm = re.findall(r'\b\w+\b', msg_norm)
    for alias, target in CITY_ALIASES.items():
        if alias in words_norm:
            for ville in villes_list:
                if _strip_accents(ville) == _strip_accents(target):
                    return ville

    # 3. Fuzzy matching (tolère les fautes de frappe)
    best_ville = None
    best_ratio = 0.0

    for word in words_norm:
        if len(word) < 4:
            continue
        for ville in villes_list:
            v_norm = _strip_accents(ville)
            ratio = SequenceMatcher(None, word, v_norm).ratio()
            if ratio > best_ratio and ratio >= 0.80:
                best_ratio = ratio
                best_ville = ville

    return best_ville


def _is_global_category_question(message: str) -> bool:
    """Vérifie si la question porte sur une catégorie globale plutôt que sur la ville précédente."""
    msg = _strip_accents(message)
    global_triggers = [
        'villes cotieres', 'ville cotiere', 'villes de montagne', 'villes culturelles',
        'villes sahariennes', 'villes agricoles', 'montre-moi les villes', 'montre moi les villes',
        'toutes les villes', 'liste des villes', 'quelles sont les villes',
        'montre-moi les attractions', 'montre moi les attractions',
        'meilleure periode', 'quand visiter', 'informations sur le desert',
        'gastronomie marocaine', 'me deplacer au maroc', 'deplacer au maroc'
    ]
    return any(t in msg for t in global_triggers)


# ============================================================
# RETRIEVAL — Récupération du contexte MySQL
# ============================================================

def retrieve_context(message: str, history: list = None) -> dict:
    """
    Interroge MySQL et retourne les données pertinentes.
    """
    context = {
        'ville':      None,
        'intent':     'general',
        'data':       {},
        'all_villes': []
    }
    conn = get_db_connection()
    if not conn:
        return context

    try:
        with conn.cursor() as cur:
            # Liste complète des villes avec métadonnées
            cur.execute(
                "SELECT nom_ville, slogan, description, type_ville, latitude, longitude "
                "FROM villes ORDER BY nom_ville"
            )
            toutes_villes = cur.fetchall()
            villes_list = [r['nom_ville'] for r in toutes_villes]
            context['all_villes'] = villes_list
            context['data']['toutes_villes'] = toutes_villes

            # Détection ville & intention
            ville  = detect_ville(message, villes_list)
            intent = detect_intent(message)

            # Si aucune ville dans ce message mais historique présent et pas question globale
            if not ville and history and not _is_global_category_question(message):
                for item in reversed(history):
                    content = item.get('content') or item.get('message') or ''
                    prev_ville = detect_ville(content, villes_list)
                    if prev_ville:
                        ville = prev_ville
                        break

            context['ville']  = ville
            context['intent'] = intent

            if ville:
                # ── Infos générales de la ville ──────────────────────────────
                ville_info = next((v for v in toutes_villes if v['nom_ville'] == ville), None)
                if not ville_info:
                    cur.execute(
                        "SELECT nom_ville, slogan, description, type_ville, latitude, longitude "
                        "FROM villes WHERE nom_ville = %s", (ville,)
                    )
                    ville_info = cur.fetchone()
                context['data']['ville_info'] = ville_info

                # Récupérer toutes les rubriques de la ville pour un contexte complet
                cur.execute(
                    "SELECT nom, description, specialites, lien "
                    "FROM restaurants WHERE nom_ville = %s LIMIT 6", (ville,)
                )
                context['data']['restaurants'] = cur.fetchall()

                cur.execute(
                    "SELECT nom, description, type_attraction FROM attractions "
                    "WHERE nom_ville = %s LIMIT 8", (ville,)
                )
                context['data']['attractions'] = cur.fetchall()

                cur.execute(
                    "SELECT nom, etoiles, avis, lien FROM hebergements "
                    "WHERE nom_ville = %s LIMIT 6", (ville,)
                )
                context['data']['hebergements'] = cur.fetchall()

                cur.execute(
                    "SELECT description FROM activites "
                    "WHERE nom_ville = %s LIMIT 8", (ville,)
                )
                context['data']['activites'] = cur.fetchall()

                cur.execute(
                    "SELECT nom_plat FROM specialites_ville "
                    "WHERE nom_ville = %s LIMIT 10", (ville,)
                )
                context['data']['specialites'] = cur.fetchall()

                cur.execute(
                    "SELECT nom, periode, description FROM evenements "
                    "WHERE nom_ville = %s LIMIT 6", (ville,)
                )
                context['data']['evenements'] = cur.fetchall()

                cur.execute(
                    "SELECT type FROM transports WHERE nom_ville = %s", (ville,)
                )
                context['data']['transports'] = cur.fetchall()

                cur.execute(
                    "SELECT nom, description FROM patrimoine_vestimentaire "
                    "WHERE nom_ville = %s LIMIT 5", (ville,)
                )
                context['data']['vetements'] = cur.fetchall()

                cur.execute(
                    "SELECT nom_artisanat, description FROM artisanat "
                    "WHERE nom_ville = %s LIMIT 6", (ville,)
                )
                context['data']['artisanat'] = cur.fetchall()

            else:
                # ── Pas de ville détectée → données générales depuis MySQL ──
                cur.execute("SELECT DISTINCT type_ville FROM villes WHERE type_ville IS NOT NULL")
                context['data']['types'] = cur.fetchall()

                cur.execute(
                    "SELECT nom, nom_ville, type_attraction, description "
                    "FROM attractions ORDER BY id ASC LIMIT 15"
                )
                context['data']['global_attractions'] = cur.fetchall()

                cur.execute(
                    "SELECT nom_ville, nom_plat FROM specialites_ville LIMIT 25"
                )
                context['data']['global_specialites'] = cur.fetchall()

                cur.execute(
                    "SELECT nom, nom_ville, specialites, description FROM restaurants LIMIT 10"
                )
                context['data']['global_restaurants'] = cur.fetchall()

                cur.execute(
                    "SELECT DISTINCT type FROM transports WHERE type IS NOT NULL LIMIT 20"
                )
                context['data']['global_transports'] = cur.fetchall()

                cur.execute(
                    "SELECT nom, nom_ville, periode, description FROM evenements LIMIT 10"
                )
                context['data']['global_evenements'] = cur.fetchall()

                cur.execute(
                    "SELECT nom, nom_ville, etoiles, avis FROM hebergements LIMIT 10"
                )
                context['data']['global_hebergements'] = cur.fetchall()

                cur.execute(
                    "SELECT nom_artisanat, nom_ville, description FROM artisanat LIMIT 10"
                )
                context['data']['global_artisanat'] = cur.fetchall()

    except Exception as e:
        print(f"RAG Retrieval Error: {e}")
        traceback.print_exc()
    finally:
        conn.close()

    return context


# ============================================================
# AUGMENTATION — Construction du prompt enrichi
# ============================================================

def build_augmented_prompt(user_message: str, context: dict, limit_info: str = None) -> tuple[str, str]:
    """
    Construit le system_prompt avec les données MySQL injectées.
    Retourne (system_prompt, user_prompt).
    """
    ville      = context.get('ville')
    data       = context.get('data', {})
    all_villes = context.get('all_villes', [])

    system_prompt = """Tu es Trippy, le guide touristique de Morocco Secrets.

RÈGLES DE RÉPONSE
- Réponds uniquement à partir des données de la plateforme incluses dans le contexte et de l'historique fourni. L'historique sert à comprendre les références et préférences, pas à confirmer un fait absent des données.
- N'utilise pas tes connaissances générales pour compléter une information manquante.
- N'invente jamais de prix, horaires, adresses, coordonnées, disponibilités ou détails pratiques.
- Si les données ne répondent pas à la question, dis clairement que l'information n'est pas disponible sur Morocco Secrets et invite l'utilisateur à préciser sa demande ou à consulter l'établissement.
- Pour toute question sur un prix, un horaire ou une adresse, ne donne une valeur que si elle figure explicitement dans les données fournies.
- Réponds aux questions liées au tourisme au Maroc. Pour les autres sujets, indique brièvement que tu peux aider à préparer un voyage au Maroc.
- Réponds toujours dans la langue du dernier message de l'utilisateur, y compris en arabe, darija, anglais ou toute autre langue. Si l'utilisateur change de langue, change immédiatement.
- Pour une demande de traduction, fournis uniquement la traduction du texte donné.
- Reste chaleureux, clair et concis; utilise des listes si elles améliorent la lisibilité."""

    if limit_info:
        system_prompt += f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n⚠️ INFOS SUR LES LIMITES (À inclure en fin de réponse dans la même langue que la réponse)\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n{limit_info}\n"

    context_parts = []

    if ville and data.get('ville_info'):
        vi = data['ville_info']
        context_parts.append(f"""
=== VILLE : {vi['nom_ville']} ===
Slogan      : {vi.get('slogan', 'N/A')}
Type        : {vi.get('type_ville', 'N/A')}
Description : {vi.get('description', 'N/A')}
Coordonnées : {vi.get('latitude', '')}, {vi.get('longitude', '')}
""")

    if data.get('restaurants'):
        ctx = "\n=== RESTAURANTS ===\n"
        for r in data['restaurants']:
            ctx += f"• {r['nom']}"
            if r.get('specialites'):
                ctx += f" — Spécialités : {r['specialites']}"
            if r.get('description'):
                ctx += f"\n  {r['description'][:120]}"
            if r.get('lien'):
                ctx += f"\n  🔗 {r['lien']}"
            ctx += "\n"
        context_parts.append(ctx)

    if data.get('attractions'):
        ctx = "\n=== ATTRACTIONS TOURISTIQUES ===\n"
        for a in data['attractions']:
            type_str = f" ({a['type_attraction']})" if a.get('type_attraction') else ""
            ctx += f"• {a['nom']}{type_str}"
            if a.get('description'):
                ctx += f" : {a['description'][:150]}"
            ctx += "\n"
        context_parts.append(ctx)

    if data.get('hebergements'):
        ctx = "\n=== HÉBERGEMENTS ===\n"
        for h in data['hebergements']:
            ctx += f"• {h['nom']}"
            if h.get('etoiles'):
                ctx += f" ({h['etoiles']} étoiles)"
            if h.get('avis'):
                ctx += f" — {h['avis'][:100]}"
            if h.get('lien'):
                ctx += f" | 🔗 {h['lien']}"
            ctx += "\n"
        context_parts.append(ctx)

    if data.get('activites'):
        ctx = "\n=== ACTIVITÉS ===\n"
        for a in data['activites']:
            ctx += f"• {a['description'][:150]}\n"
        context_parts.append(ctx)

    if data.get('specialites'):
        plats = [s['nom_plat'] for s in data['specialites']]
        context_parts.append(f"\n=== SPÉCIALITÉS CULINAIRES ===\n{', '.join(plats)}\n")

    if data.get('evenements'):
        ctx = "\n=== ÉVÉNEMENTS & FESTIVALS ===\n"
        for e in data['evenements']:
            ctx += f"• {e['nom']}"
            if e.get('periode'):
                ctx += f" ({e['periode']})"
            if e.get('description'):
                ctx += f" : {e['description'][:120]}"
            ctx += "\n"
        context_parts.append(ctx)

    if data.get('transports'):
        types = [t['type'] for t in data['transports']]
        context_parts.append(f"\n=== TRANSPORTS DISPONIBLES ===\n{', '.join(types)}\n")

    if data.get('vetements'):
        ctx = "\n=== PATRIMOINE VESTIMENTAIRE ===\n"
        for v in data['vetements']:
            ctx += f"• {v['nom']} : {v.get('description', '')[:120]}\n"
        context_parts.append(ctx)

    if data.get('artisanat'):
        ctx = "\n=== ARTISANAT ===\n"
        for a in data['artisanat']:
            ctx += f"• {a['nom_artisanat']} : {a.get('description', '')[:120]}\n"
        context_parts.append(ctx)

    if data.get('toutes_villes') and not ville:
        ctx = "\n=== TOUTES LES VILLES MAROCAINES ===\n"
        for v in data['toutes_villes']:
            ctx += f"• {v['nom_ville']} ({v.get('type_ville', '')}) — {v.get('slogan', '')}\n"
        context_parts.append(ctx)

    if all_villes:
        context_parts.append(
            f"\n=== VILLES DANS NOTRE BASE ===\n{', '.join(all_villes)}\n"
        )

    if context_parts:
        full_context = "\n".join(context_parts)
        system_prompt += f"""

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📚 CONTEXT (Real data from our DB) :
{full_context}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Consignes : utilise uniquement les informations présentes dans ce contexte.
Les liens fournis peuvent être mentionnés. N'ajoute aucun détail absent des données.
"""
    else:
        system_prompt += """

Aucune donnée spécifique de la plateforme n'a été trouvée pour cette demande.
Dis clairement que l'information n'est pas disponible sur Morocco Secrets.
Ne complète pas avec des connaissances générales et réponds dans la langue de l'utilisateur.
"""

    return system_prompt, user_message


# ============================================================
# FALLBACK INTELLIGENT BASÉ SUR MYSQL (SI GROQ INDISPONIBLE)
# ============================================================

def generate_fallback_response(user_message: str, context: dict, limit_info: str = None) -> str:
    """
    Génère une réponse complète, naturelle et structurée directement à partir
    des données MySQL de Morocco Secrets lorsque l'API LLM externe est indisponible.
    """
    msg_norm = _strip_accents(user_message)
    ville = context.get('ville')
    intent = context.get('intent', 'general')
    data = context.get('data', {})
    toutes_villes = data.get('toutes_villes', [])

    lines = []

    # ── 1. Question sur une ville précise ────────────────────────────────────
    if ville and data.get('ville_info'):
        vi = data['ville_info']
        nom_v = vi['nom_ville']
        slogan = vi.get('slogan') or ''
        desc = vi.get('description') or ''
        type_v = vi.get('type_ville') or ''

        if intent == 'activites':
            acts = data.get('activites', [])
            atts = data.get('attractions', [])
            lines.append(f"✨ **Activités et découvertes à {nom_v}** ({slogan}) :")
            if acts:
                for a in acts[:6]:
                    lines.append(f"• {a['description']}")
            if atts:
                lines.append(f"\n🏛️ **Lieux incontournables à visiter à {nom_v} :**")
                for at in atts[:5]:
                    t_str = f" ({at['type_attraction']})" if at.get('type_attraction') else ""
                    d_str = f" — {at['description'][:120]}..." if at.get('description') and len(at['description']) > 120 else (f" — {at['description']}" if at.get('description') else "")
                    lines.append(f"• **{at['nom']}**{t_str}{d_str}")

        elif intent == 'restaurants':
            restos = data.get('restaurants', [])
            specs = data.get('specialites', [])
            lines.append(f"🍽️ **Où manger à {nom_v} ?**")
            if restos:
                for r in restos[:6]:
                    sp = f" — *Spécialités : {r['specialites']}*" if r.get('specialites') else ""
                    ds = f"\n  {r['description'][:130]}" if r.get('description') else ""
                    lines.append(f"• **{r['nom']}**{sp}{ds}")
            if specs:
                plats = ", ".join(s['nom_plat'] for s in specs)
                lines.append(f"\n🥘 **Spécialités locales de {nom_v} :** {plats}.")

        elif intent == 'specialites':
            specs = data.get('specialites', [])
            restos = data.get('restaurants', [])
            lines.append(f"🥘 **Spécialités culinaires de {nom_v} :**")
            if specs:
                for s in specs:
                    lines.append(f"• {s['nom_plat']}")
            if restos:
                lines.append(f"\n🍽️ **Restaurants recommandés à {nom_v} :** " + ", ".join(r['nom'] for r in restos[:4]) + ".")

        elif intent == 'attractions':
            atts = data.get('attractions', [])
            lines.append(f"🏛️ **Attractions touristiques à {nom_v}** ({slogan}) :")
            if atts:
                for at in atts[:6]:
                    t_str = f" ({at['type_attraction']})" if at.get('type_attraction') else ""
                    d_str = f" : {at['description'][:140]}" if at.get('description') else ""
                    lines.append(f"• **{at['nom']}**{t_str}{d_str}")
            else:
                lines.append(desc)

        elif intent == 'hebergements':
            hebs = data.get('hebergements', [])
            lines.append(f"🏨 **Hébergements recommandés à {nom_v} :**")
            if hebs:
                for h in hebs[:6]:
                    stars = f" ({h['etoiles']}★)" if h.get('etoiles') else ""
                    avis = f" — {h['avis'][:110]}" if h.get('avis') else ""
                    lines.append(f"• **{h['nom']}**{stars}{avis}")
            else:
                lines.append(f"Consultez la page de {nom_v} sur Morocco Secrets pour découvrir nos adresses d'hébergement.")

        elif intent == 'evenements':
            evs = data.get('evenements', [])
            lines.append(f"🎭 **Événements et festivals à {nom_v} :**")
            if evs:
                for e in evs:
                    per = f" ({e['periode']})" if e.get('periode') else ""
                    ds = f" : {e['description'][:130]}" if e.get('description') else ""
                    lines.append(f"• **{e['nom']}**{per}{ds}")
            else:
                lines.append(f"Aucun événement spécifique n'est répertorié pour {nom_v} pour le moment.")

        elif intent == 'transports':
            trs = data.get('transports', [])
            lines.append(f"🚆 **Transports disponibles à {nom_v} :**")
            if trs:
                for t in trs:
                    lines.append(f"• {t['type']}")
            else:
                lines.append(f"Vous pouvez rejoindre et parcourir {nom_v} en taxi, bus interurbain (CTM / Supratours) ou voiture.")

        elif intent == 'vetements':
            vets = data.get('vetements', [])
            lines.append(f"👘 **Patrimoine vestimentaire de {nom_v} :**")
            if vets:
                for v in vets:
                    lines.append(f"• **{v['nom']}** : {v.get('description', '')}")
            else:
                lines.append(f"Découvrez l'élégance traditionnelle marocaine sur la page dédiée à {nom_v}.")

        elif intent == 'artisanat':
            arts = data.get('artisanat', [])
            lines.append(f"🏺 **Artisanat traditionnel de {nom_v} :**")
            if arts:
                for a in arts:
                    lines.append(f"• **{a['nom_artisanat']}** : {a.get('description', '')}")
            else:
                lines.append(f"Découvrez le savoir-faire artisanal local sur la fiche de {nom_v}.")

        else:
            # Présentation complète de la ville
            lines.append(f"📍 **{nom_v}** — *{slogan}* ({type_v})")
            if desc:
                lines.append(f"\n{desc}")
            atts = data.get('attractions', [])
            if atts:
                lines.append("\n🏛️ **À voir absolument :** " + ", ".join(a['nom'] for a in atts[:5]) + ".")
            acts = data.get('activites', [])
            if acts:
                lines.append("🎯 **Activités :** " + " | ".join(a['description'][:60] for a in acts[:3]) + ".")
            specs = data.get('specialites', [])
            if specs:
                lines.append("🥘 **Spécialités culinaires :** " + ", ".join(s['nom_plat'] for s in specs[:5]) + ".")
            restos = data.get('restaurants', [])
            if restos:
                lines.append("🍽️ **Restaurants :** " + ", ".join(r['nom'] for r in restos[:4]) + ".")
            hebs = data.get('hebergements', [])
            if hebs:
                lines.append("🏨 **Hébergements :** " + ", ".join(h['nom'] for h in hebs[:4]) + ".")

    # ── 2. Questions par catégories de villes (Côtières, Désert, Montagne, Culturelles, Agricoles) ──
    elif any(k in msg_norm for k in ['cotiere', 'cotieres', 'cotier', 'mer', 'plage', 'ocean', 'atlantique', 'mediterranee', 'coastal', 'beach', 'bhar']):
        cotieres = [
            v for v in toutes_villes
            if any(p in _strip_accents(v.get('type_ville', '')) for p in ['cotier', 'cotiere'])
        ]
        lines.append("🌊 **Voici les villes côtières du Maroc disponibles sur Morocco Secrets :**\n")
        for v in cotieres:
            slogan = f" — *{v['slogan']}*" if v.get('slogan') else ""
            lines.append(f"• **{v['nom_ville']}**{slogan}")
        lines.append("\n💡 *Dites-moi le nom d'une ville (ex. Agadir, Essaouira, Tanger, Dakhla, Al Hoceima...) pour découvrir ses plages, activités, restaurants et hôtels !*")

    elif any(k in msg_norm for k in ['desert', 'sahara', 'saharienne', 'sahariennes', 'dune', 'dunes', 'merzouga', 'bivouac']):
        sahariennes = [
            v for v in toutes_villes
            if 'sahar' in _strip_accents(v.get('type_ville', ''))
        ]
        lines.append("🐪 **Explorer le Désert et le Sahara marocain avec Morocco Secrets :**\n")
        for v in sahariennes:
            slogan = f" — *{v['slogan']}*" if v.get('slogan') else ""
            desc_short = f"\n  {v['description'][:130]}..." if v.get('description') and len(v['description']) > 130 else (f"\n  {v['description']}" if v.get('description') else "")
            lines.append(f"• **{v['nom_ville']}**{slogan}{desc_short}")
        lines.append("\n✨ **Expériences phares dans le désert :** excursion à dos de chameau au coucher du soleil, nuit en bivouac berbère sous les étoiles à Merzouga ou Zagora, balade en 4x4 ou quad dans les dunes de l'Erg Chebbi, et découverte des kasbahs d'Ouarzazate.")

    elif any(k in msg_norm for k in ['montagne', 'atlas', 'rif', 'neige', 'ski', 'randonnee', 'trek']):
        montagnes = [
            v for v in toutes_villes
            if 'mont' in _strip_accents(v.get('type_ville', ''))
        ]
        lines.append("🏔️ **Voici les destinations de montagne au Maroc sur Morocco Secrets :**\n")
        for v in montagnes:
            slogan = f" — *{v['slogan']}*" if v.get('slogan') else ""
            lines.append(f"• **{v['nom_ville']}**{slogan}")
        lines.append("\n💡 *Idéal pour le trekking dans le Haut Atlas (Imlil), la fraîcheur des forêts de cèdres (Ifrane, Moyen Atlas) ou le ski à Oukaïmeden !*")

    elif any(k in msg_norm for k in ['culturelle', 'culturelles', 'imperiale', 'historique', 'patrimoine']):
        culturelles = [
            v for v in toutes_villes
            if any(p in _strip_accents(v.get('type_ville', '')) for p in ['cultur', 'histor'])
        ]
        lines.append("🕌 **Voici les villes culturelles et historiques du Maroc :**\n")
        for v in culturelles:
            slogan = f" — *{v['slogan']}*" if v.get('slogan') else ""
            lines.append(f"• **{v['nom_ville']}**{slogan}")
        lines.append("\n💡 *Posez-moi une question sur l'une de ces villes (ex. Marrakech, Fès, Chefchaouen, Meknès) pour découvrir ses monuments, souks et riads !*")

    elif any(k in msg_norm for k in ['agricole', 'agricoles', 'agriculture', 'berkane']):
        agricoles = [
            v for v in toutes_villes
            if 'agric' in _strip_accents(v.get('type_ville', ''))
        ]
        lines.append("🍊 **Villes agricoles du Maroc sur Morocco Secrets :**\n")
        for v in agricoles:
            slogan = f" — *{v['slogan']}*" if v.get('slogan') else ""
            lines.append(f"• **{v['nom_ville']}**{slogan}")

    # ── 3. Meilleure période pour visiter ────────────────────────────────────
    elif any(k in msg_norm for k in ['meilleure periode', 'quand visiter', 'periode', 'saison', 'climat', 'meteo', 'when to visit']):
        lines.append("☀️ **Meilleure période pour visiter le Maroc selon votre destination :**\n")
        lines.append("• **Printemps (mars à mai) & Automne (septembre à novembre) :** La période idéale pour tout le Maroc ! Températures douces parfaites pour les villes culturelles (**Marrakech, Fès, Meknès, Chefchaouen**) et les excursions dans le **Sahara (Merzouga, Zagora, Ouarzazate)**.")
        lines.append("• **Été (juin à août) :** Parfait pour profiter des **villes côtières** (**Agadir, Essaouira, Tanger, Al Hoceima, Saïdia, Dakhla, Oualidia**) et de la fraîcheur des **montagnes de l'Atlas (Ifrane, Imlil)**.")
        lines.append("• **Hiver (décembre à février) :** Idéal pour le soleil du Sud (**Agadir, Dakhla, Sahara**) ou la neige et le ski en montagne (**Ifrane, Oukaïmeden**).")
        evs = data.get('global_evenements', [])
        if evs:
            lines.append("\n🎭 **Quelques festivals et événements au Maroc :**")
            for e in evs[:4]:
                per = f" ({e['periode']})" if e.get('periode') else ""
                lines.append(f"• **{e['nom']}** à {e['nom_ville']}{per}")

    # ── 4. Gastronomie marocaine (général) ───────────────────────────────────
    elif intent in ('restaurants', 'specialites') or any(k in msg_norm for k in ['gastronomie', 'cuisine marocaine', 'plats marocains', 'que manger']):
        lines.append("🥘 **La Gastronomie Marocaine sur Morocco Secrets :**\n")
        lines.append("Chaque région du Maroc possède ses spécialités emblématiques :")
        specs = data.get('global_specialites', [])
        by_city = {}
        for s in specs:
            by_city.setdefault(s['nom_ville'], []).append(s['nom_plat'])
        for c_name, plats in list(by_city.items())[:8]:
            lines.append(f"• **{c_name}** : {', '.join(plats[:4])}")
        restos = data.get('global_restaurants', [])
        if restos:
            lines.append("\n🍽️ **Quelques tables incontournables :**")
            for r in restos[:5]:
                sp = f" ({r['specialites']})" if r.get('specialites') else ""
                lines.append(f"• **{r['nom']}** à {r['nom_ville']}{sp}")
        lines.append("\n💡 *Indiquez-moi une ville pour voir tous ses restaurants et spécialités !*")

    # ── 5. Transports au Maroc (général) ─────────────────────────────────────
    elif intent == 'transports' or any(k in msg_norm for k in ['deplacer', 'transport', 'comment voyager', 'train', 'bus', 'taxi']):
        lines.append("🚄 **Comment se déplacer au Maroc ?**\n")
        lines.append("Voici les principaux moyens de transport répertoriés dans nos villes :")
        lines.append("• **Train (ONCF & Al Boraq TGV) :** Relie rapidement Tanger, Kénitra, Rabat, Casablanca, Meknès, Fès, Marrakech et Oujda.")
        lines.append("• **Autocar interurbain (CTM & Supratours) :** Confortable et dessert toutes les villes, y compris Agadir, Essaouira, Chefchaouen, Dakhla, Merzouga et Ouarzazate.")
        lines.append("• **Petits Taxis & Grands Taxis :** Les petits taxis pour vos trajets en ville, et les grands taxis pour relier les villes et villages voisins.")
        lines.append("• **Tramway & Bus urbains :** Tramway moderne à **Rabat** et **Casablanca**, bus urbains dans toutes les grandes villes.")
        lines.append("• **Avion (Aéroports internationaux) :** Casablanca, Marrakech, Agadir, Tanger, Fès, Rabat, Oujda, Nador, Dakhla, Laâyoune, Ouarzazate, Essaouira, Al Hoceima.")
        lines.append("\n💡 *Précisez une ville pour connaître ses transports spécifiques !*")

    # ── 6. Attractions (général / bouton "Voir les attractions") ─────────────
    elif intent == 'attractions' or any(k in msg_norm for k in ['montre-moi les attractions', 'montre moi les attractions', 'voir les attractions']):
        atts = data.get('global_attractions', [])
        lines.append("🏛️ **Attractions incontournables au Maroc sur Morocco Secrets :**\n")
        for a in atts[:10]:
            t_str = f" ({a['type_attraction']})" if a.get('type_attraction') else ""
            lines.append(f"• **{a['nom']}** à **{a['nom_ville']}**{t_str}")
        lines.append("\n💡 *Vous pouvez demander les attractions d'une ville précise (ex. « Attractions à Fès » ou « Que visiter à Chefchaouen ») !*")

    # ── 7. Liste de toutes les villes (bouton "Explorer les villes") ─────────
    elif any(k in msg_norm for k in ['ville', 'villes', 'destinations', 'ou aller', 'explorer']):
        lines.append(f"🇲🇦 **Morocco Secrets vous fait découvrir {len(toutes_villes)} destinations au Maroc :**\n")
        cats = {
            '🌊 Villes côtières': ['cotier', 'cotiere'],
            '🕌 Villes culturelles & historiques': ['cultur', 'histor'],
            '🏔️ Villes de montagne': ['mont'],
            '🐪 Villes sahariennes': ['sahar'],
            '🍊 Villes agricoles': ['agric']
        }
        for label, pats in cats.items():
            matched = [
                v['nom_ville'] for v in toutes_villes
                if any(p in _strip_accents(v.get('type_ville', '')) for p in pats)
            ]
            if matched:
                lines.append(f"**{label} :** {', '.join(matched)}")
        lines.append("\n💡 *Écrivez le nom d'une ville pour connaître ses activités, monuments, restaurants et hébergements !*")

    # ── 8. Salutations / Aide / Général ──────────────────────────────────────
    else:
        lines.append("👋 **Bonjour ! Je suis Trippy, votre guide touristique Morocco Secrets.**\n")
        lines.append("Je peux vous renseigner sur **34 destinations marocaines** à partir de notre base de données :")
        lines.append("• 🌊 **Les villes par catégorie** : côtières, culturelles, de montagne, sahariennes ou agricoles")
        lines.append("• 🏛️ **Les attractions, monuments et activités** de chaque ville (ex. *« Activités à Marrakech »*, *« Que visiter à Chefchaouen ? »*)")
        lines.append("• 🥘 **La gastronomie et les restaurants** (ex. *« Restaurants à Fès »*, *« Gastronomie marocaine »*)")
        lines.append("• 🏨 **Les hébergements, événements, artisanat et transports**.\n")
        lines.append("Quelle ville ou catégorie souhaitez-vous explorer aujourd'hui ?")

    response = "\n".join(lines)

    if limit_info:
        if "mode anonyme" in limit_info:
            response += "\n\n💡 *Astuce : Créez un compte gratuitement (bouton « Se connecter » en haut à droite) pour sauvegarder vos conversations et bénéficier de 50 requêtes par jour !*"
        else:
            response += f"\n\n💡 *{limit_info}*"

    return response


# ============================================================
# POINT D'ENTRÉE PRINCIPAL
# ============================================================

def rag_retrieve_and_augment(user_message: str, limit_info: str = None, history: list = None) -> dict:
    """
    Appelé par app.py pour chaque message du chatbot.
    Retourne : system_prompt, user_prompt, fallback_response, ville_detec, intent_detec, has_context
    """
    # 1. Retrieval — chercher dans MySQL
    context = retrieve_context(user_message, history=history)

    # 2. Augmentation — construire le prompt enrichi
    system_prompt, user_prompt = build_augmented_prompt(user_message, context, limit_info)

    # 3. Réponse directe basée sur MySQL (utilisée si l'API Groq est indisponible ou expirée)
    fallback_response = generate_fallback_response(user_message, context, limit_info)

    return {
        'system_prompt':    system_prompt,
        'user_prompt':      user_prompt,
        'fallback_response': fallback_response,
        'ville_detec':      context.get('ville'),
        'intent_detec':     context.get('intent'),
        'has_context':      bool(context.get('data'))
    }
