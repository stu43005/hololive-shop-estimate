# Dify Workflow API Contract Specification

This document defines the interface between the Estimator King Discord bot and the Dify Workflow API. It serves as a technical reference for bot developers implementing the price estimation feature (Wave 3).

## Purpose
The Workflow API provides price estimates for Shopify products based on historical data stored in the Dify Knowledge Base. The Discord bot sends product inquiries, and the workflow returns structured estimates using LLM reasoning and retrieved references.

## Base URL and Endpoint
- **Base URL**: `https://dify.long-cod.ts.net/v1`
- **Workflow Endpoint**: `POST /v1/workflows/run`
- **Authentication**: Bearer token via `Authorization` header.
- **API Key Format**: `app-{uuid}` (Extract from Dify UI "API" tab).

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
- `inputs.query`: Full text from the Discord modal. Each line should ideally represent one product.
- `response_mode`: Must be `"blocking"` to ensure the bot receives the full result before responding.
- `user`: Unique identifier for rate limiting and tracking. Format: `discord-{snowflake_id}`.

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

The Dify API returns a standard workflow run response with the estimation data in the `outputs` field.

### Response Body Structure
```json
{
  "workflow_run_id": "string",
  "task_id": "string", 
  "data": {
    "outputs": {
      "estimates": [
        {
          "product_name": "string",
          "suggested_price_jpy": "number",
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
      ]
    }
  },
  "status": "succeeded",
  "error": null,
  "elapsed_time": "number",
  "total_tokens": "number",
  "created_at": "number",
  "finished_at": "number"
}
```

### Field Specifications
- `data.outputs.estimates`: Array of objects, one per product identified in the query.
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
  "task_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "data": {
    "outputs": {
      "estimates": [
        {
          "product_name": "ホロライブ 誕生日ボイス 2025",
          "suggested_price_jpy": 2000,
          "price_range_jpy": {"min": 1800, "max": 2200},
          "confidence": "high",
          "rationale": "Birthday voice packs typically range 1800-2200 JPY based on 15 similar products in the knowledge base.",
          "reference_products": [
            {"name": "さくらみこ 誕生日ボイス 2024", "price_jpy": 2000, "store": "hololive"},
            {"name": "白上フブキ 誕生日ボイス 2024", "price_jpy": 1980, "store": "hololive"}
          ]
        },
        {
          "product_name": "さくらみこ 等身大タペストリー",
          "suggested_price_jpy": 12000,
          "price_range_jpy": {"min": 10000, "max": 15000},
          "confidence": "medium",
          "rationale": "Life-size tapestries for popular talents typically cost 10,000-15,000 JPY. Exact pricing depends on print quality.",
          "reference_products": [
            {"name": "兎田ぺこら 等身大タペストリー", "price_jpy": 11800, "store": "hololive"}
          ]
        }
      ]
    }
  },
  "status": "succeeded",
  "error": null,
  "elapsed_time": 4.523,
  "total_tokens": 2145,
  "created_at": 1708531200,
  "finished_at": 1708531204
}
```

---

## 3. Recommended Workflow Node Structure

To implement this contract in the Dify UI, follow this node chain:
`Start → Knowledge Retrieval → LLM → Code → End`

### Node Details
1. **Start Node**: Defines the `query` text variable.
2. **Knowledge Retrieval Node**:
   - Dataset: Estimator King Products (Task 13).
   - Top K: 10.
   - Score threshold: 0.7.
3. **LLM Node**:
   - Model: GPT-4o or Claude 3.5 Sonnet.
   - System Prompt: Instructions to parse multiline input and return JSON based on retrieved KB context.
4. **Code Node (Python)**:
   - Purpose: Extract JSON from LLM text output and validate structure.
   - Code snippet:
     ```python
     import json
     def main(llm_output):
         llm_text = llm_output
         if "```json" in llm_text:
             llm_text = llm_text.split("```json")[1].split("```")[0]
         estimates = json.loads(llm_text.strip())
         return {"outputs": {"estimates": estimates}}
     ```
5. **End Node**: Outputs the final JSON object.

---

## 4. Discord Bot Integration Guide

### Implementation Pattern (Python)
The Discord bot must defer the interaction because Dify calls usually exceed the 3-second limit.

```python
import httpx
import discord

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
        return response.json()
```

### Error Handling
- **401 Unauthorized**: Check `DIFY_WORKFLOW_API_KEY` validity.
- **400 Bad Request**: Validate input query length and user format.
- **Workflow Execution Failure**: Check the `status` field. If `"failed"`, the `error` field contains details.
- **Timeout**: The bot should handle cases where Dify takes longer than 30 seconds by sending an ephemeral error message.

---

## 5. Constraints and Limitations
- **Max Input**: 2000 characters (Discord modal limit).
- **Batch Size**: Recommended 1-10 products per request.
- **Timeout**: 30 seconds for end-to-end execution.
- **Rate Limit**: Default Dify rate limits apply per user.

---

## 6. Cross-References
- [Task 13: Knowledge Base Setup](task-13-ui-guide.md) - Defines the data retrieval source.
