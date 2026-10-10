# Profil de transcription du homelab

Le profil privilégie la précision : faster-whisper `medium`, CPU/int8, beam 5,
deux threads et un worker. Il remplace `small`/beam 3 sans changement du plugin,
du contrat API, de la diarisation ou de l'accès au GPU. Aucun service cloud n'est
utilisé. Le modèle est téléchargé une seule fois, puis le worker reste hors ligne.

Le VAD et `condition_on_previous_text=False` sont conservés. Aucun filtre de
débruitage ni glossaire arbitraire n'est appliqué aux enregistrements. Les langues
`auto`, `fr` et `en`, les timestamps par mot et les formats audio restent pris en
charge. Pour une réunion principalement française, choisir `fr` dans le plugin.

## Validation avant bascule

1. Vérifier qu'aucun job audio n'est actif, les ressources disponibles et les
   sauvegardes. Noter le tag/digest de l'image worker, le modèle et le beam.
2. Sauvegarder `.env.homelab` et les Compose en mode protégé ; conserver le cache
   `small` et l'image actuelle. Ne pas afficher les secrets.
3. Précharger `medium` dans le volume existant avec le service `prepare-model`,
   sans redémarrer le worker. Avec l'override d'images locales :

   ```bash
   WHISPER_MODEL_SIZE=medium docker compose --env-file .env.homelab \
     -f infra/docker-compose.homelab.yml \
     -f infra/docker-compose.homelab.local-images.yml \
     --profile prepare-model run --rm whisper-model-preparer
   ```

4. Comparer les deux profils dans des conteneurs de diagnostic isolés : pas de
   réseau, pas d'accès à PostgreSQL/Redis, cache modèles en lecture seule, limites
   identiques au worker (2 CPU, 4 Gio, tmpfs 512 Mio). Utiliser
   `scripts/homelab/benchmark-transcription.py` avec l'environnement du worker.
   Le script n'affiche pas de transcription et ne consomme pas de jobs.

   ```bash
   python /bench.py /sample.wav --language fr --reference /reference.txt
   ```

   `--reference` est facultatif. Le WER ignore casse et ponctuation, mais conserve
   accents et représentation des nombres. Le pic RSS concerne le processus
   Python ; surveiller aussi la mémoire totale du conteneur avec `docker stats`.
   Mesurer sur un extrait court et un fichier long avant activation. La synthèse
   vocale ne remplace pas une référence humaine sur une véritable réunion.

## Déploiement et retour arrière

Après validation, définir `WHISPER_MODEL_SIZE=medium` et `WHISPER_BEAM_SIZE=5`
dans l'environnement effectif de la stack. Réutiliser l'image worker testée :
une reconstruction n'est pas nécessaire pour cette modification de configuration
et pourrait mettre à jour des dépendances Python non épinglées.

Quand la file et les jobs actifs sont vides, recréer uniquement le worker :

```bash
docker compose --env-file .env.homelab \
  -f infra/docker-compose.homelab.yml \
  -f infra/docker-compose.homelab.local-images.yml \
  up -d --no-deps whisper-worker
```

Vérifier le modèle effectif (sans afficher l'environnement complet), le heartbeat,
la santé authentifiée, puis un upload/transcription réel via le gateway. Vérifier
que la diarisation optionnelle et les timestamps restent utilisables. Les anciens
résultats ne sont pas retranscrits automatiquement.

Retour arrière : remettre `WHISPER_MODEL_SIZE=small`, `WHISPER_BEAM_SIZE=3`,
conserver le tag worker précédent et recréer uniquement ce service. Ne supprimer
aucun volume. Sous Portainer, répercuter les mêmes variables si la stack est
gérée par son éditeur ; le déploiement homelab historique est une stack Compose
externe visible dans Portainer.

## Limites

Le temps de calcul augmente ; il dépend de la parole détectée, de la langue et de
la charge de l'hôte. La RAM disponible et le tmpfs limitent toujours les réunions
très longues. La qualité des noms propres, du jargon et des voix superposées doit
être évaluée sur des audios représentatifs. Aucune amélioration universelle ou
identification automatique des personnes n'est garantie.
