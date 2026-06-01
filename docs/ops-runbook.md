# Estimator King Operations Runbook

This runbook provides procedures for deploying, managing, and troubleshooting the Estimator King Discord bot — which runs the crawl scheduler in-process — on Kubernetes.

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

5. **Deploy the Bot (Deployment)**:

   The bot process runs the crawl scheduler in-process; there is no separate crawler workload.

   ```bash
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

---

## 3. Crawling

Crawling runs **in-process** inside the bot: the `CrawlScheduler` triggers a cycle on startup (`run_on_start`) and then every `crawl_schedule_hours`. There is no separate crawler workload and no ad-hoc crawl job in production — the ChromaDB vector store is single-writer, so a second crawl process must never run against the live PVC.

- To force a fresh full rebuild (for example after an embedding-model change), see [§6 Re-index Procedure](#6-re-index-procedure).
- For a local one-off crawl during development, see [local-runbook.md](local-runbook.md) (`python -m estimator_king crawl`).

---

## 4. Log Inspection

### Viewing Logs

- **Bot Logs**:

  ```bash
  kubectl logs -l app.kubernetes.io/name=estimator-king-bot -n estimator-king -f
  ```

  The in-process crawl cycle logs to the same bot logs (there is no separate crawler pod).

### Log Field Definitions

Logging follows a structured format: `%(asctime)s [%(levelname)s] %(message)s`

| Field | Definition |
| ----- | ---------- |
| `asctime` | Timestamp of the log entry (YYYY-MM-DD HH:MM:SS,sss) |
| `levelname` | Severity level: `INFO`, `ERROR`, `WARNING`, `DEBUG` |
| `message` | The log message content |
| `store_id` | (crawl phase only) ID of the store being processed |
| `product_id` | (crawl phase only) ID of the product being synced |
| `operation` | (crawl phase only) Sync operation: create, update, skip |

Common message patterns:

- `Processing store: <store_id>`: Start of a store crawl.
- `Discovered <N> products from <store_id>`: Results from sitemap enumeration.
- `Sync completed for <store_id>: +<C> created, ~<U> updated, =<S> skipped`: Per-store sync summary.
- `Crawl cycle complete: <JSON_SUMMARY>`: Final report for the entire crawl cycle (logged by the in-process scheduler under `run`).

---

## 5. Recovery Procedures

### Bot Crash Loop

If the bot is crashing:

1. Check logs for authentication errors (Discord token invalid or revoked).
2. Check resource limits (OOMKill) — the bot holds ChromaDB in memory.
3. Verify `OPENAI_API_KEY` is valid: check logs for embedding or chat API errors.

### Crawl Cycle Failure

If a crawl fails:

1. **Database Lock**: If SQLite is locked, restart the bot Deployment to release the in-process lock.
2. **PVC Full**: Check `estimator-king-state-pvc` usage — both `estimator_king.db` and `chroma/` live here.
3. **Sitemap Changes**: If "Discovered 0 products" appears for a previously working store, verify the `base_url` and Shopify sitemap availability.
4. **Embedding API Error**: Check `OPENAI_API_KEY` quota and rate limits.

---

## 6. Re-index Procedure

Vectors from different embedding models or dimension settings are incompatible. If you change `EMBEDDING_MODEL` or `EMBEDDING_DIMENSIONS`, you must clear the vector store and let the bot rebuild it from scratch. Because ChromaDB is single-writer, the bot must be stopped before clearing the data.

1. **Scale the bot down** (releases the PVC / ChromaDB):

   ```bash
   kubectl scale deployment/estimator-king-bot --replicas=0 -n estimator-king
   ```

2. **Clear the vector store and crawl state** with a short-lived pod that mounts the PVC:

   ```bash
   kubectl run reindex-clean -it --rm -n estimator-king \
     --image=busybox --restart=Never \
     --overrides='{"spec":{"containers":[{"name":"reindex-clean","image":"busybox","command":["sh","-c","rm -rf /data/chroma /data/estimator_king.db"],"volumeMounts":[{"name":"data","mountPath":"/data"}]}],"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"estimator-king-state-pvc"}}]}}'
   ```

3. **Scale the bot back up**:

   ```bash
   kubectl scale deployment/estimator-king-bot --replicas=1 -n estimator-king
   ```

On startup the bot's scheduler runs a crawl immediately (`run_on_start`). Because the SQLite crawl state was deleted, every product is rediscovered and re-embedded in a single cycle, rebuilding the vector index from scratch.

> Deleting `estimator_king.db` resets crawl state (content hashes, active/inactive tracking). This is intended for a full re-index — the next crawl rebuilds it.

### Re-index after the item-level indexing upgrade

The vector ID scheme and document format changed (per-item vectors). After deploying, follow steps 1–3 above (scale down → clear `/data/chroma` → scale up). The SQLite DB does **not** need to be deleted for this migration — the schema migrates additively on startup. If you also changed `EMBEDDING_MODEL`/`EMBEDDING_DIMENSIONS` or bumped `item_types_version` in `stores_config.yaml`, clear both `chroma/` and `estimator_king.db` as shown above.

### Wrong-currency prices

The crawler pins Shopify prices to JPY (`?currency=JPY`) and validates each variant's `price_currency`. Prices crawled before this fix self-heal: the corrected price changes a product's content hash, so the next scheduled in-process crawl re-indexes it. No manual re-index is needed.

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

**Expected**: `... [INFO] Logged in as EstimatorKing#1234`

### Summary Report Verification

```bash
kubectl logs -l app.kubernetes.io/name=estimator-king-bot -n estimator-king | grep "Crawl cycle complete"
```

---

## 8. Summary Report JSON Specification

The `crawl` CLI prints this JSON object to stdout on completion. Under `run`, the in-process scheduler logs the same counters via `logger.info` (`Crawl cycle complete: ...`) rather than printing to stdout.

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
- `store_id`: (crawl phase only) ID of the store being processed.
- `product_id`: (crawl phase only) ID of the product being synced.

### Metrics (Future)

- `crawler_run_duration_seconds`: Time taken for full crawl.
- `bot_command_latency_seconds`: Time taken for `/estimate` command.
- `embedding_api_error_count`: Number of non-200 responses from the embedding provider.

### Recommended Alerts

- **Crawl Failures**: Alert if `errors > 0` in the crawl summary counters.
- **Bot Downtime**: Alert if `estimator-king-bot` deployment replicas < 1 for > 5 minutes.
- **Persistent Failures**: Alert if the bot's daily crawl logs `errors > 0` for 2 consecutive days.
