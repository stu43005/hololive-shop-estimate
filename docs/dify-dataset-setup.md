# Dify Dataset UI Setup Guide

This guide provides step-by-step instructions for creating a Dify Knowledge Base dataset and extracting the API credentials required for the Estimator King crawler.

- **Date**: 2026-02-21
- **Target Audience**: Ops engineers deploying Estimator King to Kubernetes
- **Prerequisites**: Access to the Dify instance at https://dify.long-cod.ts.net

## 1. Introduction

The Estimator King crawler requires a Dify Knowledge Base dataset to store and index product information. This dataset acts as the "brain" for the Estimator King Discord bot, providing the necessary context for accurate estimations.

## 2. Step-by-Step UI Setup

Follow these steps to create a new dataset in the Dify web UI.

### Step 2.1: Login
1. Navigate to [https://dify.long-cod.ts.net](https://dify.long-cod.ts.net).
2. Login with your credentials.

### Step 2.2: Create Knowledge Base
1. Click the **Knowledge** section in the top navigation bar.
2. Click the **Create Knowledge** button (usually at the top right).
3. Select **Create from empty** if prompted.

### Step 2.3: Configure Dataset Settings
1. **Name**: Enter a descriptive name, e.g., `Estimator King Products`.
2. **Type**: Select **Text** (do not use file upload for the crawler).
3. **Indexing Mode**: Choose **High Quality** for better retrieval accuracy.
4. **Embedding Model**: Select the recommended model (e.g., `text-embedding-ada-002`).
5. Click **Save & Next** or **Create**.

### Step 2.4: Extract Dataset ID
1. Once the dataset is created, look at the URL in your browser's address bar.
2. The URL format is `https://dify.long-cod.ts.net/datasets/{dataset_id}/...`
3. Copy the `{dataset_id}` (a standard UUID). Store this value.

## 3. API Credential Extraction

The crawler needs an API key with specific permissions for the dataset.

### Step 3.1: Navigate to API Management
1. Inside your new dataset, click **API Management** in the left sidebar.
2. Go to the **Dataset API Keys** tab.

### Step 3.2: Generate API Key
1. Click the **Create API Key** button.
2. Copy the generated key immediately.
3. The key format will be `dataset-{uuid}`.

> [!WARNING]
> This API key is shown only once. Store it securely in a password manager or secret vault immediately. It provides full read and write access to your dataset.

## 4. curl Smoke Tests

Verify your credentials before deploying to Kubernetes. Replace `{dataset_id}` and `{your-api-key}` with your actual values.

### A) List Documents (Verify Read Access)
```bash
curl -X GET "https://dify.long-cod.ts.net/v1/datasets/{dataset_id}/documents?page=1&limit=20" \
  -H "Authorization: Bearer dataset-{your-api-key}"
```
**Expected Outcome**: `200 OK`. If the dataset is new, you will receive a JSON response with an empty `data` array: `{"data": [], "has_more": false, "limit": 20, "total": 0}`.

### B) Create Test Document (Verify Write Access)
```bash
curl -X POST "https://dify.long-cod.ts.net/v1/datasets/{dataset_id}/document/create_by_text" \
  -H "Authorization: Bearer dataset-{your-api-key}" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "smoke-test-doc",
    "text": "This is a smoke test document to verify API write permissions.",
    "indexing_technique": "high_quality",
    "process_rule": {
      "mode": "automatic"
    }
  }'
```
**Expected Outcome**: `200 OK`. You will receive a JSON response containing a `document` object and a `batch` ID. Note the `batch` ID for the next step.

### C) Check Indexing Status (Verify Async Operations)
Replace `{batch_id}` with the ID from the previous step.
```bash
curl -X GET "https://dify.long-cod.ts.net/v1/datasets/{dataset_id}/documents/{batch_id}/indexing-status" \
  -H "Authorization: Bearer dataset-{your-api-key}"
```
**Expected Outcome**: `200 OK`. The `status` field in the JSON response should eventually show `completed`. It may show `indexing` for 10-30 seconds first.

## 5. Kubernetes Integration

Integrate these credentials into the existing Kubernetes deployment manifests.

### 5.1: Secrets (`dify-deploy/02-secrets.yaml`)
Add the following keys to the `dify-shared-secrets` Secret. You must base64 encode the values first.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: dify-shared-secrets
  namespace: dify
data:
  # ... existing keys ...
  DIFY_API_KEY: <base64-encoded dataset-{uuid}>
  DIFY_DATASET_ID: <base64-encoded uuid-without-prefix>
```

**Encoding Example**:
```bash
echo -n "dataset-abc123..." | base64
echo -n "abc123-def456-..." | base64
```

### 5.2: ConfigMap (`dify-deploy/01-configmap.yaml`)
Add the base URL to the `dify-shared-config` ConfigMap. These are plain text.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: dify-shared-config
  namespace: dify
data:
  # ... existing keys ...
  DIFY_BASE_URL: "https://dify.long-cod.ts.net/v1"
```

## 6. Troubleshooting

| Error Code | Potential Cause | Resolution |
| :--- | :--- | :--- |
| **401 Unauthorized** | API key is incorrect, expired, or missing prefix. | Verify the key includes the `dataset-` prefix and hasn't been deleted in Dify UI. |
| **404 Not Found** | `dataset_id` is wrong or the dataset was deleted. | Verify the ID against the URL bar in the Dify web UI. |
| **422 Unprocessable Entity** | Invalid request body or missing required fields. | Check your JSON syntax and ensure `indexing_technique` is specified. |
| **Indexing Stuck** | Dify background workers might be overloaded. | Wait up to 5 minutes. Check Dify service logs in the Kubernetes cluster. |
| **Empty Documents List** | Normal state for new datasets. | Run the Estimator King crawler to populate the dataset. |

## 7. Security Best Practices

- **Never Commit Keys**: Do not commit raw API keys to any git repository.
- **Masking**: The Estimator King crawler automatically masks these keys in logs.
- **Kubernetes Secrets**: Always use `Secret` objects for credentials, never `ConfigMap` or environment variables in Pod specs.
- **Rotation**: Rotate the `DIFY_API_KEY` every 90 days.
- **Principle of Least Privilege**: Ensure the API key is restricted to the specific dataset used by Estimator King.
