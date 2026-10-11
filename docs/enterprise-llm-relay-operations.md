# Exploiter le relais LLM entreprise

Le relais permet à Obsidian Desktop, sur le PC professionnel, d'appeler
`https://ai.headmind.com` avec un token conservé uniquement en mémoire. Le gateway
personnel prépare les messages et conserve l'état du traitement ; il ne reçoit
jamais le token HeadMind et n'accède pas au réseau de l'entreprise.

Cette livraison ajoute les générations persistantes **entreprise**. Le mode
personnel conserve ses routes synchrones et son accès Ollama actuels. Whisper,
la diarisation et les embeddings restent personnels. Le choix du fournisseur et
du modèle est explicite ; aucune panne entreprise ne provoque de bascule vers
Ollama. La conception cible est décrite dans
[le plan d'intégration](enterprise-llm-relay-plan.md).

## État avant le pilote

La fonctionnalité est désactivée par défaut. La liste des modèles autorisés est
vide, car l'identifiant exact du modèle HeadMind doit être découvert sur le PC
professionnel. Les tests simulés ne valident ni l'accès VPN, ni les droits du token,
ni la compatibilité de l'instance réelle. Ne pas interpréter un déploiement sain
du gateway comme une recette réussie de HeadMind.

Le PC professionnel doit pouvoir joindre simultanément le gateway personnel et
HeadMind. Aucun port entrant n'est ouvert sur le PC. La configuration TLS et les
contrôles d'accès de l'entreprise restent applicables.

Le connecteur utilise les routes documentées [Open WebUI](https://docs.openwebui.com/reference/api-endpoints/) :
`GET /api/models` et `POST /api/chat/completions`, avec authentification Bearer.
Il accepte aussi les réponses SSE si l'instance impose le streaming.

L'exemple curl fourni depuis le PC professionnel le 11 octobre 2026 confirme
l'identifiant `gpt-oss-120b` et le contrat `model` / `messages` / `stream: false`.
La réponse contenant `content: "OK"`, `finish_reason: "stop"` et des champs
optionnels nuls est couverte par un test du connecteur. Le champ `reasoning`
n'est pas utilisé. Cet exemple valide l'appel de génération ; la découverte
`GET /api/models` et le relais complet dans Obsidian restent à tester. Garder
les listes de modèles explicites : les modèles futurs seront découverts côté PC
mais devront aussi être autorisés côté gateway.

## Configuration serveur

Les exemples complets sont dans [`.env.homelab.example`](../.env.homelab.example)
et [`.env.example`](../.env.example). Ne jamais y ajouter de token HeadMind, même
temporairement, ni en créer une variable dans Portainer.

- `ENTERPRISE_RELAY_ENABLED=false` : interrupteur serveur, à laisser désactivé
  jusqu'à la préparation du pilote.
- `ENTERPRISE_RELAY_ALLOWED_MODELS=` : liste séparée par des virgules des
  identifiants exacts autorisés, distincte de `ALLOWED_MODELS` pour Ollama.
- `ENTERPRISE_RELAY_ALLOWED_USER_IDS=` : identifiants des utilisateurs du gateway
  autorisés pour le pilote. Utiliser les utilisateurs associés aux tokens du
  gateway, pas les identifiants de compte HeadMind ni les tokens eux-mêmes. Une
  liste vide ne donne accès à personne, y compris aux utilisateurs administrateurs.
- `ENTERPRISE_RELAY_SESSION_TTL_SECONDS=28800` : durée maximale d'une session,
  soit huit heures. Une session du relais n'allonge pas la validité du token
  HeadMind.
- `ENTERPRISE_RELAY_LEASE_SECONDS=90` : bail d'une tentative, renouvelé par le PC
  pendant l'appel. Son expiration signale une issue potentiellement incertaine ;
  elle ne démontre pas que le calcul distant a été interrompu.
- `ENTERPRISE_GENERATION_TTL_SECONDS=86400` : durée maximale d'un traitement,
  soit 24 heures par défaut.
- `ENTERPRISE_GENERATION_RETENTION_DAYS=7` : conservation des résultats terminés
  pour permettre leur récupération dans Obsidian.
- `ENTERPRISE_MAX_ACTIVE_GENERATIONS_PER_USER=3` et
  `ENTERPRISE_DAILY_GENERATIONS_PER_USER=100` : plafonds propres au relais.
- `ENTERPRISE_MAX_PROMPT_CHARS=100000` et
  `ENTERPRISE_MAX_RESULT_CHARS=200000` : limites en **caractères**, pas en tokens.
  Adapter le plafond des messages à la limite réelle du modèle pendant le pilote.

Les limites métier existantes sur les notes, transcriptions et templates restent
applicables. Les recherches RAG consomment aussi le quota d'embeddings local.
Chaque étape accepte au maximum cinq tentatives, toutes les reprises après un
appel incertain nécessitant une décision explicite. Aucun paramètre propre à Ollama, tel que `num_ctx` ou `keep_alive`,
n'est envoyé au fournisseur entreprise.

## Service interne de maintenance

Le service `relay-maintenance` utilise exactement l'image du gateway. Il traite
l'expiration et la conservation des données en base, sans appeler un LLM. Les
transitions ordinaires sont traitées par les routes du gateway ; ce service
n'est pas un second worker audio et ne consomme pas sa file Redis.

- Profil Compose optionnel : `enterprise-relay`.
- Commande : `python -m app.generation_maintenance --loop`.
- Contrôle de disponibilité : `python -m app.generation_maintenance --check`.
- Maintenance ponctuelle : `python -m app.generation_maintenance --once`.
- Budget initial : 0,25 CPU, 256 Mio, système de fichiers en lecture seule.
- Réseau Docker interne uniquement ; aucun port publié, volume audio ou GPU.

La commande du service remplace le démarrage HTTP de l'image et ne lance pas de
migration. Le gateway exécute Alembic ; la maintenance démarre après son contrôle
de santé. La migration `20261010_0003` ajoute les tables du relais sans modifier
le contrat des jobs audio. Lancer un seul gateway pendant cette migration.

Le contrôle de santé de la maintenance vérifie son accès à la base et aux tables.
Il ne vérifie pas HeadMind, le VPN ou le token de l'utilisateur. Examiner également
les journaux et redémarrages du service si une purge semble ne plus avancer.

## Déploiement homelab

Les commandes suivantes s'exécutent dans le répertoire de la stack sur le serveur.
Elles supposent le mécanisme existant d'images locales immuables ; utiliser le tag
validé dans `GATEWAY_IMAGE`, et conserver le tag précédent pour le retour arrière.
Ne pas copier des variables de test par-dessus le fichier réel.

1. Vérifier l'absence de génération active à interrompre. Sauvegarder PostgreSQL,
   les fichiers Compose et `.env.homelab` dans un emplacement privé, ainsi que les
   références des images. Utiliser la procédure de sauvegarde homelab existante.
2. Charger ou construire l'image gateway validée, puis mettre à jour uniquement
   sa référence et les nouveaux fichiers Compose. Conserver
   `ENTERPRISE_RELAY_ENABLED=false`.
3. Valider la configuration sans afficher les valeurs interpolées :

   ```sh
   docker compose --env-file .env.homelab \
     -f infra/docker-compose.homelab.yml \
     -f infra/docker-compose.homelab.local-images.yml config --quiet
   ```

4. Recréer seulement le gateway ; son démarrage applique la migration additive :

   ```sh
   docker compose --env-file .env.homelab \
     -f infra/docker-compose.homelab.yml \
     -f infra/docker-compose.homelab.local-images.yml \
     up -d --no-deps ai-gateway
   ```

5. Contrôler sa santé, l'authentification et un parcours personnel avec l'ancien
   plugin. Vérifier que Whisper, PostgreSQL et Redis n'ont pas été recréés.
6. Après découverte du modèle sur le PC professionnel, renseigner les listes de
   modèles et d'utilisateurs, puis activer le relais. Recréer le gateway avec la
   même commande et attendre son état sain.
7. Démarrer la maintenance avec la même image :

   ```sh
   docker compose --env-file .env.homelab \
     -f infra/docker-compose.homelab.yml \
     -f infra/docker-compose.homelab.local-images.yml \
     --profile enterprise-relay up -d --no-deps relay-maintenance
   ```

Le profil n'active pas la fonctionnalité à lui seul : les variables et listes
restent nécessaires. Pour les mises à jour suivantes, mettre à jour le gateway
et la maintenance ensemble. Toujours conserver l'override `local-images` tant
que l'accès au registre privé n'est pas configuré ; il interdit les pulls.

La stack reste administrée par ses fichiers Compose. Portainer permet de
contrôler les conteneurs et leurs états. Ne pas créer une seconde stack parallèle
qui utiliserait les mêmes volumes ou ports.

## Autres installations Docker Compose

La stack générique utilise l'overlay
[`infra/docker-compose.enterprise-relay.yml`](../infra/docker-compose.enterprise-relay.yml)
en complément de `docker-compose.yml` et de ses overrides habituels. Ne pas ajouter
cet overlay à la stack homelab, qui contient déjà le service et les variables.

Valider sans daemon, avec les exemples non secrets :

```sh
docker compose --env-file .env.example \
  -f docker-compose.yml -f infra/docker-compose.enterprise-relay.yml \
  --profile enterprise-relay config --quiet
```

Pour un déploiement, utiliser le fichier d'environnement réel et les overrides
de production existants. Le build du gateway produit l'image également utilisée
par `relay-maintenance`. Celui-ci ne lance pas Alembic et attend un gateway sain.

## Installation et recette sur le PC professionnel

1. Installer la version du plugin contenant le relais, en conservant une copie
   des fichiers de la version précédente. Vérifier le mode personnel.
2. Connecter le VPN si nécessaire, puis choisir **Entreprise — HeadMind** dans
   le sélecteur d'exécution du LLM.
3. Utiliser **Connecter HeadMind**, saisir le token dans la boîte de dialogue et
   relever l'identifiant du modèle accessible. Le token ne doit être envoyé ni
   dans le chat, ni dans une capture d'écran, ni à l'administrateur du serveur.
4. Faire autoriser cet identifiant et l'utilisateur gateway côté serveur. Choisir
   ensuite explicitement le modèle et utiliser **Tester ce modèle**. Ce test
   envoie un texte synthétique ; il ne lit pas le vault.
5. Lancer une question synthétique dans l'assistant. Vérifier le mode affiché,
   le passage du traitement à « Terminé » et la récupération du résultat.
6. Vérifier les résumés, une réunion courte, une réunion en plusieurs étapes et
   une question RAG. Les extraits RAG et la transcription sont des textes transmis
   à HeadMind ; le connecteur ne transmet pas l'audio brut.
7. Couper le réseau, renouveler le token et fermer Obsidian lors de traitements
   synthétiques. Vérifier les états d'attente, la reprise explicite et l'absence
   de bascule vers Ollama.

La liste visible sur le PC et la liste autorisée par le gateway doivent avoir
le modèle choisi en commun. Le catalogue est consulté avec le token local ; le
serveur ne peut pas vérifier indépendamment les droits HeadMind.

La connexion entreprise et le token sont locaux à la session Obsidian. Après
fermeture, saisir de nouveau le token puis reprendre explicitement le traitement
dans le centre de tâches. Un autre appareil ne prend pas automatiquement le
relais. Changer le sélecteur ne modifie pas les traitements déjà créés.

## Interpréter les interruptions

- **Token à renouveler** : remplacer le token, tester la connexion puis reprendre.
- **Connexion indisponible** : vérifier les deux accès depuis le PC. Une panne
  réseau n'indique pas nécessairement que le VPN est coupé.
- **Modèle ou accès refusé** : vérifier les droits HeadMind, la liste serveur et
  l'identité de l'utilisateur gateway.
- **Limite atteinte** : distinguer les quotas personnels du gateway d'un refus
  temporaire du fournisseur. Éviter les relances rapides.
- **Résultat incertain** : l'appel a pu terminer chez HeadMind sans accusé reçu.
  Une reprise explicite peut refaire ce calcul ; aucune relance automatique ne
  garantit l'absence d'un second calcul chez le fournisseur.
- **Annulé ou expiré** : le serveur n'accepte plus le résultat tardif. L'arrêt
  local ne garantit pas l'arrêt du calcul déjà lancé dans l'entreprise.

La fermeture d'une fenêtre de suivi n'efface pas le job persistant. Récupérer un
résultat terminé depuis le centre de tâches avant de créer une nouvelle demande.
Le secret de session du relais est distinct du token HeadMind ; seul son hash
est enregistré côté serveur.

## Reverse proxy et contrôle d'exploitation

Toutes les nouvelles routes restent sous `/v1` sur le gateway HTTPS existant et
exigent l'authentification. Le long polling est borné à 25 secondes ; conserver
un délai proxy supérieur. Aucun accès direct à PostgreSQL, Redis, Ollama ou à la
maintenance n'est ajouté.

Le proxy homelab limite actuellement les connexions et le débit par IP. Tester
le relais en parallèle du suivi des jobs et d'un upload avant de changer ces
limites. Une IP partagée par plusieurs clients peut atteindre le plafond même
si chaque client reste raisonnable. Ne pas désactiver globalement ces protections.

Surveiller les erreurs normalisées, les attentes longues, les tentatives
incertaines, la disponibilité de la base et l'état de la maintenance. Ne jamais
ajouter aux logs les en-têtes Authorization, tokens, prompts, transcriptions ou
réponses complètes pour diagnostiquer le réseau.

Les snapshots et réponses nécessaires à la reprise sont des données applicatives
sur le serveur personnel. Les entrées, messages préparés et résultats
intermédiaires sont supprimés lorsque le traitement devient terminal, notamment
à son expiration après 24 heures par défaut. Le résultat final, les métadonnées
et les empreintes utiles à la livraison idempotente sont conservés sept jours
par défaut, puis purgés. Les sauvegardes ont leur propre rotation ; une purge en
base ne supprime pas immédiatement leurs anciennes copies. Les notes déjà écrites
dans Obsidian restent dans le vault indépendamment de cette conservation.

## Désactivation et retour arrière

1. Demander aux clients d'arrêter les nouvelles générations et annuler ou laisser
   terminer les traitements actifs tant que les routes du relais sont disponibles.
   Déconnecter le relais sur le PC.
2. Mettre `ENTERPRISE_RELAY_ENABLED=false`, puis recréer seulement le gateway.
   Le mode personnel reste disponible ; le mode entreprise affiche un refus
   explicite et ne choisit pas automatiquement Ollama.
3. Arrêter le service optionnel avant de restaurer l'ancienne image :

   ```sh
   docker compose --env-file .env.homelab \
     -f infra/docker-compose.homelab.yml \
     -f infra/docker-compose.homelab.local-images.yml \
     --profile enterprise-relay stop relay-maintenance
   ```

4. Restaurer la référence gateway précédente. Son script de démarrage ne connaît
   pas la nouvelle révision Alembic : un démarrage normal échouerait. Après
   vérification que les tables ajoutées sont compatibles avec cette ancienne
   version, utiliser l'override de retour arrière, qui lance uniquement l'API :

   ```sh
   docker compose --env-file .env.homelab \
     -f infra/docker-compose.homelab.yml \
     -f infra/docker-compose.homelab.local-images.yml \
     -f infra/docker-compose.enterprise-relay.rollback.yml \
     up -d --no-deps ai-gateway
   ```

   Conserver les tables additives et la sauvegarde. Ne pas supprimer les volumes,
   effacer la révision Alembic ou lancer de migration destructive. Cet override
   est réservé à ce retour arrière ; le retirer dès le retour à une image récente
   contenant les migrations requises.
5. Vérifier le parcours personnel et restaurer le plugin précédent si nécessaire.

Les traitements encore présents ne sont pas exécutés par un ancien backend.
Après rétablissement du nouveau backend, reprendre explicitement ce qui n'a pas
expiré. Vérifier la conservation des données après une interruption prolongée
de la maintenance. Une désactivation arrête les nouveaux appels via ce relais,
pas un calcul déjà reçu par HeadMind.
