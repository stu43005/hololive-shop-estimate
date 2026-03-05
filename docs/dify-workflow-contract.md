# Dify Workflow API Contract Specification

This document defines the interface between the Estimator King Discord bot and the Dify Workflow API. It serves as a technical reference for bot developers implementing the price estimation feature (Wave 3).

## Purpose
The Workflow API provides price estimates for Shopify products based on historical data stored in the Dify Knowledge Base. The Discord bot sends product inquiries, and the workflow returns structured estimates using LLM reasoning and retrieved references.

## Base URL and Endpoint
- **Base URL**: `https://dify.long-cod.ts.net/v1`
- **Workflow Endpoint**: `POST /v1/workflows/run`
- **Authentication**: Bearer token via `Authorization` header.
- **API Key Format**: `app-` followed by random characters (e.g., `app-aBcDeFgHiJkLmNoPqRsTuVwX`). Extracted from the Workflow app's **"Access API"** page in Dify — see [Section 3.0](#30-prerequisite-create-the-workflow-app).

> **Important**: The Workflow API key (`app-` prefix) is different from the Dataset API key (`dataset-` prefix). The workflow key is specific to the Workflow app you create; the dataset key is for direct Knowledge Base operations (see [Dataset Setup Guide](dify-dataset-setup.md)).

---

## 1. Workflow Input Schema

The Discord bot must send a JSON payload containing the user's product query.

### Request Body
```json
{
  "inputs": {
    "query": "string - User inquiry (multiline text, 1-10 products)"
  },
  "response_mode": "blocking",
  "user": "string - Discord user identifier"
}
```

### Field Specifications
- `inputs.query`: Full text from the Discord modal. Each line should ideally represent one product. This **must match** the variable name defined in the Start node (see [Section 3.1](#31-start-node)).
- `response_mode`: Must be `"blocking"` to ensure the bot receives the full result before responding. Also supports `"streaming"` for Server-Sent Events.
- `user`: **Required**. Unique identifier for rate limiting and tracking. Format: `discord-{snowflake_id}`.

### Example Request
```json
{
  "inputs": {
    "query": "ホロライブ 誕生日ボイス 2025\nさくらみこ 等身大タペストリー"
  },
  "response_mode": "blocking",
  "user": "discord-987654321"
}
```

---

## 2. Workflow Output Schema

The Dify API returns a standard workflow run response with the estimation data in the `data.outputs` field.

### Response Body Structure (Blocking Mode)
```json
{
  "workflow_run_id": "string",
  "data": {
    "id": "string (workflow run ID)",
    "workflow_id": "string (workflow definition ID)",
    "status": "succeeded | failed | stopped",
    "inputs": {
      "query": "string (echo of your input)"
    },
    "outputs": {
      "estimates": "string (JSON-formatted estimation results)"
    },
    "error": "string | null",
    "total_steps": "number",
    "total_tokens": "number",
    "created_at": "number (unix timestamp)",
    "finished_at": "number (unix timestamp) | null",
    "elapsed_time": "number (seconds, float)"
  }
}
```

> **Note**: The blocking response does **not** include a top-level `task_id` field. The `data.outputs` values are strings (as defined by the End node), so the bot must parse the `estimates` field from a JSON string.

### Output Field Specifications
- `data.status`: One of `"succeeded"`, `"failed"`, or `"stopped"`.
- `data.outputs.estimates`: A **JSON string** containing an array of estimate objects. The bot must `JSON.parse()` / `json.loads()` this value.
- `data.inputs`: Echo of the inputs sent in the request.
- `data.total_steps`: Number of workflow nodes executed.
- `data.error`: Error message if `status` is `"failed"`, otherwise `null`.

### Parsed `estimates` Structure
After parsing the `estimates` string, each object has:
```json
{
  "product_name": "string",
  "suggested_price_jpy": "number (integer)",
  "price_range_jpy": {
    "min": "number",
    "max": "number"
  },
  "confidence": "string (high/medium/low)",
  "rationale": "string (2-3 sentences)",
  "reference_products": [
    {
      "name": "string",
      "price_jpy": "number",
      "store": "string (hololive/vspo)"
    }
  ]
}
```

- `suggested_price_jpy`: The primary estimate in JPY (integer).
- `price_range_jpy`: A range indicating estimation uncertainty.
- `confidence`: Qualitative measure of estimate quality:
  - `high`: Direct match or very close variant found in Knowledge Base.
  - `medium`: Similar product types found; reasonable inference.
  - `low`: No strong matches found; general category guess.
- `rationale`: Explanation for the estimate (displayed to user).
- `reference_products`: Up to 3 evidence items retrieved from the Knowledge Base.

### Example Success Response
```json
{
  "workflow_run_id": "550e8400-e29b-41d4-a716-446655440000",
  "data": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "workflow_id": "7e9c1a3b-2f4d-4e5a-8b6c-9d0e1f2a3b4c",
    "status": "succeeded",
    "inputs": {
      "query": "ホロライブ 誕生日ボイス 2025\nさくらみこ 等身大タペストリー"
    },
    "outputs": {
      "estimates": "[{\"product_name\":\"ホロライブ 誕生日ボイス 2025\",\"suggested_price_jpy\":2000,\"price_range_jpy\":{\"min\":1800,\"max\":2200},\"confidence\":\"high\",\"rationale\":\"Birthday voice packs typically range 1800-2200 JPY based on 15 similar products in the knowledge base.\",\"reference_products\":[{\"name\":\"さくらみこ 誕生日ボイス 2024\",\"price_jpy\":2000,\"store\":\"hololive\"},{\"name\":\"白上フブキ 誕生日ボイス 2024\",\"price_jpy\":1980,\"store\":\"hololive\"}]},{\"product_name\":\"さくらみこ 等身大タペストリー\",\"suggested_price_jpy\":12000,\"price_range_jpy\":{\"min\":10000,\"max\":15000},\"confidence\":\"medium\",\"rationale\":\"Life-size tapestries for popular talents typically cost 10,000-15,000 JPY. Exact pricing depends on print quality.\",\"reference_products\":[{\"name\":\"兎田ぺこら 等身大タペストリー\",\"price_jpy\":11800,\"store\":\"hololive\"}]}]"
    },
    "error": null,
    "total_steps": 4,
    "total_tokens": 2145,
    "created_at": 1708531200,
    "finished_at": 1708531204,
    "elapsed_time": 4.523
  }
}
```

---

## 3. Workflow Setup in Dify UI

This section provides step-by-step instructions to build the workflow in the Dify web UI.

### 3.0: Prerequisite — Create the Workflow App

Before configuring nodes, you need a Workflow-type app:

1. Click **Studio** in the top navigation bar.
2. Click the **Create App** button.
3. In the creation modal, select **Workflow** as the app type.
4. Enter a name, e.g., `Estimator King Workflow`.
5. Click **Create**. You'll be taken to the workflow editor canvas.

#### Get the Workflow API Key

After creating the app (and ideally after publishing it):

1. In the workflow app, click **Access API** (or **Publish** → **Access API**) in the top-right area.
2. You'll be taken to the API access page.
3. Click **API Key** in the page.
4. Click **Create Secret Key** (or the **+** button).
5. Copy the generated key immediately — it's shown only once.
6. The key format is `app-` followed by random characters.

### Recommended Node Chain

```
Start → Knowledge Retrieval → LLM → Code → End
```

### 3.1: Start Node

The Start node defines the **input variables** that the API caller must provide.

**Configuration:**
1. Click the **Start** node on the canvas.
2. In the right panel, you'll see the **Input Fields** section.
3. Click the **+** button to add a variable.
4. Configure the variable:
   - **Field Type**: Select **Short Text** (or **Paragraph** for longer input).
   - **Variable Name**: `query` (this must match `inputs.query` in the API request).
   - **Label**: `Product Query` (display name, shown in test UI).
   - **Required**: Toggle **on**.
   - **Max Length**: 2000 (matching Discord modal limit).

> **Important**: The variable name in the Start node (`query`) must exactly match the key used in the API `inputs` object. If you name it `product_query` here, the API request must use `inputs.product_query`.

### 3.2: Knowledge Retrieval Node

This node searches the Estimator King Knowledge Base for relevant product data.

**Configuration:**
1. Drag a **Knowledge Retrieval** node from the node palette onto the canvas.
2. Connect the Start node's output to this node's input.
3. Click the Knowledge Retrieval node to open its settings panel.

**Panel Fields:**
- **Query Text** (required): Click the variable picker and select **Start / query** (the input variable from the Start node). This tells the node what text to search for.
- **Knowledge** (required): Click the **+** (Add) button in the Knowledge section to open the dataset selector. Find and select your `Estimator King Products` dataset. Multiple datasets can be added.

> **Troubleshooting: Dataset not showing up?**
> - The dataset must exist and have at least one indexed document. An empty dataset with no documents may not appear or return results.
> - Ensure you are logged in as a user with access to the dataset (check permissions on the Knowledge page).
> - The dataset must have its embedding model configured. This happens automatically when the first document is indexed.

- **Retrieval Mode**: Choose between:
  - **N-to-1 Retrieval** (`single`): Uses an LLM to reason about which dataset to query first. Requires selecting a **Model** for routing. Better when you have multiple datasets and want intelligent selection.
  - **Multi-Path Retrieval** (`multiple`): Queries all selected datasets simultaneously and merges results. Configure:
    - **TopK**: `10` (number of text chunks to retrieve).
    - **Score Threshold**: `0.5` (minimum relevance score; set to 0 to disable). Toggle on the **Score Threshold** switch to enable it.
    - **Reranking**: Optionally enable a reranking model to improve result quality.

**Recommended for Estimator King**: Use **Multi-Path Retrieval** with TopK=10 and Score Threshold=0.5 (adjust based on testing).

### 3.3: LLM Node

This node uses an LLM to generate price estimates based on the retrieved context.

**Configuration:**
1. Drag an **LLM** node onto the canvas and connect it after Knowledge Retrieval.
2. Click the LLM node to open its settings panel.

**Panel Fields:**
- **Model**: Select your preferred model (e.g., GPT-4o, Claude 3.5 Sonnet) from the dropdown.
- **Context**: Click the **+** button in the Context section. Select the **Knowledge Retrieval** node's `result` output. This passes the retrieved documents as context to the LLM.
- **SYSTEM prompt**: Write the instruction prompt. Use `{{#context#}}` to reference the context variable, and reference the Start node's query with `{{#start_node_id.query#}}` or use the variable picker to insert it. Example:

```
You are the Estimator King, a price estimation assistant for Japanese merchandise.

Given the user's product query and the reference data from the knowledge base, provide price estimates.

## Context (Knowledge Base Results)
{{#context#}}

## Instructions
1. Parse the user query. Each line is a separate product.
2. For each product, find the closest matches in the context.
3. Return a JSON array of estimate objects with this structure:
[
  {
    "product_name": "string",
    "suggested_price_jpy": number,
    "price_range_jpy": {"min": number, "max": number},
    "confidence": "high|medium|low",
    "rationale": "2-3 sentence explanation",
    "reference_products": [{"name": "string", "price_jpy": number, "store": "hololive|vspo"}]
  }
]
4. Return ONLY the JSON array, no markdown formatting or extra text.
```

- **USER prompt**: Reference the user's query. Use the variable picker to insert `{{#start_node_id.query#}}` (replace `start_node_id` with the actual Start node's ID shown in the canvas). Or simply write:

```
{{#start_node_id.query#}}
```

### 3.4: Code Node (Python)

This node extracts and validates the JSON from the LLM's text output.

**Configuration:**
1. Drag a **Code** node onto the canvas and connect it after the LLM node.
2. Click the Code node to open its settings panel.

**Panel Fields:**
- **Input Variables**: Add an input variable:
  - Click **+** to add a variable.
  - **Variable Name**: `llm_output`
  - **Value**: Use the variable picker to select the **LLM** node's `text` output.

- **Code** (Python 3):
```python
import json

def main(llm_output: str) -> dict:
    text = llm_output
    # Strip markdown code fences if present
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]

    estimates = json.loads(text.strip())

    # Ensure it's a list
    if not isinstance(estimates, list):
        estimates = [estimates]

    return {
        "result": json.dumps(estimates, ensure_ascii=False)
    }
```

- **Output Variables**: Define the output:
  - **Variable Name**: `result`
  - **Type**: `String`

> **Important**: Code node functions must return a `dict`. Each key in the returned dict corresponds to an output variable defined in the Output Variables section. The variable names must match exactly.

### 3.5: End Node

The End node defines what the API returns in `data.outputs`.

**Configuration:**
1. Click the **End** node on the canvas (it's pre-placed on the canvas).
2. In the right panel, you'll see the **Output Variables** section.
3. Click the **+** button to add an output variable.
4. Configure:
   - **Variable Name**: `estimates`
   - **Value**: Use the variable picker to select the **Code** node's `result` output.

> **Important**: The variable names defined here become the keys in the API response's `data.outputs` object. Since `result` from the Code node is a JSON string, `data.outputs.estimates` will be a string that the bot must parse.

### 3.6: Publish the Workflow

After configuring all nodes:
1. Click **Preview** (top-right) to test the workflow with sample input.
2. Once satisfied, click **Publish** to make the workflow available via API.

> **Note**: The workflow must be **published** for the API endpoint to work. Unpublished workflows will return errors when called via API.

---

## 4. Discord Bot Integration Guide

### Implementation Pattern (Python)
The Discord bot must defer the interaction because Dify calls usually exceed the 3-second limit.

```python
import httpx
import json

DIFY_WORKFLOW_API_KEY = "app-..."  # From Section 3.0

async def call_estimator(product_lines: str, user_id: str):
    url = "https://dify.long-cod.ts.net/v1/workflows/run"
    headers = {
        "Authorization": f"Bearer {DIFY_WORKFLOW_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "inputs": {"query": product_lines},
        "response_mode": "blocking",
        "user": f"discord-{user_id}"
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()

    # Check workflow execution status
    if result["data"]["status"] != "succeeded":
        raise RuntimeError(f"Workflow failed: {result['data'].get('error')}")

    # Parse the estimates from the JSON string output
    estimates_str = result["data"]["outputs"]["estimates"]
    estimates = json.loads(estimates_str)
    return estimates
```

### Error Handling
- **401 Unauthorized**: Check `DIFY_WORKFLOW_API_KEY` validity. Ensure it uses the `app-` prefix (not `dataset-`).
- **400 Bad Request**: Validate input query length, user format, and that `inputs` keys match the Start node variable names.
- **404 Not Found**: The workflow may not be published yet. Publish it from the Dify UI.
- **Workflow Execution Failure**: Check the `data.status` field. If `"failed"`, the `data.error` field contains details.
- **Timeout**: The bot should handle cases where Dify takes longer than 30 seconds by sending an ephemeral error message.

---

## 5. Constraints and Limitations
- **Max Input**: 2000 characters (Discord modal limit).
- **Batch Size**: Recommended 1-10 products per request.
- **Timeout**: 30 seconds for end-to-end execution.
- **Rate Limit**: Default Dify rate limits apply per user.
- **Workflow must be published**: Unpublished workflows cannot be called via API.

---

## 6. Cross-References
- [Dataset Setup Guide](dify-dataset-setup.md) - Knowledge Base creation and Dataset API key setup.
