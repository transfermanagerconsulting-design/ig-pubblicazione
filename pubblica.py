"""Gira su GitHub Actions ogni 5 minuti.

Legge la coda privata (repo ig-coda, file coda.json) e pubblica su Instagram
i reel il cui orario è passato. Il video sta come release asset privato in
ig-coda; per i pochi minuti della pubblicazione viene copiato con un nome
casuale nella release "tmp" di questo repo (Instagram vuole un link pubblico)
e poi cancellato.

Questo repo è pubblico: nei log non stampiamo mai caption né token.
"""
import base64, json, os, secrets, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

V = "v23.0"
TOKEN = os.environ["IG_TOKEN"]
IG_USER = os.environ["IG_USER_ID"]
OWNER = os.environ["GITHUB_REPOSITORY_OWNER"]
CODA_REPO = f"{OWNER}/ig-coda"
PUB_REPO = os.environ["GITHUB_REPOSITORY"]
MAX_RITARDO_ORE = 6  # oltre questo ritardo non pubblichiamo: segnaliamo errore


def gh(*args, capture=True):
    r = subprocess.run(["gh", *args], capture_output=capture, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh {args[0]} {args[1] if len(args) > 1 else ''} fallito: {r.stderr.strip()[:300]}")
    return r.stdout


def ig(method, path, params=None):
    params = dict(params or {}, access_token=TOKEN)
    url = f"https://graph.instagram.com/{V}/{path}"
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method), timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        msg = e.read().decode()[:400].replace(TOKEN, "***")
        raise RuntimeError(f"Instagram HTTP {e.code}: {msg}")


def leggi_coda():
    r = json.loads(gh("api", f"repos/{CODA_REPO}/contents/coda.json"))
    return json.loads(base64.b64decode(r["content"])), r["sha"]


def scrivi_coda(coda, sha, messaggio):
    body = json.dumps({
        "message": messaggio,
        "sha": sha,
        "content": base64.b64encode(json.dumps(coda, indent=2, ensure_ascii=False).encode()).decode(),
    })
    subprocess.run(["gh", "api", "-X", "PUT", f"repos/{CODA_REPO}/contents/coda.json", "--input", "-"],
                   input=body, text=True, capture_output=True, check=True)


def aggiorna_post(pid, **campi):
    # rilegge sempre la coda fresca: nel frattempo il Mac può averla modificata
    for _ in range(5):
        coda, sha = leggi_coda()
        for p in coda:
            if p["id"] == pid:
                p.update(campi)
        try:
            scrivi_coda(coda, sha, f"{pid}: {campi.get('stato')}")
            return
        except subprocess.CalledProcessError:
            time.sleep(3)
    raise RuntimeError("impossibile aggiornare coda.json")


def metti_online(locale, nomi_tmp):
    """Copia un file nella release pubblica "tmp" con nome casuale e ritorna l'URL firmato."""
    nome = secrets.token_hex(16) + os.path.splitext(locale)[1]
    dest = f"/tmp/{nome}"
    os.rename(locale, dest)
    gh("release", "upload", "tmp", dest, "-R", PUB_REPO)
    nomi_tmp.append(nome)
    os.remove(dest)
    link = f"https://github.com/{PUB_REPO}/releases/download/tmp/{nome}"
    return subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{redirect_url}", link],
                          capture_output=True, text=True).stdout.strip() or link


def aspetta(cid, minuti=10):
    for _ in range(minuti * 6):
        s = ig("GET", cid, {"fields": "status_code,status"})
        if s.get("status_code") == "FINISHED":
            return
        if s.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram ha rifiutato il contenuto: {s.get('status')}")
        time.sleep(10)
    raise RuntimeError(f"Instagram non ha finito di elaborare in {minuti} minuti")


def scarica(nome):
    locale = f"/tmp/scarico-{nome}"
    gh("release", "download", "video", "-R", CODA_REPO, "-p", nome, "-O", locale, "--clobber")
    return locale


def pubblica(p):
    nomi_tmp = []
    try:
        if p.get("tipo") == "carosello":
            figli = []
            for nome in p["files"]:
                url = metti_online(scarica(nome), nomi_tmp)
                cid = ig("POST", f"{IG_USER}/media", {"image_url": url, "is_carousel_item": "true"})["id"]
                aspetta(cid, 3)
                figli.append(cid)
            cid = ig("POST", f"{IG_USER}/media", {"media_type": "CAROUSEL", "children": ",".join(figli),
                                                  "caption": p.get("caption", "")})["id"]
        else:
            url = metti_online(scarica(p["file"]), nomi_tmp)
            params = {"media_type": "REELS", "video_url": url, "caption": p.get("caption", ""),
                      "share_to_feed": "true"}
            if p.get("copertina_ms") is not None:
                params["thumb_offset"] = str(p["copertina_ms"])
            cid = ig("POST", f"{IG_USER}/media", params)["id"]
        aspetta(cid)
        media = ig("POST", f"{IG_USER}/media_publish", {"creation_id": cid})["id"]
        link_post = ig("GET", media, {"fields": "permalink"}).get("permalink", "")
    finally:
        for nome in nomi_tmp:
            subprocess.run(["gh", "release", "delete-asset", "tmp", nome, "-R", PUB_REPO, "-y"],
                           capture_output=True)
    return media, link_post


def main():
    coda, _ = leggi_coda()
    adesso = datetime.now(timezone.utc)
    da_fare = [p for p in coda if p.get("stato") == "in coda"
               and datetime.fromisoformat(p["quando_utc"]) <= adesso]
    if not da_fare:
        print("Niente da pubblicare.")
        return
    errori = 0
    for p in sorted(da_fare, key=lambda x: x["quando_utc"]):
        pid = p["id"]
        ritardo = (adesso - datetime.fromisoformat(p["quando_utc"])).total_seconds() / 3600
        if ritardo > MAX_RITARDO_ORE:
            aggiorna_post(pid, stato="errore", errore=f"saltato: in ritardo di {ritardo:.1f} ore")
            print(f"{pid}: troppo in ritardo, NON pubblicato")
            errori += 1
            continue
        aggiorna_post(pid, stato="in pubblicazione")
        try:
            media, link = pubblica(p)
        except Exception as e:
            aggiorna_post(pid, stato="errore", errore=str(e)[:500])
            print(f"{pid}: ERRORE (dettagli in coda.json)")
            errori += 1
            continue
        aggiorna_post(pid, stato="pubblicato", media_id=media, link=link,
                      pubblicato_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        for nome in p.get("files") or [p["file"]]:  # i file non servono più nella coda
            subprocess.run(["gh", "release", "delete-asset", "video", nome, "-R", CODA_REPO, "-y"],
                           capture_output=True)
        print(f"{pid}: pubblicato")
    sys.exit(1 if errori else 0)  # job rosso = mail di GitHub


if __name__ == "__main__":
    main()
