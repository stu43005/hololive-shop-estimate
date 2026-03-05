# Dify Dataset UI Setup Guide

This guide provides step-by-step instructions for creating a Dify Knowledge Base dataset and extracting the API credentials required for the Estimator King crawler.

- **Date**: 2026-03-06
- **Target Audience**: Ops engineers deploying Estimator King to Kubernetes
- **Prerequisites**: Access to the Dify instance at https://dify.long-cod.ts.net

## 1. Introduction

The Estimator King crawler requires a Dify Knowledge Base dataset to store and index product information. This dataset acts as the "brain" for the Estimator King Discord bot, providing the necessary context for accurate estimations.

## 2. Step-by-Step UI Setup

Follow these steps to create a new dataset in the Dify web UI.

### Step 2.1: Login
1. Navigate to [https://dify.long-cod.ts.net](https://dify.long-cod.ts.net).
2. Login with your credentials.

### Step 2.2: Create Empty Knowledge Base
1. Click the **Knowledge** section in the top navigation bar.
2. Click the **Create Knowledge** button (top-right area of the page).
3. On the data source selection page, click the **Create an empty knowledge base** link at the bottom of the dialog.
4. In the popup modal, enter a descriptive name (max 40 characters), e.g., `Estimator King Products`.
5. Click **Create**.

> **Note**: When creating an empty Knowledge Base, the UI only asks for a **name**. There is no description field, embedding model selection, index method, or retrieval settings at this step. These are configured automatically when the first document is added — either via the UI "Add Document" wizard or via the API.

### Step 2.3: Extract Dataset ID
1. Once the dataset is created, you are redirected to the dataset's **Documents** page.
2. Look at the URL in your browser's address bar.
3. The URL format is `https://dify.long-cod.ts.net/datasets/{dataset_id}/documents`.
4. Copy the `{dataset_id}` (a standard UUID). Store this value.

## 3. API Credential Extraction

The crawler needs a **Dataset API Key** to interact with the Knowledge Base via the Service API.

> **Important**: Dify has two types of API keys:
> - **Dataset API Key** (prefix `dataset-`): Used for Knowledge Base operations (create/list/delete documents). This is what the crawler needs.
> - **App API Key** (prefix `app-`): Used for running Workflow/Chat apps. This is covered in the [Workflow Contract](dify-workflow-contract.md).

### Step 3.1: Navigate to API Access

There is **no "API Management" tab** in the left sidebar. Instead:

1. Open your newly created dataset (click into it from the Knowledge page).
2. In the left sidebar, scroll down to the **bottom section** where extra information is displayed.
3. You will see an **API** section showing the API access status and endpoint URL.
4. If API access is disabled (yellow indicator), click the toggle to **enable** it.

### Step 3.2: Generate API Key
1. In the API section at the bottom of the sidebar, click the **API Key** button.
2. A **Secret Key** modal will open, listing any existing keys.
3. Click **Create Secret Key** (or the **+** button).
4. A new key will be generated and displayed. **Copy it immediately.**
5. The key format is `dataset-` followed by 24 random characters (e.g., `dataset-aBcDeFgHiJkLmNoPqRsTuVwX`).

> [!WARNING]
> This API key is shown only once. Store it securely in a password manager or secret vault immediately. It provides full read and write access to **all** datasets in the workspace.

### Step 3.3: Find the API Base URL
The API section in the sidebar also displays the **API Base URL** with a copy button. The base URL should be:
```
https://dify.long-cod.ts.net/v1
```

## 4. curl Smoke Tests

Verify your credentials before deploying to Kubernetes. Replace `{dataset_id}` and `{your-api-key}` with your actual values.

> **Note**: The `{your-api-key}` should be the **full key** including the `dataset-` prefix.

### A) List Documents (Verify Read Access)
```bash
curl -X GET "https://dify.long-cod.ts.net/v1/datasets/{dataset_id}/documents?page=1&limit=20" \
  -H "Authorization: Bearer {your-api-key}"
```
**Expected Outcome**: `200 OK`. If the dataset is new, you will receive a JSON response with an empty `data` array: `{"data": [], "has_more": false, "limit": 20, "total": 0, "page": 1}`.

### B) Create Test Document (Verify Write Access)
```bash
curl -X POST "https://dify.long-cod.ts.net/v1/datasets/{dataset_id}/document/create-by-text" \
  -H "Authorization: Bearer {your-api-key}" \
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
  -H "Authorization: Bearer {your-api-key}"
```
**Expected Outcome**: `200 OK`. The `indexing_status` field in the JSON response should eventually show `completed`. It may show `indexing` for 10-30 seconds first.

## 5. Kubernetes Integration

Integrate these credentials into the existing Kubernetes deployment manifests.

### 5.1: Secrets (`deploy/secrets.yaml`)
Add the following keys to the Estimator King Secret. You must base64 encode the values first.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: estimator-king-secrets
  namespace: dify
data:
  # ... existing keys ...
  DIFY_API_KEY: <base64-encoded full-api-key-including-prefix>
  DIFY_DATASET_ID: <base64-encoded dataset-uuid>
```

**Encoding Example**:
```bash
echo -n "dataset-aBcDeFgHiJkLmNoPqRsTuVwX" | base64
echo -n "550e8400-e29b-41d4-a716-446655440000" | base64
```

### 5.2: ConfigMap (`deploy/configmap.yaml`)
Add the base URL to the Estimator King ConfigMap. These are plain text.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: estimator-king-config
  namespace: dify
data:
  # ... existing keys ...
  DIFY_BASE_URL: "https://dify.long-cod.ts.net/v1"
```

## 6. Troubleshooting

| Error Code | Potential Cause | Resolution |
| :--- | :--- | :--- |
| **401 Unauthorized** | API key is incorrect, expired, or missing `dataset-` prefix. | Verify the key includes the `dataset-` prefix and hasn't been deleted in Dify UI. Regenerate if needed. |
| **403 Forbidden** | Dataset API access is disabled. | Go to the dataset's sidebar → API section → toggle API access to **enabled**. |
| **404 Not Found** | `dataset_id` is wrong or the dataset was deleted. | Verify the ID against the URL bar in the Dify web UI (visible on the Documents page). |
| **422 Unprocessable Entity** | Invalid request body or missing required fields. | Check your JSON syntax and ensure `indexing_technique` is specified (`high_quality` or `economy`). |
| **Indexing Stuck** | Dify background workers might be overloaded. | Wait up to 5 minutes. Check Dify service logs in the Kubernetes cluster. |
| **Empty Documents List** | Normal state for new datasets. | Run the Estimator King crawler to populate the dataset. |

## 7. Security Best Practices

- **Never Commit Keys**: Do not commit raw API keys to any git repository.
- **Masking**: The Estimator King crawler automatically masks these keys in logs.
- **Kubernetes Secrets**: Always use `Secret` objects for credentials, never `ConfigMap` or environment variables in Pod specs.
- **Rotation**: Rotate the `DIFY_API_KEY` every 90 days.
- **Workspace Scope**: The `dataset-` prefixed API keys provide access to **all datasets** in the workspace. Be mindful of who has access.
