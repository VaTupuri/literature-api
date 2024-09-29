from flask import Flask, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_cors import CORS  # Add this import
import psycopg2
from psycopg2.extras import RealDictCursor, Json
import random
import json
import uuid
import os
from collections import defaultdict
from itertools import combinations

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

socketio = SocketIO(app, cors_allowed_origins="*")

# Database setup
DB_HOST = "reed-db.cf2o2o4wczoh.us-east-2.rds.amazonaws.com"
DB_PORT = "5432"
DB_NAME = "postgres"
DB_USER = "vtupuri_reed"
DB_PASSWORD = os.environ["POSTGRES_PASSWORD"]

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD
    )

# Game logic
CARDS = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'Jack', 'Queen', 'King', 'Ace']
SUITS = ['Hearts', 'Diamonds', 'Clubs', 'Spades']

def deal_cards():
    deck = [f"{card} of {suit}" for card in CARDS for suit in SUITS] + ['Joker', 'Joker']
    random.shuffle(deck)
    return [deck[i:i+9] for i in range(0, 54, 9)]

def get_set_for_card(card):
    if card == 'Joker':
        return 8  # Set 9 (0-indexed)
    value, _, suit = card.split(' ')
    if value == '8':
        return 8  # Set 9 (0-indexed)
    if value in ['2', '3', '4', '5', '6', '7']:
        return {'Spades': 0, 'Hearts': 1, 'Clubs': 2, 'Diamonds': 3}[suit]
    return {'Spades': 4, 'Hearts': 5, 'Clubs': 6, 'Diamonds': 7}[suit]

@app.route('/create_room', methods=['POST'])
def create_room():
    room_id = str(uuid.uuid4())
    player_name = request.json.get('name')
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Create the room with the new schema
        cur.execute("""
        INSERT INTO rooms (id, current_round, game_status, current_turn, scores, additional_state)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """, (room_id, 0, 'setup', None, Json({}), Json({"started": False})))
        new_room_id = cur.fetchone()['id']
        conn.commit()

        # Add the creator as the first player
        cur.execute("""
        INSERT INTO players (room_id, name, team)
        VALUES (%s, %s, 0)
        RETURNING id
        """, (new_room_id, player_name))
        new_player_id = cur.fetchone()['id']

        conn.commit()
        return jsonify({"room_id": new_room_id, "player_id": new_player_id}), 201
    except psycopg2.Error as e:
        print(str(e))
        conn.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        cur.close()
        conn.close()

@app.route('/join_room/<room_id>', methods=['POST'])
def join_room_route(room_id):
    print("join_room_route called")
    player_name = request.json.get('name')
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("SELECT * FROM rooms WHERE id = %s", (room_id,))
    room = cur.fetchone()
    
    if not room:
        cur.close()
        conn.close()
        print("room not found")
        return jsonify({"error": "Room not found"}), 404
    
    cur.execute("SELECT * FROM players WHERE room_id = %s", (room_id,))
    players = cur.fetchall()
    if len(players) >= 6:
        cur.close()
        conn.close()
        print("room is full")
        return jsonify({"error": "Room is full"}), 400
    
    cur.execute(
        "INSERT INTO players (room_id, name, team) VALUES (%s, %s, %s) RETURNING id",
        (room_id, player_name, len(players) % 2)
    )
    new_player_id = cur.fetchone()['id']
    
    if len(players) + 1 == 6:
        print("starting game")
        # Start the game
        hands = deal_cards()
        for i, player in enumerate(players + [{'id': new_player_id}]):
            player_hand = hands[i]
            cur.execute(
                "UPDATE players SET cards = %s WHERE id = %s",
                (json.dumps(player_hand), player['id'])
            )
            # Emit hand update to each player
            socketio.emit('hand_updated', {'hand': player_hand}, room=str(player['id']))
        
        # Update room status
        current_turn = random.choice([p['id'] for p in players + [{'id': new_player_id}]])
        cur.execute("""
            UPDATE rooms 
            SET game_status = 'active', 
                current_turn = %s, 
                current_round = 1,
                additional_state = %s
            WHERE id = %s
        """, (current_turn, Json({"started": True}), room_id))
    else:
        # If the game hasn't started, get the current game state
        current_turn = room['current_turn']
        additional_state = room['additional_state']
    
    conn.commit()
    cur.close()
    conn.close()

    # Emit the updated player list to all clients in the room
    socketio.emit('update_players', {'players': players + [{'id': new_player_id, 'name': player_name}]}, room=room_id)

    # Emit game start event if the game is starting
    if len(players) + 1 == 6:
        socketio.emit('game_started', {'current_turn': current_turn}, room=room_id)
    else:
        # Emit current game state to the new player
        socketio.emit('game_state', {
            'started': additional_state.get('started', False),
            'current_turn': current_turn
        }, room=str(new_player_id))

    return jsonify({"success": True, "player_id": new_player_id, "room_id": room_id})

@app.route('/get_player_hand/<player_id>', methods=['GET'])
def get_player_hand(player_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("SELECT cards FROM players WHERE id = %s", (player_id,))
    player = cur.fetchone()
    
    cur.close()
    conn.close()
    
    if player and player['cards']:
        return jsonify({"hand": json.loads(player['cards'])})
    else:
        return jsonify({"error": "Player not found or has no cards"}), 404

@app.route('/get_room_players/<room_id>', methods=['GET'])
def get_room_players(room_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("SELECT id, name, team FROM players WHERE room_id = %s", (room_id,))
    players = cur.fetchall()
    
    cur.close()
    conn.close()
    
    return jsonify({'players': players})

@app.route('/get_player_team/<player_id>', methods=['GET'])
def get_player_team(player_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("SELECT team FROM players WHERE id = %s", (player_id,))
    player = cur.fetchone()
    
    cur.close()
    conn.close()
    
    if player:
        return jsonify({'team': player['team']})
    else:
        return jsonify({'error': 'Player not found'}), 404

@app.route('/get_current_turn/<room_id>', methods=['GET'])
def get_current_turn(room_id):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("SELECT current_turn, additional_state, scores FROM rooms WHERE id = %s", (room_id,))
    room = cur.fetchone()
    
    cur.close()
    conn.close()
    
    if room:
        return jsonify({
            "current_turn": room['current_turn'],
            "started": room['additional_state'].get('started', False),
            "scores": room['scores']
        })
    else:
        return jsonify({"error": "Room not found"}), 404

@socketio.on('get_players')
def handle_get_players(data):
    room_id = data['room_id']
    
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, name FROM players WHERE room_id = %s", (room_id,))
    players = cur.fetchall()
    cur.close()
    conn.close()

    emit('update_players', {'players': players}, room=room_id)
    
@socketio.on('connect')
def handle_connect():
    player_id = request.args.get('player_id')
    room_id = request.args.get('room_id')
    join_room(room_id)
    
    # Fetch all players in the room
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT id, name FROM players WHERE room_id = %s", (room_id,))
    players = cur.fetchall()
    
    # Fetch the player's hand
    cur.execute("SELECT cards FROM players WHERE id = %s", (player_id,))
    player = cur.fetchone()
    
    cur.close()
    conn.close()

    # Emit updated player list to all clients in the room
    emit('update_players', {'players': players}, room=room_id)
    
    # Emit hand update to the connected player
    if player and player['cards']:
        emit('hand_updated', {'hand': json.loads(player['cards'])}, room=player_id)

@socketio.on('disconnect')
def handle_disconnect():
    room_id = request.args.get('room_id')
    leave_room(room_id)

@socketio.on('ask_card')
def handle_ask_card(data):
    print("ask_card called")
    asking_player_id = data['asking_player_id']
    asked_player_id = data['asked_player_id']
    card = data['card']
    room_id = data['room_id']

    print("asking_player_id", asking_player_id)
    print("asked_player_id", asked_player_id)
    print("card", card)
    print("room_id", room_id)

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Get room and current turn
    cur.execute("SELECT current_turn, additional_state FROM rooms WHERE id = %s", (room_id,))
    room = cur.fetchone()
    
    if room['current_turn'] != asking_player_id:
        print("not your turn")
        emit('error', {'message': 'Not your turn'}, room=asking_player_id)
        cur.close()
        conn.close()
        return
    
    # Get players' cards
    cur.execute("SELECT id, cards FROM players WHERE id IN (%s, %s)", (asking_player_id, asked_player_id))
    players = {str(player['id']): json.loads(player['cards']) for player in cur.fetchall()}
    
    asking_player_cards = players[str(asking_player_id)]
    asked_player_cards = players[str(asked_player_id)]
    
    # Check if the ask is valid
    asked_card_set = get_set_for_card(card)
    asking_player_sets = defaultdict(list)
    for c in asking_player_cards:
        asking_player_sets[get_set_for_card(c)].append(c)
    
    if card in asking_player_cards:
        emit('error', {'message': 'You cannot ask for a card you already have'}, room=asking_player_id)
        cur.close()
        conn.close()
        return
    
    if asked_card_set not in asking_player_sets or len(asking_player_sets[asked_card_set]) == 0:
        emit('error', {'message': 'You must have a card in the set you are asking for'}, room=asking_player_id)
        cur.close()
        conn.close()
        return
    
    card_transferred = False
    print(asked_player_cards)
    if card in asked_player_cards:
        print("card in asked_player_cards")
        asked_player_cards.remove(card)
        asking_player_cards.append(card)
        card_transferred = True
        
        # Update players' cards in the database
        cur.execute("UPDATE players SET cards = %s WHERE id = %s", (json.dumps(asking_player_cards), asking_player_id))
        cur.execute("UPDATE players SET cards = %s WHERE id = %s", (json.dumps(asked_player_cards), asked_player_id))
        
        socketio.emit('card_transferred', {
            'from_player': asked_player_id,
            'to_player': asking_player_id,
            'card': card
        }, room=room_id)
        
        # Emit updated hands to both players
        socketio.emit('hand_updated', {
            'player_id': asking_player_id,
            'hand': asking_player_cards
        }, room=room_id)
        print(f"Emitting hand_updated to asked_player_id {asked_player_id}")
        socketio.emit('hand_updated', {
            'player_id': asked_player_id,
            'hand': asked_player_cards
        }, room=room_id)
    
    if not card_transferred:
        room['current_turn'] = asked_player_id
    
    # Update current turn in the database
    cur.execute("UPDATE rooms SET current_turn = %s WHERE id = %s", (room['current_turn'], room_id))
    
    conn.commit()
    cur.close()
    conn.close()
    
    emit('turn_changed', {'current_turn': room['current_turn']}, room=room_id)

@socketio.on('update_hand')
def handle_update_hand(data):
    player_id = data['player_id']
    room_id = data['room_id']
    
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("SELECT cards FROM players WHERE id = %s", (player_id,))
    player = cur.fetchone()
    
    cur.close()
    conn.close()
    
    if player and player['cards']:
        emit('hand_updated', {'hand': json.loads(player['cards'])}, room=player_id)
    else:
        emit('error', {'message': 'Failed to update hand'}, room=player_id)

@socketio.on('turn_changed')
def handle_turn_changed(data):
    room_id = data['room_id']
    new_turn = data['new_turn']
    
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    cur.execute("UPDATE rooms SET current_turn = %s WHERE id = %s", (new_turn, room_id))
    conn.commit()
    
    cur.close()
    conn.close()
    
    emit('turn_changed', {'current_turn': new_turn}, room=room_id)

def validate_set_declaration(set_cards, player_cards):
    return all(card in player_cards for card in set_cards)

@socketio.on('declare_set')
def handle_declare_set(data):
    declaring_player_id = data['declaring_player_id']
    room_id = data['room_id']
    set_declaration = data['set_declaration']
    
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    
    # Get room and current turn
    cur.execute("SELECT current_turn, scores FROM rooms WHERE id = %s", (room_id,))
    room = cur.fetchone()
    
    if room['current_turn'] != declaring_player_id:
        emit('error', {'message': 'Not your turn'}, room=declaring_player_id)
        cur.close()
        conn.close()
        return
    
    # Get all players in the room
    cur.execute("SELECT id, team, cards FROM players WHERE room_id = %s", (room_id,))
    players = cur.fetchall()
    
    declaring_player = next(p for p in players if str(p['id']) == declaring_player_id)
    declaring_team = declaring_player['team']
    opposing_team = 1 if declaring_team == 0 else 0
    
    # Validate the declaration and find actual card owners
    is_valid = True
    actual_card_owners = {}
    for player_id, cards in set_declaration.items():
        player = next(p for p in players if str(p['id']) == player_id)
        if player['team'] != declaring_team:
            is_valid = False
            break
        player_cards = json.loads(player['cards'])
        for card in cards:
            if card in player_cards:
                actual_card_owners[card] = player_id
            else:
                is_valid = False
                # Find the actual owner of the card
                for p in players:
                    if card in json.loads(p['cards']):
                        actual_card_owners[card] = str(p['id'])
                        break
    
    # Update scores
    scores = room['scores']
    if is_valid:
        scores[str(declaring_team)] = scores.get(str(declaring_team), 0) + 1
    else:
        scores[str(opposing_team)] = scores.get(str(opposing_team), 0) + 1
    
    # Check if a team has won
    winning_team = None
    if scores.get(str(declaring_team), 0) >= 5:
        winning_team = declaring_team
    elif scores.get(str(opposing_team), 0) >= 5:
        winning_team = opposing_team

    if winning_team is not None:
        # Mark the game as ended
        cur.execute("UPDATE rooms SET game_status = 'ended', additional_state = %s WHERE id = %s", 
                    (Json({"started": False, "ended": True, "winning_team": winning_team}), room_id))
    
    cur.execute("UPDATE rooms SET scores = %s WHERE id = %s", (Json(scores), room_id))
    
    # Remove declared cards from players' hands and update database
    affected_players = {}
    for card, owner_id in actual_card_owners.items():
        if owner_id not in affected_players:
            affected_players[owner_id] = json.loads(next(p['cards'] for p in players if str(p['id']) == owner_id))
        affected_players[owner_id].remove(card)
    
    for player_id, updated_hand in affected_players.items():
        cur.execute("UPDATE players SET cards = %s WHERE id = %s", (json.dumps(updated_hand), player_id))
    
    conn.commit()
    
    emit('set_declared', {
        'declaring_player': declaring_player_id,
        'set_declaration': set_declaration,
        'actual_card_owners': actual_card_owners,
        'scores': scores,
        'is_valid': is_valid,
        'declaring_team': declaring_team,
        'winning_team': winning_team
    }, room=room_id)
    
    # Emit hand updates for all affected players
    for player_id, updated_hand in affected_players.items():
        emit('hand_updated', {
            'player_id': player_id,
            'hand': updated_hand
        }, room=room_id)
    
    cur.close()
    conn.close()

if __name__ == '__main__':
    socketio.run(app, debug=True)
