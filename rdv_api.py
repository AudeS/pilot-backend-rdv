# -*- coding: utf-8 -*-
"""
API de réservation pour le widget rdv (widget-rdv.js) — style iClosed.

Zéro dépendance : stdlib Python uniquement (http.server, urllib, zoneinfo).

    python rdv_api.py          → http://127.0.0.1:8877

Deux modes, choisis automatiquement :
  - MOCK  : pas de clé cal.com → créneaux simulés (lun-ven 9h-18h, préavis 12 h,
            horizon 5 j, pas de 30 min). Les résas sont stockées dans
            rdv-bookings.json → le créneau disparaît. Parfait pour tester
            le widget en local sans aucun compte.
  - CAL   : variables CAL_API_KEY + CAL_EVENT_TYPE_ID définies
            → proxy de l'API cal.com v2 (dispos réelles + vraie réservation :
            agenda bloqué, e-mails, lien Meet, rappels 24 h/1 h).

Variables d'environnement (voir .env.example) :
  CAL_API_KEY        clé cal.com (cal_live…) — SECRET
  CAL_EVENT_TYPE_ID  id numérique de l'événement cal.com
  NTFY_TOPIC         topic ntfy.sh pour les notifications push (optionnel)
  NOTIF_TZ           fuseau IANA pour l'heure affichée dans les notifs
                     (défaut Europe/Paris)
  EVENT_TITRE / EVENT_DUREE_MIN / EVENT_DESCRIPTION  affichage du widget
  TEST_EMAILS / TEST_TELS  vos propres coordonnées (csv) → vos tests ne
                     déclenchent ni notification ni enregistrement de lead
  DATA_DIR           dossier des fichiers de données (défaut : dossier du script)
  EST_PROD=1         force le lancement des veilleurs hors Render

Endpoints (CORS ouvert : le widget peut appeler depuis n'importe quel domaine) :
  GET  /api/rdv/config              → mode, infos événement, questions
  GET  /api/rdv/slots?month=YYYY-MM&tz=Europe/Paris
  POST /api/rdv/lead                → capture partielle (lead incomplet)
  POST /api/rdv/book                → {slot, name, email, tel, answers}
  POST /api/optin                   → {prenom, email, tel, page, utm}
"""
import hashlib
import json
import os
import re
import sys
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))

# Répertoire de données : sur Render, pointer DATA_DIR vers un disque persistant
# (ex. /var/data) pour que leads/dédup survivent aux redéploiements. Défaut = local.
DATA_DIR = os.environ.get("DATA_DIR", HERE)
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = HERE
BOOKINGS_FILE = os.path.join(DATA_DIR, "rdv-bookings.json")
LEADS_FILE = os.path.join(DATA_DIR, "rdv-leads.json")
OPTIN_FILE = os.path.join(DATA_DIR, "optin-leads.json")
RAPPELS_FILE = os.path.join(DATA_DIR, "rdv-rappels.json")
VUS_FILE = os.path.join(DATA_DIR, "rdv-vus.json")
PORT = int(os.environ.get("PORT", "8877"))   # Render fournit $PORT en prod

CAL_API_KEY = os.environ.get("CAL_API_KEY", "")
CAL_EVENT_TYPE_ID = os.environ.get("CAL_EVENT_TYPE_ID", "")
MODE = "cal" if (CAL_API_KEY and CAL_EVENT_TYPE_ID) else "mock"

# Seule la PROD (Render définit RENDER=true tout seul) lance les veilleurs :
# notifs ntfy, rappels, détecteur de nouveaux RDV. Une instance locale qui poll
# cal.com avec les mêmes clés doublerait chaque notification. En local le serveur
# reste utilisable pour tester le widget, mais muet. Forçable : EST_PROD=1.
EST_PROD = bool(os.environ.get("RENDER") or os.environ.get("EST_PROD"))

# ---------------------------------------------------- notifications (ntfy)
# Push gratuit sur téléphone : app ntfy abonnée au topic NTFY_TOPIC.
# Un lead INCOMPLET = il a laissé un tél ou un e-mail valide, n'a PAS réservé,
# et plus aucune activité depuis DELAI_SORTIE_S (onglet fermé) ou DELAI_INACTIF_S.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
DELAI_SORTIE_S = 2 * 60
DELAI_INACTIF_S = 2 * 60
LEADS_LOCK = threading.Lock()
OPTIN_LOCK = threading.Lock()

# Fuseau du PROPRIÉTAIRE du calendrier : c'est l'heure affichée dans les
# notifications ("mercredi 23/07 à 17h00"). Le visiteur, lui, voit toujours
# son propre fuseau dans le widget.
NOTIF_TZ_NAME = os.environ.get("NOTIF_TZ", "Europe/Paris")
try:
    from zoneinfo import ZoneInfo
    TZ_PROPRIO = ZoneInfo(NOTIF_TZ_NAME)
except Exception:
    # zoneinfo indisponible (vieux Python / base tzdata absente) → offset fixe
    TZ_PROPRIO = timezone(timedelta(hours=int(os.environ.get("NOTIF_TZ_OFFSET", "1"))))


def notifier(titre, corps, tags="warning", priorite="high"):
    if not NTFY_TOPIC:
        return False
    try:
        # un en-tête HTTP n'accepte que l'ASCII : titre accentué → RFC 2047,
        # que ntfy décode côté serveur (les accents s'affichent sur le tél)
        try:
            titre.encode("ascii")
        except UnicodeEncodeError:
            from email.header import Header
            titre = Header(titre, "utf-8").encode()
        req = urllib.request.Request(
            "https://ntfy.sh/" + NTFY_TOPIC,
            data=corps.encode("utf-8"), method="POST",
            headers={"Title": titre, "Tags": tags, "Priority": priorite})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        sys.stderr.write("[rdv] notification impossible : %s\n" % e)
        return False


JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def format_slot_fr(iso):
    """'2026-07-23T10:00:00Z' → 'mercredi 23/07 à 12h00 (Europe/Paris)'."""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ_PROPRIO)
        return "%s %02d/%02d à %02dh%02d (%s)" % (
            JOURS_FR[t.weekday()], t.day, t.month, t.hour, t.minute, NOTIF_TZ_NAME)
    except Exception:
        return iso


def label_court(slug):
    """Slug cal.com interminable → étiquette courte pour la notification."""
    return slug.replace("-", " ").strip()[:34]


def source_label(utm):
    """Déduit le canal d'acquisition depuis l'utm/src (défaut = Facebook).
    Le visiteur n'a rien à préciser : YouTube → lien avec ?utm_source=youtube
    (ou ?src=youtube) ; Insta → ?src=ig ; Meta ads → fbclid ou aucun signal."""
    utm = utm or {}
    blob = ((utm.get("utm_source") or "") + " " + (utm.get("src") or "")
            + " " + (utm.get("utm_medium") or "")).lower()
    if "youtu" in blob or "yt" in blob.split():
        return "YouTube"
    if "insta" in blob or "ig" in blob.split():
        return "Instagram"
    if "linkedin" in blob or "li" in blob.split():
        return "LinkedIn"
    if "manuel" in blob or "direct" in blob:
        return "Direct / manuel"
    return "Facebook"  # fbclid, fb/meta, ou aucun signal → défaut


# ------------------------------------------------ Meta Conversions API
# Envoi SERVEUR de l'évènement "Lead" à Meta, en complément du pixel client.
# Pourquoi : le pixel du navigateur (lp.html/merci.html) ne se déclenche que si
# le visiteur clique "Accepter" sur le bandeau cookies — et sur la page "C'est
# réservé", une fois l'objectif atteint, beaucoup ne cliquent jamais ce bandeau.
# Le rendez-vous est pourtant bien réel. Ce chemin serveur est indépendant du
# navigateur/bloqueur de pub/bandeau : c'est la méthode que Meta recommande
# pour ce cas précis (voir doctrine média buying, "erreur n°10").
META_PIXEL_ID = os.environ.get("META_PIXEL_ID", "1295267600331261")
META_CAPI_TOKEN = os.environ.get("META_CAPI_TOKEN", "")


def _hash_meta(valeur):
    return hashlib.sha256(valeur.strip().lower().encode("utf-8")).hexdigest()


def meta_lead(p, headers=None, client_ip=None):
    """Miroir serveur de la logique client (widget-rdv.js, rendOk()) : jamais
    pour un test du propriétaire, jamais pour un lead hors cible (réponse
    chiffre d'affaires = "pas encore vendu" → même logique que la redirection
    vers merci-attente.html côté front, qui elle non plus ne déclenche pas Lead)."""
    if not META_PIXEL_ID or not META_CAPI_TOKEN:
        return
    if est_test(p.get("name", ""), p.get("email", ""), p.get("tel", "")):
        return
    ca = (p.get("answers") or {}).get("chiffre-affaires") or ""
    if re.search(r"pas encore vendu", ca, re.I):
        return
    user_data = {"country": [_hash_meta("fr")]}
    if p.get("email"):
        user_data["em"] = [_hash_meta(p["email"])]
    if p.get("tel"):
        tel_digits = re.sub(r"\D", "", p["tel"])
        if tel_digits:
            user_data["ph"] = [_hash_meta(tel_digits)]
    morceaux = (p.get("name") or "").split()
    if morceaux:
        user_data["fn"] = [_hash_meta(morceaux[0])]
    if len(morceaux) > 1:
        user_data["ln"] = [_hash_meta(" ".join(morceaux[1:]))]
    # fbc (clic sur la pub, via fbclid) et fbp (cookie pixel) : c'est ce qui relie
    # le rendez-vous à la pub cliquée dans le Gestionnaire de pubs. Jamais hachés.
    fbc = p.get("fbc") or ""
    fbclid = (p.get("utm") or {}).get("fbclid") or ""
    if not fbc and fbclid:
        fbc = "fb.1.%d.%s" % (int(datetime.now(timezone.utc).timestamp() * 1000), fbclid)
    if fbc:
        user_data["fbc"] = fbc
    if p.get("fbp"):
        user_data["fbp"] = p["fbp"]
    if client_ip:
        user_data["client_ip_address"] = client_ip
    if headers and headers.get("User-Agent"):
        user_data["client_user_agent"] = headers.get("User-Agent")
    evt = {
        "event_name": "Lead",
        "event_time": int(datetime.now(timezone.utc).timestamp()),
        "action_source": "website",
        "event_source_url": p.get("url") or "https://pilot-aude-lp.netlify.app/",
        "user_data": user_data,
    }
    # même event_id que le Lead navigateur de merci.html → dédoublonné par Meta
    if p.get("event_id"):
        evt["event_id"] = p["event_id"]
    corps = {"data": [evt]}
    try:
        req = urllib.request.Request(
            "https://graph.facebook.com/v21.0/%s/events" % META_PIXEL_ID,
            data=json.dumps(corps).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + META_CAPI_TOKEN})
        with urllib.request.urlopen(req, timeout=10) as r:
            rep = json.loads(r.read().decode())
            if not rep.get("events_received"):
                sys.stderr.write("[rdv] Meta CAPI : réponse inattendue %s\n" % rep)
    except Exception as e:
        sys.stderr.write("[rdv] Meta CAPI erreur : %s\n" % e)


# Bruit de test à ne PAS enregistrer/notifier (vos propres tests via le widget).
# Renseignez VOS coordonnées dans TEST_EMAILS / TEST_TELS (séparées par des virgules).
# NB : on filtre par e-mail/tél précis, jamais par prénom (un vrai prospect
# pourrait porter le même prénom que vous).
TEST_EMAILS = {e.strip().lower() for e in os.environ.get("TEST_EMAILS", "").split(",") if e.strip()}
TEST_TELS = {re.sub(r"\D", "", t) for t in os.environ.get("TEST_TELS", "").split(",") if t.strip()}


def est_test(name="", email="", tel=""):
    """Vrai si la résa/lead est un test du propriétaire (nom 'test', e-mail/tél listés)."""
    n = (name or "").lower()
    e = (email or "").strip().lower()
    t = re.sub(r"\D", "", tel or "")
    return ("test" in n or e in TEST_EMAILS or (bool(t) and t in TEST_TELS))


def lead_utm_by_contact(email, tel):
    """Retrouve l'utm d'un lead (rdv-leads.json) par e-mail OU téléphone : cal.com ne
    stocke pas l'utm, on croise donc côté notif RDV. Le tél compte car les leads
    incomplets laissent souvent le numéro (tél d'abord dans le formulaire), pas
    l'e-mail. On renvoie la PREMIÈRE source réelle trouvée."""
    try:
        with open(LEADS_FILE, encoding="utf-8") as f:
            leads = json.load(f)
    except Exception:
        return {}
    e = (email or "").strip().lower()
    t = re.sub(r"\D", "", tel or "")
    if not e and not t:
        return {}
    for l in leads:
        le = (l.get("email") or "").strip().lower()
        lt = re.sub(r"\D", "", l.get("tel") or "")
        if ((e and le == e) or (t and lt == t)) and (l.get("utm") or {}):
            return l["utm"]
    return {}


def resume_booking(p):
    """Corps de notification : qui, quand (lisible), la source, et ses réponses."""
    lignes = ["%s · %s" % (p.get("name", "?"), p.get("tel", "?")),
              "📅 " + format_slot_fr(p.get("slot", "?"))]
    utm = p.get("utm") or lead_utm_by_contact(p.get("email", ""), p.get("tel", ""))
    lignes.append("📍 Source : " + source_label(utm))
    for cle, val in (p.get("answers") or {}).items():
        if not val or not str(val).strip():
            continue  # questions masquées / non remplies : pas de bruit
        lignes.append("%s : %s" % (label_court(cle), val))
    return "\n".join(lignes)


def veilleur_leads():
    """Toutes les 60 s : notifie une seule fois chaque lead incomplet."""
    from datetime import datetime as dt
    while True:
        try:
            with LEADS_LOCK:
                try:
                    with open(LEADS_FILE, encoding="utf-8") as f:
                        leads = json.load(f)
                except Exception:
                    leads = []
                modifie = False
                maintenant = datetime.now(timezone.utc)
                # revérif cal.com : si la personne a réservé entre-temps, pas de notif
                booked_em, booked_tel = set(), set()
                try:
                    for b in bookings_a_venir():
                        if b.get("email"):
                            booked_em.add(b["email"].strip().lower())
                        if b.get("tel"):
                            booked_tel.add(re.sub(r"\D", "", b["tel"]))
                except Exception:
                    pass
                for l in leads:
                    if l.get("etape") == "reserve" or l.get("notifie"):
                        continue
                    try:
                        maj = datetime.fromisoformat(l["maj"])
                    except Exception:
                        continue
                    delai = DELAI_SORTIE_S if l.get("etape") == "sortie" else DELAI_INACTIF_S
                    if (maintenant - maj).total_seconds() < delai:
                        continue
                    em = (l.get("email") or "").strip().lower()
                    te = re.sub(r"\D", "", l.get("tel") or "")
                    if (em and em in booked_em) or (te and te in booked_tel):
                        # a réservé depuis : on classe et on n'envoie pas de "incomplet"
                        l["etape"] = "reserve"
                        modifie = True
                        continue
                    corps = "%s\n📞 %s\n✉️ %s\nArrêt : %s · page %s" % (
                        l.get("name") or "(sans nom)", l.get("tel") or "?",
                        l.get("email") or "?", l.get("etape") or "?",
                        l.get("page") or "?")
                    corps += "\n📍 Source : " + source_label(l.get("utm"))
                    if l.get("slot"):
                        corps += "\n📅 visait " + format_slot_fr(l["slot"])
                    if notifier("Lead incomplet à rappeler", corps):
                        l["notifie"] = dt.now(timezone.utc).isoformat()
                        modifie = True
                if modifie:
                    with open(LEADS_FILE, "w", encoding="utf-8") as f:
                        json.dump(leads, f, ensure_ascii=False, indent=2)
        except Exception as e:
            sys.stderr.write("[rdv] veilleur : %s\n" % e)
        threading.Event().wait(60)


# ------------------------------------------------- rappel perso avant call
# Notif ntfy au propriétaire ~1 h avant chaque rendez-vous (même corps que la résa).
# Un id notifié est mémorisé dans rdv-rappels.json → jamais deux fois.
RAPPEL_AVANT_S = 60 * 60
CHAMPS_SYSTEME = {"name", "email", "attendeePhoneNumber", "location",
                  "title", "notes", "guests", "rescheduleReason"}


def bookings_a_venir():
    if MODE == "cal":
        rep = cal_api("GET", "/v2/bookings?status=upcoming&eventTypeId="
                             + CAL_EVENT_TYPE_ID + "&take=50")
        out = []
        for b in (rep.get("data") or []):
            att = (b.get("attendees") or [{}])[0]
            reps = b.get("bookingFieldsResponses") or {}
            out.append({"id": str(b.get("uid") or b.get("id")),
                        "slot": b.get("start"),
                        "created": b.get("createdAt") or b.get("created"),
                        "meet": b.get("meetingUrl") or b.get("location"),
                        "name": att.get("name", ""),
                        "email": att.get("email", ""),
                        "tel": att.get("phoneNumber")
                               or reps.get("attendeePhoneNumber", ""),
                        "answers": {k: v for k, v in reps.items()
                                    if k not in CHAMPS_SYSTEME}})
        return out
    return [{"id": b["slot"], "slot": b["slot"], "name": b.get("name", ""),
             "email": b.get("email", ""), "tel": b.get("tel", ""),
             "utm": b.get("utm", {}), "answers": b.get("answers", {})}
            for b in lire_bookings()]


def veilleur_rappels():
    """Toutes les 2 min : rappel pour chaque call qui commence dans moins d'1 h."""
    while True:
        try:
            try:
                with open(RAPPELS_FILE, encoding="utf-8") as f:
                    deja = set(json.load(f))
            except Exception:
                deja = set()
            maintenant = datetime.now(timezone.utc)
            for b in bookings_a_venir():
                try:
                    t = datetime.fromisoformat(str(b["slot"]).replace("Z", "+00:00"))
                except Exception:
                    continue
                delta = (t - maintenant).total_seconds()
                if 0 < delta <= RAPPEL_AVANT_S and b["id"] not in deja:
                    if notifier("Call dans 1 h", resume_booking(b),
                                tags="telephone_receiver", priorite="high"):
                        deja.add(b["id"])
                        with open(RAPPELS_FILE, "w", encoding="utf-8") as f:
                            json.dump(sorted(deja), f)
        except Exception as e:
            sys.stderr.write("[rdv] rappels : %s\n" % e)
        threading.Event().wait(120)


# ---------------------------------------- détecteur de nouveaux RDV (robuste)
# Filet indépendant de la requête /book : poll cal.com toutes les 90 s et notifie
# tout RDV à venir jamais annoncé. Rattrape les notifs perdues (cold start Render).
# 1er passage (fichier absent / instance neuve) : seed SILENCIEUX pour ne pas
# re-spammer les RDV existants à chaque redémarrage.
# Anti-doublon par le temps : on n'annonce QUE les résas créées récemment. Une résa
# plus ancienne jamais vue (ex. dédup perdue au redémarrage) est marquée vue sans notif.
FENETRE_NOUVEAU_S = 30 * 60


def veilleur_nouveaux_rdv():
    premier = not os.path.exists(VUS_FILE)
    while True:
        try:
            try:
                with open(VUS_FILE, encoding="utf-8") as f:
                    vus = set(json.load(f))
            except Exception:
                vus = set()
            bookings = bookings_a_venir()
            if premier:
                vus |= {b["id"] for b in bookings}
                with open(VUS_FILE, "w", encoding="utf-8") as f:
                    json.dump(sorted(vus), f)
                premier = False
            else:
                maintenant = datetime.now(timezone.utc)
                for b in bookings:
                    if est_test(b.get("name", ""), b.get("email", ""), b.get("tel", "")):
                        if b["id"] not in vus:  # test du proprio : marquer vu, pas de notif
                            vus.add(b["id"])
                            with open(VUS_FILE, "w", encoding="utf-8") as f:
                                json.dump(sorted(vus), f)
                        continue
                    if b["id"] in vus:
                        continue
                    # anti-doublon par le temps : n'annoncer que les résas récentes
                    recent = True
                    cree = b.get("created")
                    if cree:
                        try:
                            t = datetime.fromisoformat(str(cree).replace("Z", "+00:00"))
                            recent = (maintenant - t).total_seconds() <= FENETRE_NOUVEAU_S
                        except Exception:
                            recent = True
                    envoye = True
                    if recent:
                        envoye = notifier("Nouveau rendez-vous", resume_booking(b),
                                          tags="tada", priorite="high")
                    # résa récente notifiée OU vieille résa jamais vue (on la tait) :
                    # dans les 2 cas on la marque vue pour ne jamais la ré-annoncer
                    if envoye:
                        vus.add(b["id"])
                        with open(VUS_FILE, "w", encoding="utf-8") as f:
                            json.dump(sorted(vus), f)
        except Exception as e:
            sys.stderr.write("[rdv] nouveaux rdv : %s\n" % e)
        threading.Event().wait(90)


# ------------------------------------------------------- règles (mode mock)
# Uniquement pour tester en local sans compte cal.com. En mode CAL, les
# disponibilités réelles viennent de cal.com et ces règles sont ignorées.
DUREE_MIN = int(os.environ.get("EVENT_DUREE_MIN", "30"))   # durée de l'appel
PREAVIS_H = 12          # préavis minimum
HORIZON_J = 5           # horizon de réservation
# (jour_semaine 0=lundi) → (heure_début, heure_fin) dans le fuseau du proprio
HORAIRES = {0: (9, 18), 1: (9, 18), 2: (9, 18), 3: (9, 18), 4: (9, 18)}

EVENT = {
    "titre": os.environ.get("EVENT_TITRE", "Appel découverte"),
    "duree": DUREE_MIN,
    "lieu": "Google Meet (lien envoyé par e-mail)",
    "description": os.environ.get("EVENT_DESCRIPTION", ""),
}

# Questions du mode MOCK (démo locale). En mode CAL, les vraies questions du
# formulaire cal.com (bookingFields) sont lues automatiquement — c'est donc
# dans cal.com qu'on configure ses questions, pas ici.
QUESTIONS_MOCK = [
    {"id": "activite", "type": "radio", "obligatoire": True,
     "label": "Quelle est ton activité ?",
     "options": ["Coach / consultant", "E-commerce", "Prestataire de services", "Autre"]},
    {"id": "engagement", "type": "radio", "obligatoire": True,
     "label": "Si tu réserves ce rendez-vous, est-ce que tu t'engages à venir ?",
     "options": ["Oui", "Non"]},
]


# ------------------------------------------------ questions réelles (cal)
_QUESTIONS_CAL = None
_CHAMPS_CAL = None
SLUGS_SYSTEME = {"name", "email", "location", "guests", "rescheduleReason",
                 "title", "notes", "attendeePhoneNumber", "unknown"}


def champs_cal():
    """bookingFields bruts de l'événement cal.com (cache mémoire)."""
    global _CHAMPS_CAL
    if _CHAMPS_CAL is None:
        rep = cal_api("GET", "/v2/event-types/" + CAL_EVENT_TYPE_ID,
                      version="2024-06-14")
        _CHAMPS_CAL = (rep.get("data") or {}).get("bookingFields", [])
    return _CHAMPS_CAL


def questions_cal():
    """Questions du VRAI formulaire cal.com (bookingFields non-système).
    Les ids sont les slugs cal.com → le widget renvoie ses réponses avec ces
    clés, et bookingFieldsResponses les accepte telles quelles."""
    global _QUESTIONS_CAL
    if _QUESTIONS_CAL is not None:
        return _QUESTIONS_CAL
    qs = []
    for f in champs_cal():
        if f.get("isDefault") or not f.get("slug") or f["slug"] in SLUGS_SYSTEME:
            continue
        if f.get("hidden"):
            continue  # question masquée dans cal.com → pas dans le widget non plus
        t = f.get("type")
        if t in ("radio", "select"):
            qs.append({"id": f["slug"], "type": "radio",
                       "obligatoire": bool(f.get("required")),
                       "label": f.get("label") or f["slug"],
                       "options": [o if isinstance(o, str) else o.get("label", "")
                                   for o in (f.get("options") or [])]})
        elif t in ("text", "textarea", "number"):
            qs.append({"id": f["slug"], "type": "textarea" if t == "textarea" else "text",
                       "obligatoire": bool(f.get("required")),
                       "label": f.get("label") or f["slug"]})
    _QUESTIONS_CAL = qs or QUESTIONS_MOCK
    return _QUESTIONS_CAL


# ---------------------------------------------------------------- helpers
def lire_bookings():
    try:
        with open(BOOKINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def ecrire_booking(b):
    data = lire_bookings()
    data.append(b)
    with open(BOOKINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def upsert_lead(p, etape):
    """Capture partielle : fusionne par leadId, garde la valeur la plus récente.
    C'est LA feature iClosed : un abandon à mi-parcours reste un lead contactable."""
    if est_test(p.get("name", ""), p.get("email", ""), p.get("tel", "")):
        return False  # test du proprio : on n'enregistre pas ce bruit
    with LEADS_LOCK:
        try:
            with open(LEADS_FILE, encoding="utf-8") as f:
                leads = json.load(f)
        except Exception:
            leads = []
        lid = p.get("leadId") or ""
        lead = next((l for l in leads if l.get("leadId") == lid), None) if lid else None
        if lead is None:
            lead = {"leadId": lid, "cree": datetime.now(timezone.utc).isoformat()}
            leads.append(lead)
        # "reserve" est un état final : un beacon "sortie" tardif ne doit pas le rétrograder
        if lead.get("etape") != "reserve" or etape == "reserve":
            lead["etape"] = etape
        if etape in ("formulaire", "creneau") and lead.get("notifie"):
            # il est revenu : on réarme le veilleur → un 2ᵉ abandon = nouvelle notif à jour
            lead.pop("notifie", None)
        for cle in ("name", "email", "tel", "slot", "tz", "page"):
            if p.get(cle):
                lead[cle] = p[cle]
        # source collante (first-touch) : on garde le PREMIER utm significatif
        if p.get("utm") and not (lead.get("utm") or {}):
            lead["utm"] = p["utm"]
        lead["maj"] = datetime.now(timezone.utc).isoformat()
        with open(LEADS_FILE, "w", encoding="utf-8") as f:
            json.dump(leads, f, ensure_ascii=False, indent=2)
    return True


def upsert_optin(p, email):
    """Base e-mails (opt-in) : un inscrit = une ligne, dédup par e-mail.
    Distincte de rdv-leads.json : un opt-in n'est PAS un abandon de résa,
    il ne doit pas déclencher le veilleur "lead incomplet"."""
    with OPTIN_LOCK:
        try:
            with open(OPTIN_FILE, encoding="utf-8") as f:
                leads = json.load(f)
        except Exception:
            leads = []
        lead = next((l for l in leads if l.get("email") == email), None)
        nouveau = lead is None
        if nouveau:
            lead = {"email": email, "cree": datetime.now(timezone.utc).isoformat()}
            leads.append(lead)
        for cle in ("prenom", "tel", "page"):
            if p.get(cle):
                lead[cle] = p[cle]
        if p.get("utm") and not (lead.get("utm") or {}):
            lead["utm"] = p["utm"]
        lead["maj"] = datetime.now(timezone.utc).isoformat()
        with open(OPTIN_FILE, "w", encoding="utf-8") as f:
            json.dump(leads, f, ensure_ascii=False, indent=2)
    return nouveau


def slots_bloques():
    """Instants UTC indisponibles : chaque résa bloque son créneau + le suivant."""
    bloques = set()
    for b in lire_bookings():
        try:
            t = datetime.fromisoformat(b["slot"].replace("Z", "+00:00"))
            bloques.add(t)
            bloques.add(t + timedelta(minutes=DUREE_MIN))
        except Exception:
            pass
    return bloques


def slots_mock(annee, mois):
    """Créneaux UTC (liste plate) couvrant le mois demandé ± 1 jour.
    Le widget regroupe par jour local du visiteur dans le navigateur."""
    maintenant = datetime.now(timezone.utc)
    ouverture = maintenant + timedelta(hours=PREAVIS_H)
    fermeture = maintenant + timedelta(days=HORIZON_J)
    bloques = slots_bloques()
    slots = []
    d = datetime(annee, mois, 1, tzinfo=TZ_PROPRIO) - timedelta(days=1)
    fin = (datetime(annee + (mois == 12), (mois % 12) + 1, 1, tzinfo=TZ_PROPRIO)
           + timedelta(days=1))
    while d < fin:
        plage = HORAIRES.get(d.weekday())
        if plage:
            h_debut, h_fin = plage
            t = d.replace(hour=h_debut, minute=0, second=0, microsecond=0)
            borne = d.replace(hour=h_fin, minute=0, second=0, microsecond=0)
            while t + timedelta(minutes=DUREE_MIN) <= borne:
                t_utc = t.astimezone(timezone.utc)
                if ouverture <= t_utc <= fermeture and t_utc not in bloques:
                    slots.append(t_utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
                t += timedelta(minutes=DUREE_MIN)
        d += timedelta(days=1)
    return sorted(slots)


# ---------------------------------------------------------------- cal.com
def cal_api(methode, chemin, corps=None, version="2024-08-13"):
    req = urllib.request.Request(
        "https://api.cal.com" + chemin,
        data=json.dumps(corps).encode() if corps else None,
        method=methode,
        headers={"Authorization": "Bearer " + CAL_API_KEY,
                 "cal-api-version": version,
                 "Content-Type": "application/json",
                 # Cloudflare devant api.cal.com renvoie 403 au User-Agent
                 # par défaut de urllib ("Python-urllib/...")
                 "User-Agent": "widget-rdv/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def slots_cal(annee, mois, tz_client):
    # cal.com renvoie vide si start est dans le passé → on borne à aujourd'hui
    auj = datetime.now(timezone.utc)
    a2, m2 = (annee + 1, 1) if mois == 12 else (annee, mois + 1)
    if datetime(a2, m2, 1, tzinfo=timezone.utc) <= auj:
        return []  # mois entièrement passé
    debut = f"{annee:04d}-{mois:02d}-01"
    if (annee, mois) == (auj.year, auj.month):
        debut = auj.strftime("%Y-%m-%d")
    fin = f"{a2:04d}-{m2:02d}-01"
    rep = cal_api("GET", f"/v2/slots?eventTypeId={CAL_EVENT_TYPE_ID}"
                         f"&start={debut}&end={fin}&timeZone={tz_client}",
                  version="2024-09-04")
    slots = []
    for liste in (rep.get("data") or {}).values():
        slots += [s["start"] if isinstance(s, dict) else s for s in liste]
    return sorted(slots)


def book_cal(p, tz_client):
    reponses = dict(p.get("answers") or {})
    # ⚠️ Piège cal.com : une question MASQUÉE mais toujours marquée OBLIGATOIRE
    # fait rejeter la résa. Config à respecter : masquée = facultative.
    # Garde-fou pour les champs texte seulement : un tiret. Les champs à choix
    # (radio/select) n'acceptent que leurs options → on ne peut rien inventer.
    try:
        for f in champs_cal():
            slug = f.get("slug")
            if (f.get("hidden") and f.get("required") and slug
                    and slug not in SLUGS_SYSTEME and not reponses.get(slug)
                    and f.get("type") in ("text", "textarea", "number")):
                reponses[slug] = "-"
    except Exception as e:
        sys.stderr.write("[rdv] champs masqués non pré-remplis : %s\n" % e)
    corps = {
        "start": p["slot"],
        "eventTypeId": int(CAL_EVENT_TYPE_ID),
        "attendee": {
            "name": p["name"].strip(),
            "email": p["email"],
            "phoneNumber": p.get("tel", ""),
            "timeZone": tz_client,
            "language": "fr",
        },
        "bookingFieldsResponses": reponses,
        "metadata": {"source": "widget-lp"},
    }
    return cal_api("POST", "/v2/bookings", corps)


# ---------------------------------------------------------------- serveur
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("[rdv] %s\n" % (fmt % args))

    def _json(self, code, obj):
        # un client qui coupe la connexion en pleine réponse (onglet fermé,
        # antivirus) ne doit ni cracher de traceback ni menacer le serveur
        try:
            corps = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corps)))
            self._cors()
            self.end_headers()
            self.wfile.write(corps)
        except (ConnectionError, BrokenPipeError, TimeoutError) as e:
            sys.stderr.write("[rdv] client parti en cours de réponse : %s\n" % e)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if not self.path.startswith("/api/"):
            # En prod (Render), on ne sert QUE l'API : jamais de fichiers du
            # dossier (sinon le code source serait téléchargeable). En local,
            # on sert les fichiers pour pouvoir tester demo.html.
            if EST_PROD:
                return self._json(404, {"erreur": "not found"})
            return super().do_GET()
        if self.path.startswith("/api/rdv/config"):
            questions = QUESTIONS_MOCK
            if MODE == "cal":
                try:
                    questions = questions_cal()
                except Exception as e:
                    sys.stderr.write("[rdv] questions cal.com indisponibles : %s\n" % e)
            return self._json(200, {"mode": MODE, "event": EVENT,
                                    "questions": questions,
                                    "ntfy": bool(NTFY_TOPIC)})
        if self.path.startswith("/api/rdv/slots"):
            q = dict(re.findall(r"[?&]([^=&]+)=([^&]*)", self.path))
            try:
                annee, mois = map(int, q.get("month", "").split("-"))
                tz_nom = urllib.parse.unquote(q.get("tz", "Europe/Paris"))
            except Exception:
                return self._json(400, {"erreur": "paramètres invalides"})
            try:
                slots = (slots_cal(annee, mois, tz_nom) if MODE == "cal"
                         else slots_mock(annee, mois))
                return self._json(200, {"slots": slots})
            except Exception as e:
                return self._json(502, {"erreur": str(e)})
        return self._json(404, {"erreur": "inconnu"})

    def do_POST(self):
        try:
            taille = int(self.headers.get("Content-Length", 0))
            brut = self.rfile.read(taille)
            try:
                p = json.loads(brut.decode("utf-8"))
            except UnicodeDecodeError:
                p = json.loads(brut.decode("latin-1"))
        except Exception:
            return self._json(400, {"erreur": "corps invalide"})

        # opt-in : base e-mails pour les relances (newsletter, masterclass…)
        if self.path.startswith("/api/optin"):
            email = (p.get("email") or "").strip().lower()
            if not re.match(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$", email):
                return self._json(400, {"erreur": "E-mail invalide"})
            nouveau = upsert_optin(p, email)
            if nouveau:
                utm = p.get("utm") or {}
                corps = "%s\n✉️ %s\n📞 %s" % (p.get("prenom") or "(sans prénom)",
                                              email, p.get("tel") or "?")
                corps += "\n📍 Source : " + source_label(utm)
                if utm.get("utm_campaign"):
                    corps += " · " + utm.get("utm_campaign")
                notifier("Nouvel inscrit 🎬", corps, tags="star", priorite="default")
            return self._json(200, {"ok": True, "nouveau": nouveau})

        # capture partielle de lead (aucune validation bloquante : on prend ce qu'on a)
        if self.path.startswith("/api/rdv/lead"):
            if not p.get("email") and not p.get("tel"):
                return self._json(400, {"erreur": "e-mail ou téléphone requis"})
            upsert_lead(p, p.get("etape") or "coordonnees")
            return self._json(200, {"ok": True})

        if not self.path.startswith("/api/rdv/book"):
            return self._json(404, {"erreur": "inconnu"})

        # validation serveur (mêmes règles que le front : le front peut mentir)
        if not re.match(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$", p.get("email", "")):
            return self._json(400, {"erreur": "E-mail invalide"})
        if not re.match(r"^\+?[\d\s.\-()]{9,20}$", p.get("tel", "")) \
           or not 9 <= len(re.sub(r"\D", "", p.get("tel", ""))) <= 15:
            return self._json(400, {"erreur": "Numéro de téléphone invalide"})
        if len(p.get("name", "").strip()) < 2:
            return self._json(400, {"erreur": "Nom obligatoire"})
        try:
            slot = datetime.fromisoformat(p["slot"].replace("Z", "+00:00"))
        except Exception:
            return self._json(400, {"erreur": "Créneau invalide"})

        tz_client = p.get("tz", "Europe/Paris")
        if MODE == "cal":
            try:
                rep = book_cal(p, tz_client)
                upsert_lead(p, "reserve")
                # notif "Nouveau rendez-vous" envoyée par veilleur_nouveaux_rdv (poll
                # cal.com) : plus fiable que dans la requête (cold start Render).
                ip_client = (self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                             or (self.client_address[0] if self.client_address else None))
                meta_lead(p, headers=self.headers, client_ip=ip_client)
                return self._json(200, {"ok": True, "cal": rep.get("status")})
            except urllib.error.HTTPError as e:
                detail = e.read().decode()[:400]
                bas = detail.lower()
                if "no_available" in bas or "already has booking" in bas:
                    return self._json(409, {"erreur": "Ce créneau vient d'être pris."})
                if "cannot receive mail" in bas or "email_domain" in bas:
                    return self._json(400, {"erreur": "Cette adresse e-mail semble "
                        "invalide (son domaine ne reçoit pas de courrier). "
                        "Vérifie l'adresse saisie."})
                sys.stderr.write("[rdv] refus cal.com : %s\n" % detail)
                return self._json(502, {"erreur": "La réservation a été refusée. "
                    "Réessaie, ou réserve via le lien de secours sous le calendrier."})
            except Exception as e:
                return self._json(502, {"erreur": str(e)})

        # mock : re-vérifie que le créneau est toujours libre, puis enregistre
        if slot in slots_bloques() or slot < datetime.now(timezone.utc) + timedelta(hours=PREAVIS_H):
            return self._json(409, {"erreur": "Ce créneau vient d'être pris."})
        ecrire_booking({"slot": p["slot"], "name": p["name"],
                        "email": p["email"], "tel": p["tel"], "tz": tz_client,
                        "answers": p.get("answers", {}), "utm": p.get("utm", {}),
                        "cree": datetime.now(timezone.utc).isoformat()})
        upsert_lead(p, "reserve")
        return self._json(200, {"ok": True, "mode": "mock"})


if __name__ == "__main__":
    print(f"Mode {MODE.upper()} : http://127.0.0.1:{PORT}/")
    if MODE == "mock":
        print("(pas de CAL_API_KEY/CAL_EVENT_TYPE_ID : créneaux simulés, aucun compte requis)")
    if not EST_PROD:
        print("Instance LOCALE (pas d'env RENDER) : veilleurs et notifs OFF, la prod s'en charge.")
    elif NTFY_TOPIC:
        print(f"Notifications : ntfy.sh/{NTFY_TOPIC}")
        threading.Thread(target=veilleur_leads, daemon=True).start()
        threading.Thread(target=veilleur_rappels, daemon=True).start()
        threading.Thread(target=veilleur_nouveaux_rdv, daemon=True).start()
        print("Veilleur leads incomplets : actif (scan 60 s)")
        print("Rappel perso 1 h avant chaque call : actif (scan 2 min)")
        print("Détecteur de nouveaux RDV : actif (poll 90 s)")
    else:
        print("(pas de NTFY_TOPIC : notifications désactivées)")
    try:
        serveur = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError:
        print(f"Port {PORT} déjà pris : une instance tourne déjà, rien à faire.")
        sys.exit(0)
    serveur.daemon_threads = True
    serveur.serve_forever()
