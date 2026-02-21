# Estimator King Operations Runbook

This runbook provides procedures for deploying, managing, and troubleshooting the Estimator King crawler and Discord bot.

## 1. Deployment Procedures

The system is deployed on Kubernetes in the `dify` namespace.

### Prerequisites
- `kubectl` configured with cluster access.
- `DIFY_API_KEY`, `DIFY_DATASET_ID`, `DISCORD_TOKEN`, and `DIFY_WORKFLOW_API_KEY` available.

### Initial Deployment
1. **Apply Namespace and PVC**:
   ```bash
   kubectl apply -f deploy/crawler-pvc.yaml
   ```

2. **Configure Secrets**:
   Edit `deploy/secrets.yaml` with real base64-encoded values or apply via command line:
   ```bash
   kubectl create secret opaque estimator-king-secrets \
     --namespace=dify \
     --from-literal=DIFY_API_KEY="your-key" \
     --from-literal=DIFY_DATASET_ID="your-id" \
     --from-literal=DISCORD_TOKEN="your-token" \
     --from-literal=DIFY_WORKFLOW_API_KEY="your-workflow-key" \
     --dry-run=client -o yaml | kubectl apply -f -
   ```

3. **Configure ConfigMap**:
   ```bash
   kubectl apply -f deploy/configmap.yaml
   ```

4. **Upload Stores Configuration**:
   ```bash
   kubectl create configmap estimator-king-stores-config \
     --from-file=stores_config.yaml=./stores_config.yaml \
     --namespace=dify \
     --dry-run=client -o yaml | kubectl apply -f -
   ```

5. **Deploy Crawler (CronJob) and Bot (Deployment)**:
   ```bash
   kubectl apply -f deploy/crawler-cronjob.yaml
   # Note: Bot deployment requires a built image 'estimator-king-bot:latest' in the registry
   kubectl apply -f deploy/bot-deployment.yaml
   ```

---

## 2. Secret Rotation

To rotate any secret (e.g., `DISCORD_TOKEN`):

1. **Update the Secret**:
   ```bash
   kubectl patch secret estimator-king-secrets \
     -n dify \
     -p "{\"stringData\":{\"DISCORD_TOKEN\":\"new-token-here\"}}"
   ```

2. **Restart the Bot**:
   The bot deployment needs a rollout to pick up the new secret.
   ```bash
   kubectl rollout restart deployment/estimator-king-bot -n dify
   ```

3. **Verify Crawler**:
   The crawler will pick up new secrets on its next scheduled run. No manual restart is needed unless a crawl is currently running.

---

## 3. Ad-hoc Crawl Commands

To trigger a manual crawl outside the weekly schedule:

```bash
# Create a manual job from the CronJob template
kubectl create job --from=cronjob/estimator-king-crawler manual-crawl-$(date +%s) -n dify
```

To run a crawl for a specific store (advanced):
```bash
kubectl run manual-crawl-debug -it --rm -n dify \
  --image=estimator-king-crawler:latest \
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
  kubectl logs -l app.kubernetes.io/name=estimator-king-bot -n dify -f
  ```
- **Crawler Logs (Latest Job)**:
  ```bash
  kubectl logs -n dify $(kubectl get pods -n dify -l app.kubernetes.io/name=estimator-king-crawler --sort-by=.metadata.creationTimestamp | tail -n 1 | awk '{print $1}')
  ```

### Log Field Definitions
Logging follows a structured format: `%(asctime)s - %(levelname)s - %(message)s`

| Field | Definition |
|-------|------------|
| `asctime` | Timestamp of the log entry (YYYY-MM-DD HH:MM:SS,sss) |
| `levelname` | Severity level: `INFO`, `ERROR`, `WARNING`, `DEBUG` |
| `message` | The log message content |
| `store_id` | (Crawler only) ID of the store being processed (if applicable) |
| `product_id` | (Crawler only) ID of the product being synced (if applicable) |
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
1. Check logs for authentication errors (Discord token).
2. Verify Dify API reachability.
3. Check resource limits (OOMKill).

### Crawler Failure
If a crawl fails:
1. **Database Lock**: If SQLite is locked, check for hung jobs and delete them.
2. **PVC Full**: Check `estimator-king-state-pvc` usage.
3. **Sitemap Changes**: If "Discovered 0 products" appears for a previously working store, verify the `base_url` and Shopify sitemap availability.

---

## 6. Smoke Tests & Verification

### Crawler Smoke Test
Check if the crawler pod can reach Dify:
```bash
# Run a temporary pod to test connectivity
kubectl run smoke-test -it --rm -n dify \
  --image=curlimages/curl \
  --env-from=secret/estimator-king-secrets \
  -- curl -X GET "https://dify.long-cod.ts.net/v1/datasets" \
     -H "Authorization: Bearer $DIFY_API_KEY"
```
**Expected Output**: JSON response containing a list of datasets.

### Bot Smoke Test
Verify Discord bot status in the Discord developer portal or by checking logs:
```bash
kubectl logs -l app.kubernetes.io/name=estimator-king-bot -n dify | grep "Logged in as"
```
**Expected Output**: `2025-02-22 ... - INFO - Logged in as EstimatorKing#1234`

### Summary Report Verification
Check for the final summary report in crawler logs:
```bash
kubectl logs -n dify $(kubectl get pods -n dify -l app.kubernetes.io/name=estimator-king-crawler --sort-by=.metadata.creationTimestamp | tail -n 1 | awk '{print $1}') | grep "Crawler completed"
```

---

## 7. Summary Report JSON Specification

At the end of every crawl run, the crawler outputs a JSON object to stdout and logs it.

### JSON Schema
```json
{
  "discovered": "integer - Total URLs found in sitemaps",
  "fetched_ok": "integer - Successfully fetched product details",
  "created": "integer - New products added to Dify Knowledge Base",
  "updated": "integer - Existing products refreshed in Dify KB",
  "skipped": "integer - Products unchanged (skipped sync)",
  "inactive": "integer - Products marked as inactive (removed from KB)",
  "errors": "integer - Total count of failed operations"
}
```

---

## 8. Observability Contract

### Log Fields
- `timestamp`: ISO8601 or standard Python logging timestamp.
- `level`: Log level (INFO/ERROR).
- `message`: Contextual message.
- `store_id`: (Crawler only) ID of the store being processed.
- `product_id`: (Crawler only) ID of the product being synced.

### Metrics (Future)
- `crawler_run_duration_seconds`: Time taken for full crawl.
- `bot_command_latency_seconds`: Time taken for `/estimate` command.
- `dify_api_error_count`: Number of non-200 responses from Dify.

### Recommended Alerts
- **Crawler Failures**: Alert if `errors > 0` in the summary report.
- **Bot Downtime**: Alert if `estimator-king-bot` deployment replicas < 1 for > 5 minutes.
- **Persistent Failures**: Alert if crawler CronJob fails for 2 consecutive weeks.
