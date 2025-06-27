"""
client.py

Encrypted version of the Battleship client.
Uses AES-256 encryption in CTR mode for secure communication.
"""
import threading
import socket
import os
import sys
import time
import getpass
import random
from Protocol import (
    MessageType, send_encrypted_packet, receive_encrypted_packet, 
    KeyManager
)

# Configuration - must match the server
HOST = '127.0.0.1'
PORT = 5050
KEY_FILE = '.battleship_key'  # File to store the shared key

# Global flag to control the client threads
running = True
# Global counter for packet sequence numbers
seq_counter = 0
# Global key manager
key_manager = None

def read_msg(sock):
    """Continuously read encrypted messages from the server and print them"""
    global running, key_manager
    
    try:
        while running:
            try:
                is_valid, msg_type, payload = receive_encrypted_packet(sock, key_manager.key)
                
                if not is_valid:
                    print("[WARNING] Received corrupted packet")
                    continue
                
                if payload is None:
                    print("[INFO] Server disconnected.")
                    running = False
                    break
                    
                if msg_type == MessageType.DEFAULT:
                    # Check for disconnect message
                    if payload == "GAME_ENDED_DISCONNECTING":
                        print("[INFO] Game ended. Disconnecting...")
                        running = False
                        break
                        
                    # Handle normal DEFAULT messages
                    print(payload)
                    
                    # Special handling for spectator messages
                    #if payload.startswith("[SPECTATOR]"):
                        #print(f"\n[SPECTATOR UPDATE] {payload[11:].strip()}")
                
                elif msg_type == MessageType.RESULT:
                    # Handle game result messages (hit, miss, etc.)
                    print(f"[RESULT] {payload}")
                
                elif msg_type == MessageType.PLACE:
                    # Handle placement confirmation
                    print(f"[PLACEMENT] {payload}")
                
                elif msg_type == MessageType.FIRE:
                    # Handle fire confirmation or prompt
                    print(f"[FIRE] {payload}")
                    
                elif msg_type == MessageType.GRID:
                    # Handle grid display
                    board_type = payload.split('\n')[0] if '\n' in payload else "GRID"
                    if board_type == "GRID":
                        print("\n[Board]")
                    elif board_type in ["YOUR BOARD", "ENEMY BOARD"]:
                        print(f"\n[{board_type}]")
                    
                    # Print the board
                    print(payload)
                
            except Exception as e:
                print(f"[ERROR] Error reading message: {e}")
                if running:  # Only break if we're still supposed to be running
                    break
    
    except Exception as e:
        if running:  # Only show error if not deliberately stopped
            print(f"[ERROR] Connection error in read_msg: {e}")
        running = False

def load_username():
    """Load previously used username from file for reconnection"""
    username_file = '.battleship_username'
    try:
        if os.path.exists(username_file):
            with open(username_file, 'r') as f:
                saved_username = f.read().strip()
                if saved_username:
                    return saved_username
    except Exception as e:
        print(f"[WARNING] Could not load saved username: {e}")
    
    return None

def register_or_login():
    """Handle user registration or login"""
    # Always ask for username
    username = input("Username: ").strip()
    
    if not username:
        # Generate a random username if empty
        username = f"Player{random.randint(1000, 9999)}"
        print(f"[INFO] Using auto-generated username: {username}")
    
    # Check if we have a token for this username
    saved_username = load_username()
    saved_token = load_token(username)
    
    if username == saved_username and saved_token:
        print("[INFO] Found saved authentication token.")
        use_token = input("Use saved authentication? (Y/n): ").lower() != 'n'
        
        if use_token:
            password = getpass.getpass("Verify your identity. Enter password: ")
            return username, f"VERIFY:{password}:{saved_token}"
    
    # At this point, either we don't have a token, or the user declined to use it
    # Ask if the user wants to register or login
    choice = input("(R)egister new account or (L)ogin? [R/L]: ").upper()
    
    if choice == 'R':
        # Registration
        password = getpass.getpass("Create password: ")
        confirm = getpass.getpass("Confirm password: ")
        
        if password != confirm:
            print("[ERROR] Passwords don't match!")
            return register_or_login()  # Try again
            
        save_username(username)  # Save username for future reference
        return username, f"REGISTER:{password}"
    else:
        # Login
        password = getpass.getpass("Enter password: ")
        save_username(username)  # Save username for future reference
        return username, f"PASSWORD:{password}"
    
def save_username(username):
    """Save username to user-specific file"""
    username_file = f'.battleship_data_{username}.txt'
    try:
        with open(username_file, 'w') as f:
            f.write(username)
    except Exception as e:
        print(f"[WARNING] Could not save username: {e}")

def save_token(username, token):
    """Save token to user-specific file"""
    token_file = f'.battleship_token_{username}.txt'
    try:
        with open(token_file, 'w') as f:
            f.write(token)
    except Exception as e:
        print(f"[WARNING] Could not save token: {e}")

def load_token(username):
    """Load token for specific username"""
    token_file = f'.battleship_token_{username}.txt'
    try:
        if os.path.exists(token_file):
            with open(token_file, 'r') as f:
                return f.read().strip()
    except Exception as e:
        print(f"[WARNING] Could not load token: {e}")
    return None

def main():
    global running, seq_counter, key_manager
    
    print(f"[INFO] Connecting to Battleship server at {HOST}:{PORT}...")
    
    # Initialize or load encryption key
    if os.path.exists(KEY_FILE):
        print(f"[INFO] Loading encryption key from {KEY_FILE}")
        key_manager = KeyManager.load_from_file(KEY_FILE)
        print(f"[INFO] Client loaded key: size = {len(key_manager.key)} bytes")
    else:
        print(f"[WARNING] No key file found at {KEY_FILE}")
        print(f"[INFO] For a real implementation, you would need to obtain")
        print(f"       the key from the server through a secure channel.")
        print(f"[INFO] Generating a random key for testing only - this won't work")
        print(f"       unless the server happens to have the same key.")
        key_manager = KeyManager()  # Random key - won't match server
        key_manager.save_to_file(KEY_FILE)
    
    try:
        # Create socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((HOST, PORT))
        
        # Handle username (with reconnection support)
        #saved_username = load_username()
        username, auth_method = register_or_login()
        # Save username for future reconnection
        save_username(username)
        
        # Send username to server using encrypted protocol
        send_encrypted_packet(s, MessageType.DEFAULT, f"USERNAME {username} {auth_method}", key_manager.key, seq_counter)
        seq_counter = (seq_counter + 1) % 256
        
        # Wait for authentication response
        is_valid, msg_type, payload = receive_encrypted_packet(s, key_manager.key)
        
        if not is_valid or not payload:
            print("[ERROR] Failed to receive authentication response from server")
            return
            
        if "Token:" in payload:
            # Extract and save authentication token
            token = payload.split("Token:")[1].strip()
            save_token(username ,token)
            print("[INFO] Successfully authenticated!")
        elif "Authentication failed" in payload:
            print(f"[ERROR] {payload}")
            return
        elif "Authentication successful" in payload:
            print("[INFO] Successfully authenticated with saved token!")
        
        # Wait a moment to ensure the server processes the username
        time.sleep(0.5)
        
        # Start thread to read server messages
        thread = threading.Thread(target=read_msg, args=(s,), daemon=True)
        thread.start()
        
        # Main loop for user input
        print("[INFO] You are now connected. Type your messages below.")
        print("[INFO] Type '/quit' to exit.")
        
        try:
            while running:
                user_input = input(">> ")
                
                # Check for local commands
                if user_input.lower() in ["/quit", "/exit"]:
                    print("[INFO] Closing connection...")
                    running = False
                    break
                
                # Determine message type based on command
                if user_input.startswith("FIRE "):
                    msg_type = MessageType.FIRE
                elif user_input.startswith("PLACE "):
                    msg_type = MessageType.PLACE
                else:
                    msg_type = MessageType.DEFAULT
                
                # Send input to server using encrypted protocol
                success = send_encrypted_packet(s, msg_type, user_input, key_manager.key, seq_counter)
                if success:
                    seq_counter = (seq_counter + 1) % 256
                else:
                    print("[ERROR] Failed to send message")
        
        except KeyboardInterrupt:
            print("\n[INFO] Client exiting.")
            running = False
        
        except Exception as e:
            print(f"[ERROR] Error in main input loop: {e}")
            running = False
        
    except ConnectionRefusedError:
        print(f"[ERROR] Connection refused. Is the server running at {HOST}:{PORT}?")
        print("[TIP] Make sure you run the server (python server.py) before running the client.")
    
    except Exception as e:
        print(f"[ERROR] Connection error: {e}")
        import traceback
        traceback.print_exc()
    
    # Clean up
    try:
        s.close()
    except:
        pass
    
    print("[INFO] Connection closed. Run the client again to reconnect.")

if __name__ == "__main__":
    main()