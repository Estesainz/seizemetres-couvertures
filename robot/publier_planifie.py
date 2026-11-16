"""Robot de publication planifiée @seizemetres (GitHub Actions, tourne sans session Claude ni ordinateur allumé).

Chaque fichier planning/*.json = un Reel déjà préparé (conteneur Instagram FINISHED, valable 24 h) :
  {"titre": "...", "container_id": "...", "heure_utc": "2026-11-17T17:04:00Z", "fichier_depot": "xxx.mp4" | null}
Le robot publie chaque entrée à son heure, écrit le résultat dans le fichier ("statut", "permalink"),
et retire la vidéo du dépôt public. Anti-doublon : un conteneur déjà PUBLISHED n'est jamais republié.
Une exécution attend (sleep) jusqu'à 5 h 40 la prochaine heure prévue ; le cron toutes les 15 min prend le relais au-delà.
"""
import glob, json, os, subprocess, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone

TOK = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
G = "https://graph.facebook.com/v21.0"
ATTENTE_MAX = 5 * 3600 + 40 * 60
DEBUT = time.time()

def api(path, data=None):
    if data is None:
        r = urllib.request.Request(f"{G}/{path}{'&' if '?' in path else '?'}access_token={TOK}")
    else:
        r = urllib.request.Request(f"{G}/{path}", data=urllib.parse.urlencode({**data, "access_token": TOK}).encode())
    try:
        with urllib.request.urlopen(r, timeout=60) as f: return json.load(f)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {e.read().decode()[:400]}")

def git(*a, check=True): return subprocess.run(["git", *a], check=check)

def enregistrer(chemin, e, message):
    json.dump(e, open(chemin, "w"), ensure_ascii=False, indent=1)
    git("add", "-A"); git("commit", "-q", "-m", message)
    for essai in range(4):
        if git("pull", "-q", "--rebase", "origin", "main", check=False).returncode == 0 and git("push", "-q", "origin", "main", check=False).returncode == 0: return
        time.sleep(5 * (essai + 1))

def heure(e): return datetime.fromisoformat(e["heure_utc"].replace("Z", "+00:00")).timestamp()

# Créneau à plus de 23 h : pas de conteneur à l'avance (il expire en 24 h). Le robot le crée lui-même 15 min avant
# l'heure, à partir de video_url + cover_url + legende (vidéo et couverture déjà en ligne sur GitHub Pages).
AVANCE_CONTENEUR = 15 * 60
def heure_action(e): return heure(e) - (0 if e.get("container_id") else AVANCE_CONTENEUR)

def creer_conteneur(e):
    base = {"media_type": "REELS", "caption": e["legende"], "share_to_feed": "true", "video_url": e["video_url"]}
    base.update({"cover_url": e["cover_url"]} if e.get("cover_url") else {"thumb_offset": "500"})
    cid = api(f"{os.environ['IG_USER_ID']}/media", base)["id"]; print("conteneur", cid, flush=True)
    t0 = time.time()
    while time.time() - t0 < 600:
        s = api(f"{cid}?fields=status_code,status")
        if s.get("status_code") == "FINISHED": return cid
        if s.get("status_code") in ("ERROR", "EXPIRED"): raise RuntimeError(f"Meta : {s}")
        time.sleep(5)
    raise RuntimeError("traitement Meta > 10 min")

def en_attente():
    out = []
    for c in sorted(glob.glob("planning/*.json")):
        e = json.load(open(c))
        if not e.get("statut"): out.append((heure_action(e), c, e))
    return sorted(out)

def publier(chemin, e):
    print("Publication", e["titre"], e["container_id"], flush=True)
    if e.get("essai_a_blanc"):
        try: compte = api(f"{os.environ['IG_USER_ID']}?fields=username").get("username") if TOK else "CLÉ ABSENTE"
        except Exception as err: compte = f"CLÉ REFUSÉE {str(err)[:200]}"
        e.update(statut="TEST OK", compte=compte, publie_le=datetime.now(timezone.utc).isoformat()); enregistrer(chemin, e, f"Test robot {e['titre']}"); return
    try:
        if not e.get("container_id") and not e.get("essai_a_blanc"):
            e["container_id"] = creer_conteneur(e)
            enregistrer(chemin, e, f"Robot : conteneur prêt {e['titre']}")
            while time.time() < heure(e): time.sleep(min(heure(e) - time.time(), 30))
        s = api(f"{e['container_id']}?fields=status_code")
        if s.get("status_code") == "PUBLISHED":
            e.update(statut="DEJA PUBLIE")
        else:
            for essai in range(3):
                try: p = api(f"{os.environ['IG_USER_ID']}/media_publish", {"creation_id": e["container_id"]}); break
                except RuntimeError as err:
                    print("essai", essai, err, flush=True); time.sleep(20)
                    if api(f"{e['container_id']}?fields=status_code").get("status_code") == "PUBLISHED": p = None; break
            else: raise RuntimeError("media_publish refusé 3 fois")
            m = api(f"{p['id']}?fields=permalink,timestamp") if p else {}
            e.update(statut="PUBLIE", permalink=m.get("permalink"), publie_le=m.get("timestamp"))
    except Exception as err:
        e.update(statut="ECHEC", erreur=str(err)[:500])
    if e["statut"] in ("PUBLIE", "DEJA PUBLIE") and e.get("fichier_depot") and os.path.exists(e["fichier_depot"]):
        git("rm", "-q", e["fichier_depot"])
    enregistrer(chemin, e, f"Robot : {e['titre']} -> {e['statut']}")
    print(json.dumps(e, ensure_ascii=False), flush=True)

while True:
    file = en_attente()
    if not file: print("Rien à publier."); break
    t, chemin, e = file[0]
    reste = t - time.time()
    if reste <= 0: publier(chemin, e); continue
    if time.time() + reste - DEBUT > ATTENTE_MAX: print(f"Prochaine publication dans {reste/60:.0f} min : le cron prendra le relais."); break
    print(f"Attente {reste:.0f} s jusqu'à {e['heure_utc']} ({e['titre']})", flush=True)
    time.sleep(min(reste, 600))
