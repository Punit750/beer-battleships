"""
server.py

An enhanced implementation of a Battleship server supporting:
- Multiple concurrent connections
- Spectator functionality with prioritization
- Reconnection support
- Improved game transitions
- Custom protocol with checksums (T4.1)
"""
import socket
import queue
import threading
import time
import os
import sys


from battleship import run_multiplayer_game_online, GameSession
from Protocol import (
    MessageType, send_encrypted_packet, receive_encrypted_packet, 
    KeyManager , UserAuthenticator
)

# Server configuration
HOST = '127.0.0.1'
PORT = 5050

KEY_FILE = '.battleship_key'  # File to store the server key

# Global key manager
key_manager = None

class WaitingPlayer:
    """Class to represent a player waiting to join a game"""
    def __init__(self, conn, addr):
        self.conn = conn
        self.addr = addr
        self.join_order = None
        self.username = None
        self.is_spectator = False
        self.seq_counter = 0  # For packet sequence numbering

def send(conn, msg, msg_type=MessageType.DEFAULT):
    """Send a message to a client using our custom protocol"""
    global seq_counters
    try:
        # Get the player's sequence counter if available
        seq_num = 0
        for player in list(waiting_queue.queue):
            if player.conn == conn:
                seq_num = player.seq_counter
                player.seq_counter = (player.seq_counter + 1) % 256
                break
        
        # Send the message with the protocol
        success = send_encrypted_packet(conn, msg_type, msg,key_manager.key, seq_num)
        return success
    except Exception as e:
        print(f"[WARNING] Error sending message: {e}")
        return False

# Global variables
join_counter = 0  # Initialize at module level
join_counter_lock = threading.Lock()
disconnected_players = {}  # username -> GameSession
game_running = False
active_game = None  # Reference to current game
waiting_queue = queue.Queue()
authenticator = UserAuthenticator()
try:
    authenticator.load_from_file()
except:
    pass

def initialize_encryption():
    """Initialize or load the encryption key"""
    global key_manager
    
    if os.path.exists(KEY_FILE):
        print(f"[INFO] Loading encryption key from {KEY_FILE}")
        key_manager = KeyManager.load_from_file(KEY_FILE)
        print(f"[INFO] Server loaded key: size = {len(key_manager.key)} bytes")
    else:
        print(f"[INFO] No key file found. Generating new secure encryption key")
        # Generate a random key - remove the hardcoded test key
        key_manager = KeyManager()  # This will create a random key
        key_manager.save_to_file(KEY_FILE)
        print(f"[INFO] Generated and saved new key to {KEY_FILE}")
    
    # For security, don't print the actual key to logs
    print(f"[INFO] Key size: {len(key_manager.key)} bytes")

def clear_persistent_state():
    """Clear any persistent state from previous server runs"""
    global disconnected_players
    disconnected_players = {}
    
    # Remove any state files if they exist
    try:
        if os.path.exists(".server_state"):
            os.remove(".server_state")
    except Exception as e:
        print(f"[WARNING] Could not clear persistent state: {e}")
        
def disconnect_timeout(username, opponent_conn):
    """Handle timeout for disconnected player"""
    print(f"[INFO] Starting disconnect timeout for {username}")
    
    # Get the session for this player
    session = disconnected_players.get(username)
    if session is None:
        print(f"[WARNING] No session found for {username}")
        return

    # Wait for reconnection event or timeout
    disconnected = not session.reconnect_events[username].wait(timeout=60)

    if disconnected:
        # Player didn't reconnect in time
        print(f"[INFO] {username} did not reconnect in time")
        if username in disconnected_players:
            del disconnected_players[username]
            # Notify opponent they won
            try:
                opponent_username = None
                if session.username1 == username:
                    opponent_username = session.username2
                else:
                    opponent_username = session.username1
                
                # Add MessageType.DEFAULT parameter here:
                send(opponent_conn, f"{username} did not reconnect in time. You win by forfeit.", MessageType.DEFAULT)
            except Exception as e:
                print(f"[WARNING] Could not notify opponent about forfeit: {e}")
    else:
        # Player reconnected in time
        print(f"[INFO] {username} reconnected in time, no forfeit.")

def update_waiting_queue_positions():
    """Update all waiting players with their queue position"""
    queue_list = list(waiting_queue.queue)
    queue_list.sort(key=lambda x: x.join_order)
    
    # Count of non-spectator players
    non_spectator_count = 0
    
    # Update each player's position
    for idx, player in enumerate(queue_list, 1):
        # Only count and update non-spectators
        if not player.is_spectator:
            non_spectator_count += 1
            try:
                send(player.conn, f"UPDATE: You are now number {non_spectator_count} in the waiting queue.\nPlease continue waiting for your match!")
            except Exception as e:
                print(f"[WARNING] Could not update player {player.addr}: {e}")

def broadcast_to_spectators(message):
    """Send a message to all spectators in the waiting queue"""
    queue_list = list(waiting_queue.queue)
    
    for player in queue_list:
        if player.is_spectator:
            try:
                send(player.conn, f"[SPECTATOR] {message}")
            except Exception as e:
                print(f"[WARNING] Could not send to spectator {player.username}: {e}")

def promote_spectator_to_player():
    """Find a spectator and promote them to a player, returning the player or None"""
    queue_list = list(waiting_queue.queue)
    
    for player in queue_list:
        if player.is_spectator:
            # Found a spectator, promote them
            player.is_spectator = False
            send(player.conn, "You've been promoted from spectator to player! Get ready to play!")
            print(f"[INFO] Promoted spectator {player.username} to player")
            
            # Give them a new join order for priority
            global join_counter
            with join_counter_lock:
                player.join_order = join_counter
                join_counter += 1
            
            return player
    
    # No spectators found
    return None

def get_spectator_count():
    """Return the number of spectators in the waiting queue"""
    queue_list = list(waiting_queue.queue)
    return sum(1 for p in queue_list if p.is_spectator)

def accept_clients(server_socket):
    """Thread function to accept client connections"""
    global join_counter, game_running
    
    while True:
        try:
            conn, addr = server_socket.accept()
            print(f"[INFO] New player connected from {addr}")
            
            # Create new player object
            new_player = WaitingPlayer(conn, addr)
            
            # Get username from the client using protocol
            try:
                is_valid, msg_type, payload = receive_encrypted_packet(conn,key_manager.key)
                
                if not is_valid:
                    print(f"[WARNING] Received corrupted packet from {addr}")
                    conn.close()
                    continue
                
                if payload and payload.startswith("USERNAME "):
                    auth_parts = payload[9:].split(' ', 1)
                    username = auth_parts[0]

                    is_authenticated = False
                    auth_method = ""
                    auth_token = None
                
                    if len(auth_parts) > 1:
                        auth_method = auth_parts[1]
                            
                        # Handle registration
                        if auth_method.startswith("REGISTER:"):
                            password = auth_method[9:]
                            
                            # Check if username already exists
                            if username in authenticator.registered_users:
                                # Username exists - reject registration attempt
                                send_encrypted_packet(conn, MessageType.DEFAULT, 
                                                "Registration failed: Username already exists", 
                                                key_manager.key, 0)
                                time.sleep(0.1)
                                try:
                                    conn.shutdown(socket.SHUT_RDWR)
                                except:
                                    pass
                                conn.close()
                                print(f"[INFO] Rejected registration attempt for existing username: {username}")
                                continue
                            else:
                                # New username - proceed with registration
                                auth_token = authenticator.register_user(username, password)
                                is_authenticated = True
                                send_encrypted_packet(conn, MessageType.DEFAULT, 
                                                f"Registration successful. Token: {auth_token}", 
                                                key_manager.key, 0)
                                authenticator.save_to_file()
                            
                        # Inside the section where auth_method is processed
                        elif auth_method.startswith("VERIFY:"):
                            # Handle combined password + token verification
                            parts = auth_method[7:].split(':', 1)
                            if len(parts) == 2:
                                password, token = parts
                                # First verify password
                                password_valid = authenticator.check_password(username, password)
                                token_valid = authenticator.verify_token(username, token)
                                    
                                if password_valid and token_valid:
                                    is_authenticated = True
                                    send_encrypted_packet(conn, MessageType.DEFAULT, 
                                                        "Authentication successful.", 
                                                        key_manager.key, 0)
                                else:
                                    send_encrypted_packet(conn, MessageType.DEFAULT, 
                                                    "Authentication failed: Invalid credentials", 
                                                    key_manager.key, 0)
                                    conn.close()
                                    continue
                                
                        # Handle token authentication
                        elif auth_method.startswith("TOKEN:"):
                            token = auth_method[6:]
                            is_authenticated = authenticator.verify_token(username, token)
                            if is_authenticated:
                                send_encrypted_packet(conn, MessageType.DEFAULT, 
                                                    f"Authentication successful.", 
                                                    key_manager.key, 0)
                            else:
                                send_encrypted_packet(conn, MessageType.DEFAULT, 
                                                    "Authentication failed: Invalid token", 
                                                    key_manager.key, 0)
                                conn.close()
                                continue
                        elif auth_method.startswith("PASSWORD:"):
                            password = auth_method[9:]
                            is_authenticated = authenticator.check_password(username, password)
                            if is_authenticated:
                                # If successful, generate a new token for future use
                                auth_token = authenticator.generate_token(username)
                                send_encrypted_packet(conn, MessageType.DEFAULT, 
                                            f"Authentication successful. Token: {auth_token}", 
                                            key_manager.key, 0)
                            else:
                                send_encrypted_packet(conn, MessageType.DEFAULT, 
                                            "Authentication failed: Invalid password", 
                                            key_manager.key, 0)
                                conn.close()
                                continue
                    else:
                        # No authentication provided
                        send_encrypted_packet(conn, MessageType.DEFAULT, 
                                        "Authentication required. Use 'USERNAME user REGISTER:password' or 'USERNAME user TOKEN:token'", 
                                        key_manager.key, 0)
                        conn.close()
                        continue

                    # Only proceed if authentication was successful
                    if is_authenticated:
                        new_player.username = username
                        print(f"[INFO] Player {username} authenticated successfully")
                        
                        # Check if this is a reconnecting player ONLY after successful authentication
                        if username in disconnected_players:
                            print(f"[INFO] {username} has reconnected!")
                            session = disconnected_players[username]
                            session.mark_reconnected(username, conn, conn)  # Using raw socket now
                            continue
                    else:
                        # Authentication failed (catch-all in case other checks missed it)
                        print(f"[WARNING] Authentication failed for user claiming to be {username}")
                        send_encrypted_packet(conn, MessageType.DEFAULT, 
                                          "Authentication failed: Invalid credentials", 
                                          key_manager.key, 0)
                        conn.close()
                        continue
                # Assign join order
                with join_counter_lock:
                    new_player.join_order = join_counter
                    join_counter += 1
                
                # Set spectator flag if a game is in progress
                if game_running:
                    new_player.is_spectator = True
                
                # Add to waiting queue
                waiting_queue.put(new_player)
                
                # Calculate position in queue (counting only non-spectators)
                position = 1
                for p in list(waiting_queue.queue):
                    if p.username == username:
                        break
                    if not p.is_spectator:
                        position += 1
                
                # Prepare welcome message
                if new_player.is_spectator:
                    message = f"Welcome to Battleship Multiplayer, {username}!\n"
                    message += "A game is currently in progress. You have joined as a spectator.\n"
                    
                    # If there's an active game, add the player names
                    if active_game:
                        player1 = active_game.get('player1_name', 'Player 1')
                        player2 = active_game.get('player2_name', 'Player 2')
                        message += f"You're watching a game between {player1} and {player2}.\n"
                        message += "Game updates will appear with [SPECTATOR] prefix.\n"
                else:
                    message = f"Welcome to Battleship Multiplayer, {username}!\n"
                    message += f"You are number {position} in the queue.\n"
                    message += "Please wait patiently - you will be matched soon!\n"
                
                # Send welcome message
                send(conn, message)
                
                # If player is a spectator, immediately send game status
                if new_player.is_spectator and active_game:
                    broadcast_to_spectators(f"New spectator {username} joined")
                
                # Show queue info
                queue_list = list(waiting_queue.queue)
                queue_list.sort(key=lambda x: x.join_order)
                
                for idx, p in enumerate(queue_list, 1):
                    spec_info = "(Spectator)" if p.is_spectator else ""
                    print(f"  {idx}. {p.username} {spec_info} (JoinOrder: {p.join_order})")
            
            except Exception as e:
                print(f"[ERROR] Error during client initialization: {e}")
                try:
                    conn.close()
                except:
                    pass
        
        except Exception as e:
            print(f"[ERROR] Error accepting connection: {e}")
            time.sleep(1)  # Avoid CPU spinning if accept fails

def cleanup_files():
    """Delete all temporary files created by the server"""
    print("[INFO] Cleaning up temporary files...")
    
    # Get a list of all files in the current directory
    files = [f for f in os.listdir('.') if os.path.isfile(f)]
    
    # Define patterns for files to delete
    patterns = [
        '.battleship_data_',   # User data files
        '.battleship_token_',  # Token files
        '.user_auth.dat',      # Authentication database
        '.server_state',       # Server state file
        '.battleship_key'      # Only delete if you want to reset encryption keys
    ]
    
    # Count deleted files
    deleted_count = 0
    
    # Delete matching files
    for file in files:
        if any(file.startswith(pattern) or file == pattern for pattern in patterns):
            try:
                os.remove(file)
                deleted_count += 1
                print(f"[INFO] Deleted: {file}")
            except Exception as e:
                print(f"[WARNING] Could not delete {file}: {e}")
    
    print(f"[INFO] Cleanup complete. Deleted {deleted_count} files.")

import signal
import sys

def signal_handler(sig, frame):
    print("\n[INFO] Shutdown signal received. Cleaning up...")
    cleanup_files()
    print("[INFO] Cleanup complete. Exiting.")
    sys.exit(0)

# Register the signal handler
signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C
signal.signal(signal.SIGTERM, signal_handler) 

def main():
    """Main server function"""
    global game_running, active_game, join_counter

    initialize_encryption()
    
    # Clear any persistent state
    clear_persistent_state()
    
    print(f"[INFO] Starting server on {HOST}:{PORT}")
    
    # Track priority player for next game
    priority_player = None
    
    try:
        # Create server socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, PORT))
        s.listen(5)  # Allow 5 pending connections
        print(f"[INFO] Server listening on {HOST}:{PORT}")
        
        # Start thread to accept client connections
        accept_thread = threading.Thread(target=accept_clients, args=(s,), daemon=True)
        accept_thread.start()
        
        # Main game management loop
        while True:
            # Skip if a game is already running
            if game_running:
                time.sleep(0.1)
                continue
            
            # Count non-spectator players in queue
            queue_list = list(waiting_queue.queue)
            non_spectator_count = sum(1 for p in queue_list if not p.is_spectator)
            spectator_count = get_spectator_count()
            
            # Handle priority player (from previous game) if available
            if priority_player:
                print(f"[INFO] Priority player {priority_player.username} exists, finding opponent")
                
                player1 = priority_player
                player2 = None
                
                # First check if we can promote a spectator to be player2
                if spectator_count > 0:
                    player2 = promote_spectator_to_player()
                    if player2:
                        waiting_queue.queue.remove(player2)
                        priority_player = None
                        print(f"[INFO] Promoted spectator {player2.username} to play against {player1.username}")
                
                # If no spectator was promoted, check regular waiting queue
                if player2 is None and waiting_queue.qsize() >= 1:
                    queue_list = list(waiting_queue.queue)
                    queue_list.sort(key=lambda x: x.join_order)
                    
                    for p in queue_list:
                        if not p.is_spectator:
                            player2 = p
                            waiting_queue.queue.remove(p)
                            priority_player = None
                            break
                
                if player2:
                    # We have both players, continue to game start
                    pass
                else:
                    # No eligible opponent found, wait
                    time.sleep(1)
                    continue
            
            # If no priority player, check if we have enough players (including spectators)
            elif non_spectator_count == 0 and spectator_count >= 2:
                # Promote two spectators to players
                print("[INFO] No regular players but 2+ spectators available, promoting two spectators")
                
                player1 = promote_spectator_to_player()
                waiting_queue.queue.remove(player1)
                
                player2 = promote_spectator_to_player()
                waiting_queue.queue.remove(player2)
                
                print(f"[INFO] Promoted spectators {player1.username} and {player2.username} to players")
            
            # If one regular player and at least one spectator
            elif non_spectator_count == 1 and spectator_count >= 1:
                print("[INFO] One regular player and 1+ spectators available, promoting one spectator")
                
                # Get the regular player
                queue_list = list(waiting_queue.queue)
                queue_list.sort(key=lambda x: x.join_order)
                
                for p in queue_list:
                    if not p.is_spectator:
                        player1 = p
                        waiting_queue.queue.remove(p)
                        break
                
                # Promote a spectator to be player2
                player2 = promote_spectator_to_player()
                waiting_queue.queue.remove(player2)
                
                print(f"[INFO] Matched regular player {player1.username} with promoted spectator {player2.username}")
            
            # If enough non-spectator players in queue, start a game
            elif non_spectator_count >= 2:
                print("[INFO] Two or more non-spectator players in queue, starting a new game")
                
                # Get first two non-spectator players
                players_selected = []
                queue_list = list(waiting_queue.queue)
                queue_list.sort(key=lambda x: x.join_order)
                
                for p in queue_list:
                    if not p.is_spectator and len(players_selected) < 2:
                        players_selected.append(p)
                        waiting_queue.queue.remove(p)
                
                player1 = players_selected[0]
                player2 = players_selected[1]
                
                print(f"[INFO] Selected {player1.username} and {player2.username} for game")
            
            else:
                # Not enough players to start a game
                time.sleep(1)
                continue
            
            # At this point, we have player1 and player2 and can start a game
            print(f"[INFO] Starting game between {player1.username} and {player2.username}")
            game_running = True
            
            # Set up active game info for spectators
            active_game = {
                'player1_name': player1.username,
                'player2_name': player2.username,
                'start_time': time.time()
            }
            
            # Notify spectators
            broadcast_to_spectators(f"Game starting between {player1.username} and {player2.username}")
            
            try:
                # NOTE: Will need to update run_multiplayer_game_online to use sockets directly
                # instead of file objects. For now, this still uses the old approach.
                # (You might need to modify battleship.py to handle the protocol)
                
                # Run the game
                result = run_multiplayer_game_online(
                    player1.conn, player1.conn,  # Using raw sockets now
                    player2.conn, player2.conn,  # Using raw sockets now
                    username1=player1.username,
                    username2=player2.username,
                    waiting_queue=waiting_queue,
                    disconnected_players=disconnected_players,
                    disconnect_timeout=disconnect_timeout,
                    key_manager_param=key_manager
                )
                
                print(f"[INFO] Game completed with result: {result}")
                broadcast_to_spectators(f"Game ended with result: {result}")
            
            except Exception as e:
                print(f"[ERROR] Game crashed: {e}")
                import traceback
                traceback.print_exc()
                
                try:
                    player1.conn.close()
                except:
                    pass
                
                try:
                    player2.conn.close()
                except:
                    pass
                
                result = None
            
            # The match is over, so nobody can reconnect to it any more. Forget both
            # players' reconnect records; otherwise logging in again with the same name
            # looks like a reconnection to this old match and the player never gets
            # back into the queue.
            disconnected_players.pop(player1.username, None)
            disconnected_players.pop(player2.username, None)

            # Reset active game
            active_game = None
            
            # Handle game result
            if result == "rematch":
                print(f"[INFO] Both {player1.username} and {player2.username} chose rematch")
                # Add players back to queue for next game (not as spectators)
                player1.is_spectator = False
                player2.is_spectator = False
                
                # Reset join order to give them priority
                with join_counter_lock:
                    player1.join_order = join_counter
                    join_counter += 1
                    player2.join_order = join_counter
                    join_counter += 1
                
                waiting_queue.put(player1)
                waiting_queue.put(player2)
                update_waiting_queue_positions()
                broadcast_to_spectators(f"Both players chose rematch - they will play again next")
            
            elif isinstance(result, tuple) and result[0] == "one_left":
                staying = result[1]
                if staying == 1:
                    print(f"[INFO] Player 1 ({player1.username}) stays, Player 2 ({player2.username}) leaves")
                    try:
                        send(player2.conn, "GAME_ENDED_DISCONNECTING", MessageType.DEFAULT)
                        time.sleep(0.5)
                        player2.conn.close()
                    except:
                        pass
                    
                    # Set returning player as priority
                    player1.is_spectator = False
                    priority_player = player1
                    broadcast_to_spectators(f"{player1.username} will play again in the next game")
                
                else:
                    print(f"[INFO] Player 2 ({player2.username}) stays, Player 1 ({player1.username}) leaves")
                    try:
                        send(player1.conn, "GAME_ENDED_DISCONNECTING", MessageType.DEFAULT)
                        time.sleep(0.5)
                        player1.conn.close()
                    except:
                        pass
                    
                    # Set returning player as priority
                    player2.is_spectator = False
                    priority_player = player2
                    broadcast_to_spectators(f"{player2.username} will play again in the next game")
                
                update_waiting_queue_positions()
            
            else:
                print(f"[INFO] Game finished. Closing connections for {player1.username} and {player2.username}")
                try:
                    # Send disconnection message before closing
                    send(player1.conn, "GAME_ENDED_DISCONNECTING", MessageType.DEFAULT)
                    time.sleep(0.5)  # Give the message time to be sent and processed
                    player1.conn.close()
                except Exception as e:
                    print(f"[WARNING] Error sending disconnect message to {player1.username}: {e}")
                
                try:
                    # Send disconnection message before closing
                    send(player2.conn, "GAME_ENDED_DISCONNECTING", MessageType.DEFAULT)
                    time.sleep(0.5)  # Give the message time to be sent and processed
                    player2.conn.close()
                except Exception as e:
                    print(f"[WARNING] Error sending disconnect message to {player2.username}: {e}")
            
            broadcast_to_spectators(f"Game ended, waiting for next match")
                    
            # Mark game as no longer running
            game_running = False
    
    except KeyboardInterrupt:
        print("\n[INFO] Server shutting down...")
    
    except Exception as e:
        print(f"[ERROR] Server error: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        # Clean up resources
        try:
            cleanup_files()
            s.close()
        except:
            pass
        
        print("[INFO] Server stopped")

if __name__ == "__main__":
    main()