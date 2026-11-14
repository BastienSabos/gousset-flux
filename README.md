# gousset-flux

Données publiques signées pour **Gousset Connecté** (ADR 0037 du dépôt budget--planner-). Aucune donnée personnelle :
seulement des taux et des ventes immobilières publiques, publiés chaque lundi sur
https://bastiensabos.github.io/gousset-flux/v1/.

| Fichier | Contenu | Source |
|---|---|---|
| `taux.json` | Livret A, LDDS, LEP et leur date ; inflation sur 12 mois | `sources/livrets.json` (à la main) ; Insee, série BDM 011814632 |
| `credit.json` | Taux moyen des nouveaux crédits à l'habitat | `sources/manuel.json` (à la main), ou Webstat avec le secret `WEBSTAT_API_KEY` |
| `logement-<dép>.json` | Prix au m² des 200 ventes les plus récentes par commune et par type, sur 3 ans | DVF géolocalisées (data.gouv.fr), Licence ouverte |

Chaque fichier est signé (ECDSA P-256) par la clé du secret `FLUX_SIGNING_KEY` ; l'application refuse tout fichier
non signé, plus ancien, ou d'un autre département.

- **Chaque lundi** : `.github/workflows/publier.yml` reconstruit les taux ; les ventes DVF le premier lundi du mois
  (ou à la demande, case « logements »).
- **À la main** : `sources/livrets.json` le 1er février et le 1er août ; `sources/manuel.json` (crédit) chaque mois,
  après la publication « Crédits aux particuliers » de la Banque de France.
- **En local** : `python3 build.py --out public --key flux.pem` (Python 3.10+ et openssl).
