#!/usr/bin/env python3
from flask import Flask, request, jsonify
import os
import json
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional

app = Flask(__name__)

BOARD_ID = "18417107340"
DEPLOYMENT_ITEM_ID = "12237339740"
TIMELINE_COLUMN_ID = "timerange_mm45hv8d"
PHASE_COLUMN_ID = "color_mm454nqv"
INVENTORY_PHASE_INDEX = 3

DATE_OFFSETS = {
    "DEADLINE All Items Due at Shipping & Kitting Partner": -14,
    "Update Shipping & Kitting Inventory": -21,
    "Full Inventory list due": -28,
}

MONDAY_API_URL = "https://api.monday.com/v2"
API_TOKEN = os.environ.get("MONDAY_API_TOKEN")


def execute_query(query: str, variables: Optional[Dict] = None) -> Dict:
    headers = {
        "Authorization": API_TOKEN,
        "Content-Type": "application/json"
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    response = requests.post(MONDAY_API_URL, json=payload, headers=headers)
    response.raise_for_status()
    result = response.json()
    if "errors" in result:
        raise Exception(f"GraphQL errors: {result['errors']}")
    return result["data"]


def get_deployment_date() -> Optional[datetime]:
    query = """
    query ($itemId: [ID!]) {
        items(ids: $itemId) {
            column_values(ids: ["timerange_mm45hv8d"]) {
                value
            }
        }
    }
    """
    variables = {"itemId": [DEPLOYMENT_ITEM_ID]}
    data = execute_query(query, variables)
    if not data["items"]:
        return None
    timeline_value = data["items"][0]["column_values"][0]["value"]
    if not timeline_value or timeline_value == "null":
        return None
    timeline_data = json.loads(timeline_value)
    deployment_date_str = timeline_data.get("to")
    if not deployment_date_str:
        return None
    return datetime.strptime(deployment_date_str, "%Y-%m-%d")


def get_inventory_items() -> List[Dict]:
    query = """
    query ($boardId: [ID!]) {
        boards(ids: $boardId) {
            items_page(limit: 500) {
                items {
                    id
                    name
                    column_values(ids: ["color_mm454nqv"]) {
                        value
                    }
                }
            }
        }
    }
    """
    variables = {"boardId": [BOARD_ID]}
    data = execute_query(query, variables)
    all_items = data["boards"][0]["items_page"]["items"]
    inventory_items = []
    for item in all_items:
        phase_value = item["column_values"][0]["value"]
        if phase_value and phase_value != "null":
            phase_data = json.loads(phase_value)
            if phase_data.get("index") == INVENTORY_PHASE_INDEX:
                inventory_items.append(item)
    return inventory_items

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = request.json
        
        # Handle Monday.com challenge verification
        if payload.get("challenge"):
            return jsonify({"challenge": payload["challenge"]}), 200
        
        # Verify this is a column change event
        if payload.get("event", {}).get("type") != "update_column_value":
            return jsonify({"status": "ignored"}), 200
        
        # Check if it's the deployment item and timeline column
        item_id = str(payload.get("event", {}).get("pulseId", ""))
        column_id = payload.get("event", {}).get("columnId", "")
        
        if item_id != DEPLOYMENT_ITEM_ID:
            return jsonify({"status": "ignored"}), 200
        
        if column_id != TIMELINE_COLUMN_ID:
            return jsonify({"status": "ignored"}), 200
        
        # Trigger the sync
        print(f"Deployment date changed - triggering sync...")
        result = sync_inventory_dates()
        
        return jsonify(result), 200
        
    except Exception as e:
        print(f"Error processing webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500
        

@app.route("/manual-sync", methods=["POST"])
def manual_sync():
    try:
        result = sync_inventory_dates()
        return jsonify(result), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
