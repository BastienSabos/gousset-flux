#!/usr/bin/env python3
"""Construit, signe et vérifie les fichiers du flux Gousset (AND-NET-04, ADR 0037).

Fichiers écrits dans <sortie>/v1/ :
  taux.json            livrets réglementés (sources/livrets.json), inflation sur 12 mois (Insee, ou sources/manuel.json)
                       et indices de prix par poste de « Se situer » (Insee, sources/series.json)
  credit.json          taux moyen des nouveaux crédits à l'habitat (sources/manuel.json, ou Webstat avec une clé)
  logement-<dép>.json  ventes DVF de logements du département (data.gouv.fr, geo-dvf)

Chaque fichier est une enveloppe {"signature", "payload"} : payload est le texte JSON exact qui est signé
(ECDSA P-256 sur SHA-256, signature DER en Base64), comme l'attend l'application (Feed.kt).

Une version ne monte que si les données changent : sinon le fichier publié reste tel quel et l'application répond
« déjà à jour ». La version est la date de construction (AAAAMMJJ), toujours plus haute que la précédente.

Seulement la bibliothèque standard de Python 3.10+, plus la commande openssl.

Usage :
  FLUX_SIGNING_KEY="$(cat flux.pem)" python3 build.py --out public
  python3 build.py --out public --key flux.pem --dvf-dir essais/dvf --only taux,credit,logement-33
"""

import argparse
import base64
import csv
import datetime as dt
import gzip
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

FORMAT = 1
ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / "sources"

# Doit rester sous la borne de l'application (FeedSource.kt : 512 000 octets).
MAX_BYTES = 500_000
MAX_SALES = 200  # par commune et par type ; réduit si le fichier dépasse MAX_BYTES
MIN_SALES = 50
YEARS_BACK = 3
PLAUSIBLE_M2 = range(100, 100_001)
MIN_SURFACE = 9

DVF_URL = "https://files.data.gouv.fr/geo-dvf/latest/csv/{year}/departements/{dep}.csv.gz"
INSEE_URL = "https://bdm.insee.fr/series/sdmx/data/SERIES_BDM/{series}?lastNObservations=1"
INSEE_SINCE_URL = "https://bdm.insee.fr/series/sdmx/data/SERIES_BDM/{series}?startPeriod={since}"
# L'année des prix livrés dans l'application (ReferenceData.PRICES_YEAR) : les indices du flux s'expriment par rapport
# à sa moyenne, quelle que soit la base de la série Insee.
APK_PRICES_YEAR = 2025
# Postes de « Se situer » (ReferencePost dans l'application).
POSTS = ["ALIMENTATION", "HABILLEMENT", "LOGEMENT", "EQUIPEMENT", "SANTE", "TRANSPORTS", "COMMUNICATIONS", "LOISIRS",
         "ENSEIGNEMENT", "RESTAURATION", "AUTRES"]
WEBSTAT_URL = (
    "https://webstat.banque-france.fr/api/explore/v2.1/catalog/datasets/observations/records"
    "?where=series_key%3D%22{series}%22&order_by=time_period_end%20desc&limit=1"
)

# DVF ne couvre ni l'Alsace-Moselle (57, 67, 68) ni Mayotte (976) : l'application ne les demande pas.
DEPARTMENTS = (
    [f"{n:02d}" for n in range(1, 20)] + ["2A", "2B"] + [f"{n:02d}" for n in range(21, 96)] + ["971", "972", "973", "974"]
)
DEPARTMENTS = [d for d in DEPARTMENTS if d not in {"57", "67", "68"}]


def log(message):
    print(message, flush=True)


def fetch(url, headers=None, timeout=120):
    request = urllib.request.Request(url, headers={"User-Agent": "gousset-flux", **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def ppm(percent_text):
    """« 1,7 » ou « 1.70 » (en %) en millionièmes : 17 000."""
    value = float(str(percent_text).replace(",", "."))
    return round(value * 10_000)


# ---------- taux.json ----------

def insee_inflation(series):
    """Le dernier glissement annuel de l'IPC (série BDM de l'Insee) : (ppm, "AAAA-MM")."""
    root = ET.fromstring(fetch(INSEE_URL.format(series=series)))
    observations = [e for e in root.iter() if e.tag.endswith("Obs")]
    if not observations:
        raise ValueError("aucune observation")
    last = max(observations, key=lambda e: e.get("TIME_PERIOD"))
    return ppm(last.get("OBS_VALUE")), last.get("TIME_PERIOD")[:7]


def insee_monthly(series):
    """Les indices mensuels d'une série BDM depuis janvier de l'année de l'APK : {"AAAA-MM": valeur}."""
    url = INSEE_SINCE_URL.format(series=series, since=f"{APK_PRICES_YEAR}-01")
    root = ET.fromstring(fetch(url))
    return {
        e.get("TIME_PERIOD")[:7]: float(e.get("OBS_VALUE"))
        for e in root.iter() if e.tag.endswith("Obs") and e.get("OBS_VALUE") not in (None, "", "NaN")
    }


def post_index(values, until):
    """Moyenne des 12 mois jusqu'à [until] rapportée à la moyenne de l'année de l'APK, en centièmes (100 = 10 000)."""
    base = [values.get(f"{APK_PRICES_YEAR}-{m:02d}") for m in range(1, 13)]
    year, month = map(int, until.split("-"))
    last = []
    for _ in range(12):
        last.append(values.get(f"{year}-{month:02d}"))
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    if None in base or None in last:
        return None
    return round(sum(last) / sum(base) * 10_000)


def build_prices(series, old):
    """Indices de prix par poste (AND-NET-06), plus récents que ceux de l'APK ; sinon ceux déjà publiés, ou rien."""
    ids = {post: series.get("prix_insee", {}).get(post, "") for post in POSTS}
    if not any(ids.values()):
        return None
    try:
        fetched = {i: insee_monthly(i) for i in set(ids.values()) if i}
        common = set.intersection(*(set(v) for v in fetched.values()))
        until = max(m for m in common if m > f"{APK_PRICES_YEAR}-12")
        posts = {post: post_index(fetched[i], until) for post, i in ids.items() if i}
        posts = {post: v for post, v in posts.items() if v is not None}
        if not posts:
            raise ValueError("aucun poste complet")
        log(f"taux : indices de prix jusqu'à {until}")
        return {"base": APK_PRICES_YEAR, "until": until, "posts": posts}
    except (urllib.error.URLError, ValueError, ET.ParseError, TypeError) as error:
        log(f"taux : indices Insee injoignables ou illisibles ({error.__class__.__name__}), ceux déjà publiés")
        return (old or {}).get("data", {}).get("prices")


def build_rates(old=None):
    livrets = json.loads((SOURCES / "livrets.json").read_text())
    manual = json.loads((SOURCES / "manuel.json").read_text())
    series = json.loads((SOURCES / "series.json").read_text())
    inflation = None
    if series.get("inflation_insee"):
        try:
            inflation = insee_inflation(series["inflation_insee"])
            log(f"taux : inflation Insee {inflation}")
        except (urllib.error.URLError, ValueError, ET.ParseError, TypeError) as error:
            log(f"taux : Insee injoignable ou illisible ({error.__class__.__name__}), valeur de sources/manuel.json")
    if inflation is None:
        inflation = (ppm(manual["inflation"]["pourcent"]), manual["inflation"]["mois"])
    data = {
        "livretAPpm": ppm(livrets["livret_a"]),
        "lddsPpm": ppm(livrets["ldds"]),
        "lepPpm": ppm(livrets["lep"]),
        "since": livrets["depuis"],
        "inflationPpm": inflation[0],
        "inflationMonth": inflation[1],
    }
    prices = build_prices(series, old)
    if prices:
        data["prices"] = prices
    return data


# ---------- credit.json ----------

def webstat_rate(series, key):
    body = json.loads(fetch(WEBSTAT_URL.format(series=series), {"Authorization": f"Apikey {key}"}))
    record = body["results"][0]
    return ppm(record["obs_value"]), str(record["time_period_end"])[:7]


def build_mortgage():
    manual = json.loads((SOURCES / "manuel.json").read_text())
    series = json.loads((SOURCES / "series.json").read_text())
    key = os.environ.get("WEBSTAT_API_KEY")
    if key and series.get("credit_webstat"):
        try:
            rate, month = webstat_rate(series["credit_webstat"], key)
            log(f"credit : Webstat {rate} ppm ({month})")
            return {"ratePpm": rate, "month": month}
        except (urllib.error.URLError, KeyError, IndexError, ValueError) as error:
            log(f"credit : Webstat injoignable ou illisible ({error.__class__.__name__}), valeur de sources/manuel.json")
    return {"ratePpm": ppm(manual["credit"]["pourcent"]), "month": manual["credit"]["mois"]}


# ---------- logement-<dép>.json ----------

def commune_name(code, name):
    """Paris, Lyon et Marseille sans arrondissement : l'application cherche la commune entière."""
    if code.startswith("751"):
        return "Paris"
    if code.startswith("6938"):
        return "Lyon"
    if code.startswith("132") and 13201 <= int(code) <= 13216:
        return "Marseille"
    return name


def read_dvf(dep, year, dvf_dir):
    """Les lignes DVF d'un département pour une année, ou rien si l'année n'est pas publiée."""
    if dvf_dir:
        path = Path(dvf_dir) / str(year) / f"{dep}.csv"
        return path.read_text(encoding="utf-8") if path.exists() else None
    try:
        return gzip.decompress(fetch(DVF_URL.format(year=year, dep=dep), timeout=300)).decode("utf-8")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def sales_of(text):
    """
    Les ventes d'un logement seul : une mutation « Vente » qui ne porte qu'un local Maison ou Appartement (des
    dépendances peuvent l'accompagner). Le prix au m² est la valeur foncière sur la surface réelle bâtie.
    Rend des tuples (mois "AAAA-MM", code commune, nom, type, prix au m²).
    """
    mutations = defaultdict(list)
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("nature_mutation") == "Vente":
            mutations[row["id_mutation"]].append(row)
    for rows in mutations.values():
        homes = [r for r in rows if r.get("type_local") in ("Maison", "Appartement")]
        if len(homes) != 1:
            continue
        home = homes[0]
        try:
            price = float(home["valeur_fonciere"])
            surface = float(home["surface_reelle_bati"])
        except (TypeError, ValueError):
            continue
        if surface < MIN_SURFACE or price <= 0:
            continue
        per_m2 = round(price / surface)
        if per_m2 not in PLAUSIBLE_M2:
            continue
        kind = "house" if home["type_local"] == "Maison" else "apartment"
        code = home["code_commune"]
        yield home["date_mutation"][:7], code, commune_name(code, home["nom_commune"]), kind, per_m2


def homes_data(dep, sales, cap):
    until = max(month for month, *_ in sales)
    end = dt.date.fromisoformat(until + "-01")
    first = f"{end.year - YEARS_BACK:04d}-{end.month:02d}"
    groups = defaultdict(list)
    names = {}
    for month, code, name, kind, per_m2 in sales:
        if month > first:
            key = commune_name(code, name)
            names[key] = key
            groups[(key, kind)].append((month, per_m2))
    communes = []
    for name in sorted(names):
        entry = {"name": name}
        for kind in ("house", "apartment"):
            recent = sorted(groups.get((name, kind), []), reverse=True)[:cap]
            if recent:
                entry[kind] = {"since": min(m for m, _ in recent), "m2": [p for _, p in recent]}
        communes.append(entry)
    return {"department": dep, "until": until, "communes": communes}


def build_homes(dep, dvf_dir, today):
    sales = []
    for year in range(today.year - YEARS_BACK - 1, today.year + 1):
        text = read_dvf(dep, year, dvf_dir)
        if text:
            sales.extend(sales_of(text))
    if not sales:
        return None
    return homes_data(dep, sales, MAX_SALES), sales


# ---------- signature et enveloppe ----------

def sign(payload, key_path):
    signature = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", key_path], input=payload.encode("utf-8"), capture_output=True, check=True
    ).stdout
    return base64.b64encode(signature).decode("ascii")


def verify(payload, signature, public_path):
    with tempfile.NamedTemporaryFile(delete=False) as sig:
        sig.write(base64.b64decode(signature))
    try:
        result = subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", public_path, "-signature", sig.name],
            input=payload.encode("utf-8"), capture_output=True,
        )
        return result.returncode == 0
    finally:
        os.unlink(sig.name)


def previous(out, name):
    path = out / "v1" / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(json.loads(path.read_text())["payload"])


def write(out, name, data, keys, today):
    """Écrit le fichier si ses données ont changé ; rend sa taille, ou 0 s'il est inchangé."""
    old = previous(out, name)
    if old is not None and old.get("data") == data and old.get("format") == FORMAT:
        log(f"{name} : inchangé (version {old['version']})")
        return 0
    version = int(today.strftime("%Y%m%d"))
    if old is not None and version <= old["version"]:
        version = old["version"] + 1
    payload = json.dumps(
        {"name": name, "format": FORMAT, "version": version, "published": today.isoformat(), "data": data},
        ensure_ascii=False, separators=(",", ":"),
    )
    signature = sign(payload, keys[0])
    if not verify(payload, signature, keys[1]):
        raise SystemExit(f"{name} : la signature ne se vérifie pas, rien n'est publié")
    body = json.dumps({"signature": signature, "payload": payload}, ensure_ascii=False, separators=(",", ":"))
    (out / "v1").mkdir(parents=True, exist_ok=True)
    (out / "v1" / f"{name}.json").write_text(body, encoding="utf-8")
    log(f"{name} : version {version}, {len(body.encode('utf-8'))} octets")
    return len(body.encode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, help="dossier publié (branche gh-pages)")
    parser.add_argument("--key", help="clé privée PEM ; sinon la variable FLUX_SIGNING_KEY")
    parser.add_argument("--dvf-dir", help="fichiers DVF locaux <année>/<dép>.csv, pour les essais")
    parser.add_argument("--only", help="liste de fichiers, par exemple taux,credit,logement-33")
    parser.add_argument("--today", help="date de construction AAAA-MM-JJ, pour les essais")
    parser.add_argument("--skip-homes", action="store_true", help="sans les ventes DVF (publiées deux fois par an)")
    args = parser.parse_args()

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    out = Path(args.out)
    only = set(args.only.split(",")) if args.only else None
    wanted = lambda name: only is None or name in only  # noqa: E731

    with tempfile.TemporaryDirectory() as work:
        key_path = args.key
        if not key_path:
            pem = os.environ.get("FLUX_SIGNING_KEY", "")
            if "PRIVATE KEY" not in pem:
                raise SystemExit("clé de signature absente : secret FLUX_SIGNING_KEY ou --key")
            key_path = os.path.join(work, "flux.pem")
            Path(key_path).write_text(pem)
            os.chmod(key_path, 0o600)
        public_path = os.path.join(work, "flux.pub")
        subprocess.run(["openssl", "ec", "-in", key_path, "-pubout", "-out", public_path], check=True,
                       capture_output=True)
        keys = (key_path, public_path)

        if wanted("taux"):
            write(out, "taux", build_rates(previous(out, "taux")), keys, today)
        if wanted("credit"):
            write(out, "credit", build_mortgage(), keys, today)
        failures = []
        for dep in DEPARTMENTS:
            name = f"logement-{dep}"
            if args.skip_homes or not wanted(name):
                continue
            try:
                built = build_homes(dep, args.dvf_dir, today)
            except (urllib.error.URLError, OSError, csv.Error) as error:
                log(f"{name} : DVF injoignable ({error.__class__.__name__}), le fichier publié reste")
                failures.append(name)
                continue
            if built is None:
                log(f"{name} : aucune vente")
                continue
            data, sales = built
            cap = MAX_SALES
            # Le fichier doit tenir sous la borne de l'application : on garde moins de ventes par commune s'il le faut.
            while len(json.dumps(data, separators=(",", ":"))) > MAX_BYTES - 2_000 and cap > MIN_SALES:
                cap -= 25
                data = homes_data(dep, sales, cap)
            if len(json.dumps(data, separators=(",", ":"))) > MAX_BYTES - 2_000:
                raise SystemExit(f"{name} : trop gros même à {cap} ventes par commune")
            write(out, name, data, keys, today)
        if failures and len(failures) > len(DEPARTMENTS) // 2:
            raise SystemExit(f"DVF injoignable pour {len(failures)} départements")


if __name__ == "__main__":
    sys.exit(main())
