from flask import Flask, request, jsonify
import requests
import os
from datetime import datetime, timedelta
import logging

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Monday.com API configuration
MONDAY_API_KEY = os.environ.get('MONDAY_API_KEY')
MONDAY_API_URL = 'https://api.monday.com/v2'

# Inventory item configuration - uses item name patterns to find items dynamically
INVENTORY_ITEMS = [
    {
        'name_pattern': 'Full Inventory list due',
        'days_offset': -28,
        'description': '28 days before deployment'
    },
    {
        'name_pattern': 'Update Shipping & Kitting Inventory',
        'days_offset': -21,
        'description': '21 days before deployment'
    },
    {
        'name_pattern': 'DEADLINE All Items Due at Shipping & Kitting Partner',
        'days_offset': -14,
        'description': '14 days before deployment'
    }
]

def monday_api_call(query, variables=None):
    """Make a call to Monday.com API"""
    headers = {
        'Authorization': MONDAY_API_KEY,
        'Content-Type': 'application/json',
        'API-Version': '2024-01'
    }
    data = {'query': query}
    if variables:
        data['variables'] = variables
    
    try:
        response = requests.post(MONDAY_API_URL, json=data, headers=headers)
        response.raise_for_status()
        result = response.json()
        
        # Log errors if any
        if 'errors' in result:
            logger.error(f"GraphQL errors: {result['errors']}")
        
        return result
    except Exception as e:
        logger.error(f"Monday API call failed: {str(e)}")
        return None

def get_board_items(board_id):
    """Get all items from a board including archived ones"""
    query = '''
    query ($boardId: ID!) {
        boards(ids: [$boardId]) {
            items_page(limit: 200, query_params: {rules: []}) {
                items {
                    id
                    name
                    column_values {
                        id
                        type
                        text
                        value
                    }
                }
            }
            columns {
                id
                title
                type
            }
        }
    }
    '''
    
    result = monday_api_call(query, {'boardId': str(board_id)})
    if result and 'data' in result and result['data']['boards']:
        board = result['data']['boards'][0]
        return {
            'items': board['items_page']['items'],
            'columns': board['columns']
        }
    return None

def find_timeline_column(columns):
    """Find the timeline column ID"""
    for col in columns:
        if col['type'] == 'timeline':
            return col['id']
    return None

def find_item_by_name_pattern(items, pattern):
    """Find an item by name pattern (case-insensitive partial match)"""
    pattern_lower = pattern.lower()
    for item in items:
        if pattern_lower in item['name'].lower():
            return item
    return None

def get_timeline_value(item, timeline_column_id):
    """Extract timeline start date from item"""
    for col_value in item['column_values']:
        if col_value['id'] == timeline_column_id:
            # Check text value first
            if col_value['text'] and col_value['text'].strip():
                # Parse text format like "2026-08-12 - 2026-08-12"
                try:
                    date_str = col_value['text'].split(' - ')[0].strip()
                    return date_str
                except:
                    pass
            
            # Try JSON value
            if col_value['value']:
                try:
                    import json
                    value_data = json.loads(col_value['value'])
                    if 'from' in value_data:
                        return value_data['from']
                except:
                    pass
    return None

def update_item_timeline(board_id, item_id, column_id, start_date, end_date):
    """Update an item's timeline column"""
    query = '''
    mutation ($boardId: ID!, $itemId: ID!, $columnId: String!, $value: JSON!) {
        change_column_value(
            board_id: $boardId,
            item_id: $itemId,
            column_id: $columnId,
            value: $value
        ) {
            id
        }
    }
    '''
    
    import json
    value = json.dumps({
        'from': start_date,
        'to': end_date
    })
    
    variables = {
        'boardId': str(board_id),
        'itemId': str(item_id),
        'columnId': column_id,
        'value': value
    }
    
    result = monday_api_call(query, variables)
    
    if result and 'data' in result and result['data'].get('change_column_value'):
        return True
    
    return False

def process_deployment_date_change(board_id, item_id):
    """Process deployment date change and update inventory items"""
    logger.info(f"Processing deployment date change for board {board_id}, item {item_id}")
    
    # Get all board items and columns
    board_data = get_board_items(board_id)
    if not board_data:
        logger.error("Failed to fetch board data")
        return False
    
    items = board_data['items']
    columns = board_data['columns']
    
    logger.info(f"Found {len(items)} items on board")
    
    # Find timeline column
    timeline_column_id = find_timeline_column(columns)
    if not timeline_column_id:
        logger.error("No timeline column found on board")
        return False
    
    logger.info(f"Found timeline column: {timeline_column_id}")
    
    # Find the deployment date item
    deployment_item = None
    for item in items:
        if item['id'] == str(item_id):
            deployment_item = item
            break
    
    if not deployment_item:
        logger.error(f"Deployment item {item_id} not found")
        return False
    
    logger.info(f"Found deployment item: {deployment_item['name']}")
    
    # Get deployment date
    deployment_date_str = get_timeline_value(deployment_item, timeline_column_id)
    if not deployment_date_str:
        logger.warning("No deployment date set, skipping update")
        return {'status': 'skipped', 'reason': 'No deployment date set'}
    
    logger.info(f"Deployment date: {deployment_date_str}")
    
    # Parse deployment date
    try:
        deployment_date = datetime.strptime(deployment_date_str, '%Y-%m-%d')
    except ValueError:
        logger.error(f"Invalid date format: {deployment_date_str}")
        return False
    
    # Update each inventory item
    updates_made = 0
    results = []
    
    for config in INVENTORY_ITEMS:
        # Find the inventory item by name pattern
        inventory_item = find_item_by_name_pattern(items, config['name_pattern'])
        
        if not inventory_item:
            logger.warning(f"Item matching '{config['name_pattern']}' not found")
            results.append({
                'pattern': config['name_pattern'],
                'status': 'not_found'
            })
            continue
        
        logger.info(f"Found inventory item: {inventory_item['name']}")
        
        # Calculate new date
        new_date = deployment_date + timedelta(days=config['days_offset'])
        new_date_str = new_date.strftime('%Y-%m-%d')
        
        logger.info(f"Updating {inventory_item['name']} to {new_date_str} ({config['description']})")
        
        # Update the item
        success = update_item_timeline(
            board_id,
            inventory_item['id'],
            timeline_column_id,
            new_date_str,
            new_date_str
        )
        
        if success:
            updates_made += 1
            logger.info(f"✓ Successfully updated {inventory_item['name']}")
            results.append({
                'item': inventory_item['name'],
                'date': new_date_str,
                'status': 'success'
            })
        else:
            logger.error(f"✗ Failed to update {inventory_item['name']}")
            results.append({
                'item': inventory_item['name'],
                'status': 'failed'
            })
    
    logger.info(f"Completed: {updates_made}/{len(INVENTORY_ITEMS)} items updated")
    
    return {
        'status': 'completed',
        'updates_made': updates_made,
        'total_items': len(INVENTORY_ITEMS),
        'results': results
    }

@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle Monday.com webhook"""
    try:
        data = request.json
        logger.info(f"Received webhook: {data}")
        
        # Handle challenge verification
        if 'challenge' in data:
            logger.info("Responding to challenge")
            return jsonify({'challenge': data['challenge']})
        
        # Handle webhook event
        if 'event' in data:
            event = data['event']
            
            # Check if this is a column value change event
            if event.get('type') == 'update_column_value':
                board_id = event.get('boardId')
                item_id = event.get('pulseId')
                column_id = event.get('columnId')
                
                logger.info(f"Column change detected - Board: {board_id}, Item: {item_id}, Column: {column_id}")
                
                # Get board data to check if this is the deployment item
                board_data = get_board_items(board_id)
                if board_data:
                    timeline_column_id = find_timeline_column(board_data['columns'])
                    
                    # Only process if timeline column changed
                    if column_id == timeline_column_id:
                        # Check if the changed item is a deployment item
                        changed_item = None
                        for item in board_data['items']:
                            if item['id'] == str(item_id):
                                changed_item = item
                                break
                        
                        if changed_item and 'DEPLOYMENT DATE' in changed_item['name'].upper():
                            logger.info(f"Deployment date changed, processing updates...")
                            result = process_deployment_date_change(board_id, item_id)
                            return jsonify(result), 200
                        else:
                            logger.info(f"Timeline changed but not on deployment item, skipping")
                    else:
                        logger.info(f"Non-timeline column changed, skipping")
        
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        logger.error(f"Webhook error: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 200

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({'status': 'healthy'}), 200

@app.route('/manual-sync', methods=['POST'])
def manual_sync():
    """Manual sync endpoint for testing"""
    try:
        data = request.json
        board_id = data.get('board_id')
        item_id = data.get('item_id')
        
        if not board_id or not item_id:
            return jsonify({'error': 'board_id and item_id required'}), 400
        
        result = process_deployment_date_change(board_id, item_id)
        
        return jsonify(result), 200
            
    except Exception as e:
        logger.error(f"Manual sync error: {str(e)}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
