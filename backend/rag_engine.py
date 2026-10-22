"""
RAG Engine pour Trippy — Guide Touristique Maroc
Retrieval-Augmented Generation depuis MySQL

Fonctionnement :
  1. retrieve_context()      → interroge MySQL selon le message
  2. build_augmented_prompt() → injecte les données dans le system prompt
  3. rag_retrieve_and_augment() → point d'entrée appelé par app.py
"""

import pymysql
import re
import os
import traceback
from difflib import SequenceMatcher
from db import DB_CONFIG


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
    'restaurants':  ['restaurant', 'manger', 'resto', 'cuisine', 'dîner',
                     'déjeuner', 'nourriture', 'gastronomie', 'food', 'eat'],
    'attractions':  ['attraction', 'visiter', 'monument', 'voir', 'touriste',
                     'site', 'musée', 'palais', 'mosquée', 'médina', 'visite'],
    'hebergements': ['hébergement', 'hotel', 'hôtel', 'dormir', 'riad',
                     'auberge', 'chambre', 'nuit', 'séjour', 'loger'],
    'activites':    ['activité', 'faire', 'sport', 'balade', 'randonnée',
                     'excursion', 'loisir', 'promenade'],
    'specialites':  ['spécialité', 'plat', 'tajine', 'couscous',
                     'gastronomie', 'local', 'typique', 'cuisine locale'],
    'evenements':   ['événement', 'festival', 'fête', 'célébration',
                     'agenda', 'manifestation', 'concert'],
    'transports':   ['transport', 'aller', 'train', 'bus', 'avion',
                     'taxi', 'voiture', 'comment arriver', 'route'],
    'vetements':    ['vêtement', 'habit', 'tenue', 'djellaba', 'caftan',
                     'artisanat', 'habit traditionnel', 'costume'],
    'artisanat':    ['artisanat', 'souk', 'poterie', 'zellige', 'tapis',
                     'cuir', 'bijoux', 'craft', 'marché'],
    'general':      []
}


def detect_intent(message: str) -> str:
    """Retourne l'intention principale du message."""
    msg = message.lower()
    scores = {
        intent: sum(1 for kw in keywords if kw in msg)
        for intent, keywords in INTENT_KEYWORDS.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else 'general'


# ============================================================
# DÉTECTION DE VILLE (exact + fuzzy)
# ============================================================

def detect_ville(message: str, villes_list: list) -> str | None:
    """
    Détecte une ville dans le message.
    D'abord correspondance exacte, puis fuzzy matching.
    """
    msg = message.lower()

    # 1. Correspondance exacte
    for ville in villes_list:
        if ville.lower() in msg:
            return ville

    # 2. Fuzzy matching (tolère les fautes de frappe)
    best_ville = None
    best_ratio = 0.0
    words = re.findall(r'\b\w+\b', msg)

    for word in words:
        if len(word) < 3:
            continue
        for ville in villes_list:
            ratio = SequenceMatcher(None, word, ville.lower()).ratio()
            if ratio > best_ratio and ratio >= 0.82:
                best_ratio = ratio
                best_ville = ville

    return best_ville


# ============================================================
# RETRIEVAL — Récupération du contexte MySQL
# ============================================================

def retrieve_context(message: str) -> dict:
    """
    Interroge MySQL et retourne les données pertinentes.
    """
    context = {
        'ville':     None,
        'intent':    'general',
        'data':      {},
        'all_villes': []
    }
    conn = get_db_connection()
    if not conn:
        return context

    try:
        with conn.cursor() as cur:
            # Liste de toutes les villes (pour la détection + suggestions)
            cur.execute("SELECT nom_ville FROM villes")
            villes_list = [r['nom_ville'] for r in cur.fetchall()]
            context['all_villes'] = villes_list

            # Détection ville & intention
            ville  = detect_ville(message, villes_list)
            intent = detect_intent(message)
            context['ville']  = ville
            context['intent'] = intent

            if ville:
                # ── Infos générales de la ville ──────────────────────────────
                cur.execute(
                    "SELECT nom_ville, slogan, description, type_ville, latitude, longitude "
                    "FROM villes WHERE nom_ville = %s", (ville,)
                )
                context['data']['ville_info'] = cur.fetchone()

                # ── Données selon l'intention ─────────────────────────────────

                if intent in ('restaurants', 'general'):
                    cur.execute(
                        "SELECT nom, description, specialites, lien "
                        "FROM restaurants WHERE nom_ville = %s LIMIT 6", (ville,)
                    )
                    context['data']['restaurants'] = cur.fetchall()

                if intent in ('attractions', 'general'):
                    cur.execute(
                        "SELECT nom, description, type_attraction FROM attractions "
                        "WHERE nom_ville = %s LIMIT 6", (ville,)
                    )
                    context['data']['attractions'] = cur.fetchall()

                if intent in ('hebergements', 'general'):
                    cur.execute(
                        "SELECT nom, etoiles, avis, lien FROM hebergements "
                        "WHERE nom_ville = %s LIMIT 5", (ville,)
                    )
                    context['data']['hebergements'] = cur.fetchall()

                if intent in ('activites', 'general'):
                    cur.execute(
                        "SELECT description FROM activites "
                        "WHERE nom_ville = %s LIMIT 5", (ville,)
                    )
                    context['data']['activites'] = cur.fetchall()

                if intent in ('specialites', 'general'):
                    cur.execute(
                        "SELECT nom_plat FROM specialites_ville "
                        "WHERE nom_ville = %s LIMIT 8", (ville,)
                    )
                    context['data']['specialites'] = cur.fetchall()

                if intent == 'evenements':
                    cur.execute(
                        "SELECT nom, periode, description FROM evenements "
                        "WHERE nom_ville = %s LIMIT 5", (ville,)
                    )
                    context['data']['evenements'] = cur.fetchall()

                if intent == 'transports':
                    cur.execute(
                        "SELECT type FROM transports WHERE nom_ville = %s", (ville,)
                    )
                    context['data']['transports'] = cur.fetchall()

                if intent == 'vetements':
                    cur.execute(
                        "SELECT nom, description FROM patrimoine_vestimentaire "
                        "WHERE nom_ville = %s LIMIT 5", (ville,)
                    )
                    context['data']['vetements'] = cur.fetchall()

                if intent == 'artisanat':
                    cur.execute(
                        "SELECT nom_artisanat, description FROM artisanat "
                        "WHERE nom_ville = %s LIMIT 5", (ville,)
                    )
                    context['data']['artisanat'] = cur.fetchall()

            else:
                # ── Pas de ville détectée → réponses générales ───────────────
                msg_lower = message.lower()

                if 'liste' in msg_lower and 'ville' in msg_lower:
                    cur.execute(
                        "SELECT nom_ville, type_ville FROM villes ORDER BY nom_ville"
                    )
                    context['data']['toutes_villes'] = cur.fetchall()

                elif 'type' in msg_lower or 'catégorie' in msg_lower:
                    cur.execute("SELECT DISTINCT type_ville FROM villes")
                    context['data']['types'] = cur.fetchall()

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

    # ── System prompt de base ────────────────────────────────────────────────
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

    # ── Construction du contexte ─────────────────────────────────────────────
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

    if data.get('toutes_villes'):
        ctx = "\n=== TOUTES LES VILLES MAROCAINES ===\n"
        for v in data['toutes_villes']:
            ctx += f"• {v['nom_ville']} ({v['type_ville']})\n"
        context_parts.append(ctx)

    if data.get('types'):
        types = [t['type_ville'] for t in data['types']]
        context_parts.append(f"\n=== TYPES DE VILLES ===\n{', '.join(types)}\n")

    # Liste des villes disponibles (toujours utile)
    if all_villes:
        context_parts.append(
            f"\n=== VILLES DANS NOTRE BASE ===\n{', '.join(all_villes)}\n"
        )

    # ── Assemblage final ──────────────────────────────────────────────────────
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
# POINT D'ENTRÉE PRINCIPAL
# ============================================================

def rag_retrieve_and_augment(user_message: str, limit_info: str = None) -> dict:
    """
    Appelé par app.py pour chaque message du chatbot.
    Retourne : system_prompt, user_prompt, ville_detec, intent_detec, has_context
    """
    # 1. Retrieval — chercher dans MySQL
    context = retrieve_context(user_message)

    # 2. Augmentation — construire le prompt enrichi
    system_prompt, user_prompt = build_augmented_prompt(user_message, context, limit_info)

    return {
        'system_prompt': system_prompt,
        'user_prompt':   user_prompt,
        'ville_detec':   context.get('ville'),
        'intent_detec':  context.get('intent'),
        'has_context':   bool(context.get('data'))
    }