#!/usr/bin/env python3
"""
Récupère les matchs (calendrier + résultats) des compétitions suivies via l'API
gratuite football-data.org, calcule automatiquement une analyse statistique
(forme récente, buts marqués/encaissés, confrontations directes, avantage du
terrain) et une suggestion de pari en double chance, puis écrit tout ça dans
data/matches.json.

Aucune cote n'est utilisée : la "confiance" et le pari suggéré viennent d'un
calcul statistique simple (transparent, pas une boîte noire), pas d'une IA ni
d'un bookmaker. Pense à toujours vérifier la cote réelle sur Winamax avant de
parier, et à garder un œil critique sur la suggestion automatique.

Ce script est fait pour tourner via GitHub Actions (voir
.github/workflows/update-matches.yml), mais tu peux aussi le lancer en local :

    export FOOTBALL_DATA_TOKEN="ta_cle_api"
    python3 scripts/fetch_matches.py
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

API_BASE = "https://api.football-data.org/v4"
TOKEN = os.environ.get("FOOTBALL_DATA_TOKEN")

# Codes de compétitions football-data.org.
# WC = Coupe du Monde, CL = Ligue des Champions, PL/PD/BL1/SA/FL1 = les 5 grands championnats.
# EL (Europa League) n'est pas garantie sur le plan gratuit : si l'API répond 403,
# le script l'ignore simplement et continue.
COMPETITIONS = {
    "WC":  "Coupe du Monde",
    "CL":  "Ligue des Champions",
    "PL":  "Premier League",
    "PD":  "Liga",
    "BL1": "Bundesliga",
    "SA":  "Serie A",
    "FL1": "Ligue 1",
    "EL":  "Europa League",
}

# Fenêtre de dates : de 3 jours en arrière (pour garder les derniers résultats)
# à 21 jours à venir (pour voir le calendrier proche).
DATE_FROM = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
DATE_TO   = (datetime.now(timezone.utc) + timedelta(days=21)).strftime("%Y-%m-%d")

# Nombre max de matchs à venir pour lesquels on calcule l'analyse poussée
# (forme + H2H = 2-3 appels API supplémentaires par match). On limite pour
# rester large sous la limite de 10 requêtes/minute du plan gratuit et pour
# que le job GitHub Actions ne tourne pas trop longtemps.
MAX_DEEP_ANALYSIS = 20

# Pause de sécurité entre CHAQUE appel API (secondes). Avec 10 req/min autorisées,
# une pause de 7s garantit ~8,5 requêtes/minute maximum : jamais de blocage.
SLEEP_BETWEEN_CALLS = 7

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "matches.json")

_team_form_cache = {}


def api_get(path):
    url = f"{API_BASE}{path}"
    req = Request(url, headers={"X-Auth-Token": TOKEN})
    try:
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            time.sleep(SLEEP_BETWEEN_CALLS)
            return data
    except HTTPError as e:
        time.sleep(SLEEP_BETWEEN_CALLS)
        if e.code == 403:
            print(f"[info] Accès refusé (plan gratuit) pour {path}")
        elif e.code == 429:
            print(f"[attention] Limite de requêtes atteinte pour {path}")
        else:
            print(f"[erreur] {path} -> HTTP {e.code}")
        return None
    except Exception as e:
        time.sleep(SLEEP_BETWEEN_CALLS)
        print(f"[erreur] {path} -> {e}")
        return None


def fetch_competition_matches(code):
    data = api_get(f"/competitions/{code}/matches?dateFrom={DATE_FROM}&dateTo={DATE_TO}")
    return data.get("matches", []) if data else []


def get_team_form(team_id):
    """Retourne la forme des 5 derniers matchs terminés d'une équipe : points,
    buts marqués, buts encaissés, et la date du dernier match joué (pour calculer
    le repos avant le prochain match). Mis en cache pour ne pas refaire le même
    appel si l'équipe apparaît dans plusieurs matchs analysés."""
    if team_id in _team_form_cache:
        return _team_form_cache[team_id]

    data = api_get(f"/teams/{team_id}/matches?status=FINISHED&limit=5")
    if not data or not data.get("matches"):
        result = None
    else:
        matches_sorted = sorted(data["matches"], key=lambda m: m.get("utcDate") or "")
        points, gf, ga, results = 0, 0, 0, []
        last_match_away_win = False
        for m in matches_sorted:
            is_home = m["homeTeam"]["id"] == team_id
            my_score = m["score"]["fullTime"]["home"] if is_home else m["score"]["fullTime"]["away"]
            opp_score = m["score"]["fullTime"]["away"] if is_home else m["score"]["fullTime"]["home"]
            if my_score is None or opp_score is None:
                continue
            gf += my_score
            ga += opp_score
            if my_score > opp_score:
                points += 3
                results.append("V")
            elif my_score == opp_score:
                points += 1
                results.append("N")
            else:
                results.append("D")
        # Détection de l'effet de relâchement : une victoire à l'extérieur au
        # dernier match peut annoncer un contrecoup (pas systématique, juste un
        # facteur de risque à signaler). On regarde spécifiquement le tout
        # dernier match joué (dernier élément une fois trié par date).
        if matches_sorted:
            last = matches_sorted[-1]
            last_is_home = last["homeTeam"]["id"] == team_id
            last_my_score = last["score"]["fullTime"]["home"] if last_is_home else last["score"]["fullTime"]["away"]
            last_opp_score = last["score"]["fullTime"]["away"] if last_is_home else last["score"]["fullTime"]["home"]
            if (last_my_score is not None and last_opp_score is not None
                    and not last_is_home and last_my_score > last_opp_score):
                last_match_away_win = True
        last_match_date = matches_sorted[-1].get("utcDate") if matches_sorted else None
        result = {
            "points": points, "goals_for": gf, "goals_against": ga,
            "results": results, "last_match_date": last_match_date,
            "last_match_away_win": last_match_away_win,
        }

    _team_form_cache[team_id] = result
    return result


def rest_days_before(form, upcoming_utc_date):
    """Nombre de jours de repos avant le prochain match. Renvoie None si la
    donnée manque (pas de matchs récents trouvés)."""
    if not form or not form.get("last_match_date") or not upcoming_utc_date:
        return None
    try:
        last = datetime.fromisoformat(form["last_match_date"].replace("Z", "+00:00"))
        upcoming = datetime.fromisoformat(upcoming_utc_date.replace("Z", "+00:00"))
        return (upcoming - last).days
    except Exception:
        return None


def get_venue_record(team_id, venue):
    """Calcule le bilan d'une équipe spécifiquement à domicile ou à l'extérieur
    sur ses 10 derniers matchs dans ce contexte : taux d'invincibilité (victoires
    + nuls) et série d'invincibilité en cours (nombre de matchs consécutifs,
    en partant du plus récent, sans défaite). Mis en cache par équipe+contexte."""
    cache_key = f"{team_id}_{venue}"
    if cache_key in _team_form_cache:
        return _team_form_cache[cache_key]

    data = api_get(f"/teams/{team_id}/matches?status=FINISHED&venue={venue}&limit=10")
    if not data or not data.get("matches"):
        result = None
    else:
        matches_sorted = sorted(data["matches"], key=lambda m: m.get("utcDate") or "", reverse=True)
        total = 0
        unbeaten = 0
        streak = 0
        streak_broken = False
        for m in matches_sorted:
            is_home = m["homeTeam"]["id"] == team_id
            my_score = m["score"]["fullTime"]["home"] if is_home else m["score"]["fullTime"]["away"]
            opp_score = m["score"]["fullTime"]["away"] if is_home else m["score"]["fullTime"]["home"]
            if my_score is None or opp_score is None:
                continue
            total += 1
            lost = my_score < opp_score
            if not lost:
                unbeaten += 1
                if not streak_broken:
                    streak += 1
            else:
                streak_broken = True
        result = {
            "matches_played": total,
            "unbeaten_count": unbeaten,
            "unbeaten_rate": round(unbeaten / total, 2) if total else None,
            "current_unbeaten_streak": streak,
        }

    _team_form_cache[cache_key] = result
    return result
    data = api_get(f"/matches/{match_id}/head2head?limit=5")
    if not data:
        return None
    agg = data.get("aggregates", {})
    return {
        "total_matches": agg.get("numberOfMatches", 0),
        "home_wins": agg.get("homeTeam", {}).get("wins", 0),
        "away_wins": agg.get("awayTeam", {}).get("wins", 0),
        "draws": agg.get("draws", 0),
    }


def compute_suggestion(home_form, away_form, h2h, rest_home, rest_away,
                        home_venue_record=None, away_venue_record=None):
    """Calcul transparent (pas une IA) : additionne des points de forme, un bonus
    de terrain, un ajustement selon l'historique direct, un ajustement de
    fatigue selon le nombre de jours de repos avant le match (les enchaînements
    à 3 jours, fréquents en février-avril avec la Ligue des Champions + les
    championnats + les coupes nationales qui se chevauchent, pèsent sur les
    organismes), un léger malus « effet de relâchement » pour une équipe qui
    vient de décrocher une victoire à l'extérieur (pas systématique, mais
    documenté : ex. Bayern vainqueur à Paris puis nul contre l'Union Berlin,
    Paris FC vainqueur à Monaco puis battu par Rennes), ET un bonus lié au
    taux d'invincibilité spécifique domicile/extérieur (une équipe increvable
    chez elle depuis X matchs, ou à l'inverse très friable à l'extérieur, ça
    compte). Renvoie None si pas assez de données.

    Important : ceci reste une formule statistique simple, pas une prédiction
    fiable à 100%. Le foot produit régulièrement des surprises (un petit
    Nation qui tient 0-0 face à un favori, par exemple) qu'aucune formule ne
    peut anticiper de façon fiable — la zone "pas de tendance claire" est donc
    volontairement large plutôt que de forcer un favori à chaque match.
    """
    if not home_form or not away_form:
        return None

    HOME_ADVANTAGE_BONUS = 2.0
    FATIGUE_THRESHOLD_SEVERE = 3   # 3 jours ou moins entre 2 matchs = enchaînement serré
    FATIGUE_THRESHOLD_LIGHT = 4
    FATIGUE_PENALTY_SEVERE = -1.8
    FATIGUE_PENALTY_LIGHT = -0.8
    FRESHNESS_BONUS = 0.5          # plus de 7 jours de repos = équipe fraîche
    LETDOWN_PENALTY = -1.0         # petit malus après une victoire marquante à l'extérieur

    score_home = home_form["points"] + HOME_ADVANTAGE_BONUS
    score_away = away_form["points"]

    # différentiel de buts sur les 5 derniers matchs
    score_home += (home_form["goals_for"] - home_form["goals_against"]) * 0.3
    score_away += (away_form["goals_for"] - away_form["goals_against"]) * 0.3

    # effet de relâchement : une victoire à l'extérieur juste avant peut annoncer
    # un contrecoup (pas systématique — Bayern/PSG puis nul contre l'Union
    # Berlin, Paris FC/Monaco puis défaite contre Rennes en sont des exemples,
    # mais ça n'arrive pas à chaque fois). On applique donc un malus léger,
    # pas éliminatoire.
    letdown_home = bool(home_form.get("last_match_away_win"))
    letdown_away = bool(away_form.get("last_match_away_win"))
    if letdown_home:
        score_home += LETDOWN_PENALTY
    if letdown_away:
        score_away += LETDOWN_PENALTY

    # taux d'invincibilité domicile/extérieur : une équipe increvable chez elle
    # (ou une équipe qui ne perd presque jamais à l'extérieur) mérite un bonus ;
    # le poids reste modéré pour ne pas écraser les autres facteurs.
    INVINCIBILITY_WEIGHT = 3.0
    STREAK_BONUS_PER_MATCH = 0.15
    STREAK_BONUS_CAP = 1.5

    if home_venue_record and home_venue_record.get("unbeaten_rate") is not None:
        score_home += home_venue_record["unbeaten_rate"] * INVINCIBILITY_WEIGHT
        score_home += min(home_venue_record["current_unbeaten_streak"] * STREAK_BONUS_PER_MATCH, STREAK_BONUS_CAP)
    if away_venue_record and away_venue_record.get("unbeaten_rate") is not None:
        score_away += away_venue_record["unbeaten_rate"] * INVINCIBILITY_WEIGHT
        score_away += min(away_venue_record["current_unbeaten_streak"] * STREAK_BONUS_PER_MATCH, STREAK_BONUS_CAP)

    # confrontations directes
    if h2h and h2h["total_matches"] > 0:
        h2h_diff = (h2h["home_wins"] - h2h["away_wins"]) / h2h["total_matches"]
        score_home += h2h_diff * 2
        score_away -= h2h_diff * 2

    # fatigue / enchaînement des matchs
    fatigue_flag_home = fatigue_flag_away = None
    if rest_home is not None:
        if rest_home <= FATIGUE_THRESHOLD_SEVERE:
            score_home += FATIGUE_PENALTY_SEVERE
            fatigue_flag_home = "enchaînement serré"
        elif rest_home <= FATIGUE_THRESHOLD_LIGHT:
            score_home += FATIGUE_PENALTY_LIGHT
            fatigue_flag_home = "repos réduit"
        elif rest_home >= 7:
            score_home += FRESHNESS_BONUS
            fatigue_flag_home = "fraîche"
    if rest_away is not None:
        if rest_away <= FATIGUE_THRESHOLD_SEVERE:
            score_away += FATIGUE_PENALTY_SEVERE
            fatigue_flag_away = "enchaînement serré"
        elif rest_away <= FATIGUE_THRESHOLD_LIGHT:
            score_away += FATIGUE_PENALTY_LIGHT
            fatigue_flag_away = "repos réduit"
        elif rest_away >= 7:
            score_away += FRESHNESS_BONUS
            fatigue_flag_away = "fraîche"

    diff = score_home - score_away

    # Zone "pas de tendance claire" volontairement large : le foot produit des
    # surprises régulièrement, une formule ne doit pas prétendre à une certitude
    # qu'elle n'a pas.
    if diff >= 5:
        pick, confidence = "Double chance 1X (équipe à domicile ou nul)", "Élevée"
    elif diff >= 2.5:
        pick, confidence = "Double chance 1X (équipe à domicile ou nul)", "Moyenne"
    elif diff <= -5:
        pick, confidence = "Double chance X2 (équipe à l'extérieur ou nul)", "Élevée"
    elif diff <= -2.5:
        pick, confidence = "Double chance X2 (équipe à l'extérieur ou nul)", "Moyenne"
    else:
        pick, confidence = "Match équilibré / risque de surprise — aucune tendance statistique fiable", "Faible"

    # signal explicite si un déséquilibre de fatigue ou de relâchement va à
    # l'encontre du favori statistique
    surprise_risk = False
    if fatigue_flag_home == "enchaînement serré" and diff > 0:
        surprise_risk = True
    if fatigue_flag_away == "enchaînement serré" and diff < 0:
        surprise_risk = True
    if letdown_home and diff > 0:
        surprise_risk = True
    if letdown_away and diff < 0:
        surprise_risk = True

    return {
        "suggested_pick": pick,
        "confidence": confidence,
        "score_diff": round(diff, 1),
        "rest_days_home": rest_home,
        "rest_days_away": rest_away,
        "fatigue_home": fatigue_flag_home,
        "fatigue_away": fatigue_flag_away,
        "letdown_home": letdown_home,
        "letdown_away": letdown_away,
        "home_venue_record": home_venue_record,
        "away_venue_record": away_venue_record,
        "surprise_risk": surprise_risk,
    }


def normalize(match, competition_name, deep=False):
    home = match.get("homeTeam", {}) or {}
    away = match.get("awayTeam", {}) or {}
    score = match.get("score", {}).get("fullTime", {}) or {}

    entry = {
        "competition": competition_name,
        "utcDate": match.get("utcDate"),
        "status": match.get("status"),
        "matchday": match.get("matchday"),
        "stage": match.get("stage"),
        "homeTeam": home.get("name"),
        "homeCrest": home.get("crest"),
        "awayTeam": away.get("name"),
        "awayCrest": away.get("crest"),
        "homeScore": score.get("home"),
        "awayScore": score.get("away"),
        "analysis": None,
    }

    if deep and home.get("id") and away.get("id"):
        home_form = get_team_form(home["id"])
        away_form = get_team_form(away["id"])
        h2h = get_head_to_head(match.get("id"))
        rest_home = rest_days_before(home_form, match.get("utcDate"))
        rest_away = rest_days_before(away_form, match.get("utcDate"))
        home_venue_record = get_venue_record(home["id"], "HOME")
        away_venue_record = get_venue_record(away["id"], "AWAY")
        suggestion = compute_suggestion(
            home_form, away_form, h2h, rest_home, rest_away,
            home_venue_record, away_venue_record,
        )
        if suggestion:
            entry["analysis"] = {
                "home_form": home_form,
                "away_form": away_form,
                "head_to_head": h2h,
                **suggestion,
            }

    return entry


def main():
    if not TOKEN:
        print("ERREUR : la variable d'environnement FOOTBALL_DATA_TOKEN n'est pas définie.")
        sys.exit(1)

    raw_matches = []  # (raw_match_dict, competition_name)
    for code, name in COMPETITIONS.items():
        for m in fetch_competition_matches(code):
            raw_matches.append((m, name))

    # on choisit les prochains matchs non encore joués, triés par date, pour
    # leur appliquer l'analyse statistique poussée (dans la limite fixée plus haut)
    upcoming = sorted(
        [rm for rm in raw_matches if rm[0].get("status") in ("SCHEDULED", "TIMED")],
        key=lambda rm: rm[0].get("utcDate") or ""
    )
    deep_ids = {rm[0]["id"] for rm in upcoming[:MAX_DEEP_ANALYSIS]}

    all_matches = [
        normalize(m, name, deep=(m.get("id") in deep_ids))
        for m, name in raw_matches
    ]
    all_matches.sort(key=lambda m: m["utcDate"] or "")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "date_from": DATE_FROM,
        "date_to": DATE_TO,
        "count": len(all_matches),
        "deep_analysis_count": len(deep_ids),
        "matches": all_matches,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"OK : {len(all_matches)} matchs écrits, dont {len(deep_ids)} avec analyse poussée -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
