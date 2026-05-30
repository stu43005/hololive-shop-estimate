# Estimator King Operations Runbook

This runbook provides procedures for deploying, managing, and troubleshooting the Estimator King crawler and Discord bot on Kubernetes.

For local development, see [local-runbook.md](local-runbook.md).

## 1. Deployment Procedures

The system is deployed on Kubernetes in the `estimator-king` namespace. A single bot process owns the RWO PVC that holds both the SQLite database and the ChromaDB vector store — do **not** scale `replicas` above 1.

### Prerequisites

- `kubectl` configured with cluster access.
- `OPENAI_API_KEY` and `DISCORD_BOT_TOKEN` available.

### Initial Deployment

1. **Apply Namespace and PVC**:

   ```bash
   kubectl apply -f deploy/crawler-pvc.yaml
   ```

2. **Configure Secrets**:

   ```bash
   kubectl create secret generic estimator-king-secrets \
     --namespace=estimator-king \
     --from-literal=OPENAI_API_KEY="sk-..." \
     --from-literal=DISCORD_BOT_TOKEN="your-token" \
     --dry-run=client -o yaml | kubectl apply -f -
   ```

   Optional provider overrides (add as needed):

   ```bash
   kubectl patch secret estimator-king-secrets \
     -n estimator-king \
     -p '{"stringData":{"OPENAI_BASE_URL":"http://ollama:11434/v1","EMBEDDING_MODEL":"bge-m3","CHAT_STRUCTURED_OUTPUT":"false"}}'
   ```

3. **Configure ConfigMap**:

   ```bash
   kubectl apply -f deploy/configmap.yaml
   ```

4. **Upload Stores Configuration**:

   ```bash
   kubectl create configmap estimator-king-stores-config \
     --from-file=stores_config.yaml=./stores_config.yaml \
     --namespace=estimator-king \
     --dry-run=client -o yaml | kubectl apply -f -
   ```

5. **Deploy Crawler (CronJob) and Bot (Deployment)**:

   ```bash
   kubectl apply -f deploy/crawler-cronjob.yaml
   kubectl apply -f deploy/bot-deployment.yaml
   ```

---

## 2. Secret Rotation

To rotate any secret (e.g., `DISCORD_BOT_TOKEN`):

1. **Update the Secret**:

   ```bash
   kubectl patch secret estimator-king-secrets \
     -n estimator-king \
     -p '{"stringData":{"DISCORD_BOT_TOKEN":"new-token-here"}}'
   ```

2. **Restart the Bot**:

   The bot deployment needs a rollout to pick up the new secret.

   ```bash
   kubectl rollout restart deployment/estimator-king-bot -n estimator-king
   ```

3. **Verify Crawler**:

   The crawler will pick up new secrets on its next scheduled run. No manual restart is needed unless a crawl is currently running.

---

## 3. Ad-hoc Crawl Commands

### Trigger a Manual Crawl

```bash
kubectl create job --from=cronjob/estimator-king-crawler manual-crawl-$(date +%s) -n estimator-king
```

### Force a Full Re-fetch (One-Cycle Backfill)

To re-fetch every product regardless of content hash and daily budget:

```bash
kubectl create job manual-refetch-$(date +%s) -n estimator-king \
  --from=cronjob/estimator-king-crawler \
  --dry-run=client -o json \
  | jq '.spec.template.spec.containers[0].args += ["--force-refetch"]' \
  | kubectl apply -f -
```

### Debug a Specific Store

```bash
kubectl run manual-crawl-debug -it --rm -n estimator-king \
  --image=estimator-king:latest \
  --env-from=configMap/estimator-king-config \
  --env-from=secret/estimator-king-secrets \
  --overrides='{"spec":{"containers":[{"name":"crawler","volumeMounts":[{"name":"data","mountPath":"/data"},{"name":"config","mountPath":"/config"}]}],"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"estimator-king-state-pvc"}},{"name":"config","configMap":{"name":"estimator-king-stores-config"}}]}}' \
  -- python -m estimator_king --config /config/stores_config.yaml --db /data/estimator_king.db
```

---

## 4. Log Inspection

### Viewing Logs

- **Bot Logs**:

  ```bash
  kubectl logs -l app.kubernetes.io/name=estimator-king-bot -n estimator-king -f
  ```

- **Crawler Logs (Latest Job)**:

  ```bash
  kubectl logs -n estimator-king \
    $(kubectl get pods -n estimator-king -l app.kubernetes.io/name=estimator-king-crawler \
      --sort-by=.metadata.creationTimestamp | tail -n 1 | awk '{print $1}')
  ```

### Log Field Definitions

Logging follows a structured format: `%(asctime)s - %(levelname)s - %(message)s`

| Field | Definition |
| ----- | ---------- |
| `asctime` | Timestamp of the log entry (YYYY-MM-DD HH:MM:SS,sss) |
| `levelname` | Severity level: `INFO`, `ERROR`, `WARNING`, `DEBUG` |
| `message` | The log message content |
| `store_id` | (Crawler only) ID of the store being processed |
| `product_id` | (Crawler only) ID of the product being synced |
| `operation` | (Crawler only) Sync operation: create, update, skip |

Common message patterns:

- `Processing store: <store_id>`: Start of a store crawl.
- `Discovered <N> products from <store_id>`: Results from sitemap enumeration.
- `Sync completed for <store_id>: +<C> created, ~<U> updated, =<S> skipped`: Per-store sync summary.
- `Crawler completed: <JSON_SUMMARY>`: Final report for the entire run.

---

## 5. Recovery Procedures

### Bot Crash Loop

If the bot is crashing:

1. Check logs for authentication errors (Discord token invalid or revoked).
2. Check resource limits (OOMKill) — the bot holds ChromaDB in memory.
3. Verify `OPENAI_API_KEY` is valid: check logs for embedding or chat API errors.

### Crawler Failure

If a crawl fails:

1. **Database Lock**: If SQLite is locked, check for hung jobs and delete them.
2. **PVC Full**: Check `estimator-king-state-pvc` usage — both `estimator_king.db` and `chroma/` live here.
3. **Sitemap Changes**: If "Discovered 0 products" appears for a previously working store, verify the `base_url` and Shopify sitemap availability.
4. **Embedding API Error**: Check `OPENAI_API_KEY` quota and rate limits.

---

## 6. Re-index Procedure

Vectors from different embedding models or dimension settings are incompatible. If you change `EMBEDDING_MODEL` or `EMBEDDING_DIMENSIONS` in the secret, you must delete the ChromaDB directory and run a full re-fetch:

1. **Open a shell into the bot pod** (or a debug pod with the PVC mounted):

   ```bash
   kubectl exec -it deployment/estimator-king-bot -n estimator-king -- sh
   ```

2. **Delete the ChromaDB directory**:

   ```bash
   rm -rf /data/chroma
   exit
   ```

3. **Trigger a full re-fetch crawl job**:

   ```bash
   kubectl create job manual-refetch-$(date +%s) -n estimator-king \
     --from=cronjob/estimator-king-crawler \
     --dry-run=client -o json \
     | jq '.spec.template.spec.containers[0].args += ["--force-refetch"]' \
     | kubectl apply -f -
   ```

This re-fetches every product and rebuilds the vector index from scratch.

---

## 7. Smoke Tests & Verification

### Provider Connectivity

```bash
kubectl run smoke-test -it --rm -n estimator-king \
  --image=curlimages/curl \
  --env-from=secret/estimator-king-secrets \
  -- curl -s -o /dev/null -w "%{http_code}" \
     "${OPENAI_BASE_URL:-https://api.openai.com}/v1/models" \
     -H "Authorization: Bearer $OPENAI_API_KEY"
```

**Expected**: `200`

### Bot Smoke Test

```bash
kubectl logs -l app.kubernetes.io/name=estimator-king-bot -n estimator-king | grep "Logged in as"
```

**Expected**: `... - INFO - Logged in as EstimatorKing#1234`

### Summary Report Verification

```bash
kubectl logs -n estimator-king \
  $(kubectl get pods -n estimator-king -l app.kubernetes.io/name=estimator-king-crawler \
    --sort-by=.metadata.creationTimestamp | tail -n 1 | awk '{print $1}') \
  | grep "Crawler completed"
```

---

## 8. Summary Report JSON Specification

At the end of every crawl run, the crawler outputs a JSON object to stdout and logs it.

### JSON Schema

```json
{
  "discovered": "integer - Total URLs found in sitemaps",
  "fetched_ok": "integer - Successfully fetched product details",
  "created": "integer - New products added to ChromaDB",
  "updated": "integer - Existing products refreshed in ChromaDB",
  "skipped": "integer - Products unchanged (skipped sync)",
  "inactive": "integer - Products marked as inactive (removed from index)",
  "errors": "integer - Total count of failed operations"
}
```

---

## 9. Observability Contract

### Log Fields

- `timestamp`: ISO8601 or standard Python logging timestamp.
- `level`: Log level (INFO/ERROR).
- `message`: Contextual message.
- `store_id`: (Crawler only) ID of the store being processed.
- `product_id`: (Crawler only) ID of the product being synced.

### Metrics (Future)

- `crawler_run_duration_seconds`: Time taken for full crawl.
- `bot_command_latency_seconds`: Time taken for `/estimate` command.
- `embedding_api_error_count`: Number of non-200 responses from the embedding provider.

### Recommended Alerts

- **Crawler Failures**: Alert if `errors > 0` in the summary report.
- **Bot Downtime**: Alert if `estimator-king-bot` deployment replicas < 1 for > 5 minutes.
- **Persistent Failures**: Alert if crawler CronJob fails for 2 consecutive days.
