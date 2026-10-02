"""
battleship.py

Contains core data structures and logic for Battleship, including:
 - Board class for storing ship positions, hits, misses
 - Utility function parse_coordinate for translating e.g. 'B5' -> (row, col)
 - A test harness run_single_player_game() to demonstrate the logic in a local, single-player mode

"""

import socket
import random
import time
import select
from Protocol import MessageType, send_encrypted_packet, receive_encrypted_packet , check_connection


BOARD_SIZE = 10

SHIPS = [
    ("Carrier", 5),
    ("Battleship", 4),
    ("Cruiser", 3),
    ("Submarine", 3),
    ("Destroyer", 2)
]


class Board:
    """
    Represents a single Battleship board with hidden ships.
    We store:
      - self.hidden_grid: tracks real positions of ships ('S'), hits ('X'), misses ('o')
      - self.display_grid: the version we show to the player ('.' for unknown, 'X' for hits, 'o' for misses)
      - self.placed_ships: a list of dicts, each dict with:
          {
             'name': <ship_name>,
             'positions': set of (r, c),
          }
        used to determine when a specific ship has been fully sunk.

    In a full 2-player networked game:
      - Each player has their own Board instance.
      - When a player fires at their opponent, the server calls
        opponent_board.fire_at(...) and sends back the result.
    """

    def __init__(self, size=BOARD_SIZE):
        self.size = size
        # '.' for empty water
        self.hidden_grid = [['.' for _ in range(size)] for _ in range(size)]
        # display_grid is what the player or an observer sees (no 'S')
        self.display_grid = [['.' for _ in range(size)] for _ in range(size)]
        self.placed_ships = []  # e.g. [{'name': 'Destroyer', 'positions': {(r, c), ...}}, ...]

    def place_ships_randomly(self, ships=SHIPS):
        """
        Randomly place each ship in 'ships' on the hidden_grid, storing positions for each ship.
        In a networked version, you might parse explicit placements from a player's commands
        (e.g. "PLACE A1 H BATTLESHIP") or prompt the user for board coordinates and placement orientations; 
        the self.place_ships_manually() can be used as a guide.
        """
        for ship_name, ship_size in ships:
            placed = False
            while not placed:
                orientation = random.randint(0, 1)  # 0 => horizontal, 1 => vertical
                print("orientation " ,orientation)
                row = random.randint(0, self.size - 1)
                col = random.randint(0, self.size - 1)

                if self.can_place_ship(row, col, ship_size, orientation):
                    occupied_positions = self.do_place_ship(row, col, ship_size, orientation)
                    print("occupied_positions", occupied_positions)
                    self.placed_ships.append({
                        'name': ship_name,
                        'positions': occupied_positions
                    })
                    placed = True
        #print(self.placed_ships)
        print("Ships placed:")
        for ship in self.placed_ships:
            print(f"{ship['name']}: {ship['positions']}")



    def place_ships_manually(self, ships=SHIPS):
        """
        Prompt the user for each ship's starting coordinate and orientation (H or V).
        Validates the placement; if invalid, re-prompts.
        """
        print("\nPlease place your ships manually on the board.")
        for ship_name, ship_size in ships:
            while True:
                self.print_display_grid(show_hidden_board=True)
                print(f"\nPlacing your {ship_name} (size {ship_size}).")
                coord_str = input("  Enter starting coordinate (e.g. A1): ").strip()
                orientation_str = input("  Orientation? Enter 'H' (horizontal) or 'V' (vertical): ").strip().upper()

                try:
                    row, col = parse_coordinate(coord_str)
                except ValueError as e:
                    print(f"  [!] Invalid coordinate: {e}")
                    continue

                # Convert orientation_str to 0 (horizontal) or 1 (vertical)
                if orientation_str == 'H':
                    orientation = 0
                elif orientation_str == 'V':
                    orientation = 1
                else:
                    print("  [!] Invalid orientation. Please enter 'H' or 'V'.")
                    continue

                # Check if we can place the ship
                if self.can_place_ship(row, col, ship_size, orientation):
                    occupied_positions = self.do_place_ship(row, col, ship_size, orientation)
                    self.placed_ships.append({
                        'name': ship_name,
                        'positions': occupied_positions
                    })
                    break
                else:
                    print(f"  [!] Cannot place {ship_name} at {coord_str} (orientation={orientation_str}). Try again.")


    def can_place_ship(self, row, col, ship_size, orientation):
        """
        Check if we can place a ship of length 'ship_size' at (row, col)
        with the given orientation (0 => horizontal, 1 => vertical).
        Returns True if the space is free, False otherwise.
        """
        if orientation == 0:  # Horizontal
            if col + ship_size > self.size:
                return False
            for c in range(col, col + ship_size):
                if self.hidden_grid[row][c] != '.':
                    return False
        else:  # Vertical
            if row + ship_size > self.size:
                return False
            for r in range(row, row + ship_size):
                if self.hidden_grid[r][col] != '.':
                    return False
        return True

    def do_place_ship(self, row, col, ship_size, orientation):
        """
        Place the ship on hidden_grid by marking 'S', and return the set of occupied positions.
        """
        occupied = set()
        if orientation == 0:  # Horizontal
            for c in range(col, col + ship_size):
                self.hidden_grid[row][c] = 'S'
                occupied.add((row, c))
        else:  # Vertical
            for r in range(row, row + ship_size):
                self.hidden_grid[r][col] = 'S'
                occupied.add((r, col))
        return occupied

    def fire_at(self, row, col):
        """
        Fire at (row, col). Return a tuple (result, sunk_ship_name).
        Possible outcomes:
          - ('hit', None)          if it's a hit but not sunk
          - ('hit', <ship_name>)   if that shot causes the entire ship to sink
          - ('miss', None)         if no ship was there
          - ('already_shot', None) if that cell was already revealed as 'X' or 'o'

        The server can use this result to inform the firing player.
        """
        cell = self.hidden_grid[row][col]
        if cell == 'S':
            # Mark a hit
            self.hidden_grid[row][col] = 'X'
            self.display_grid[row][col] = 'X'
            # Check if that hit sank a ship
            sunk_ship_name = self._mark_hit_and_check_sunk(row, col)
            if sunk_ship_name:
                return ('hit', sunk_ship_name)  # A ship has just been sunk
            else:
                return ('hit', None)
        elif cell == '.':
            # Mark a miss
            self.hidden_grid[row][col] = 'o'
            self.display_grid[row][col] = 'o'
            return ('miss', None)
        elif cell == 'X' or cell == 'o':
            return ('already_shot', None)
        else:
            # In principle, this branch shouldn't happen if 'S', '.', 'X', 'o' are all possibilities
            return ('already_shot', None)

    def _mark_hit_and_check_sunk(self, row, col):
        """
        Remove (row, col) from the relevant ship's positions.
        If that ship's positions become empty, return the ship name (it's sunk).
        Otherwise return None.
        """
        for ship in self.placed_ships:
            if (row, col) in ship['positions']:
                ship['positions'].remove((row, col))
                if len(ship['positions']) == 0:
                    return ship['name']
                break
        return None

    def all_ships_sunk(self):
        """
        Check if all ships are sunk (i.e. every ship's positions are empty).
        """
        for ship in self.placed_ships:
            if len(ship['positions']) > 0:
                return False
        return True

    def print_display_grid(self, show_hidden_board=False):
        """
        Print the board as a 2D grid.
        
        If show_hidden_board is False (default), it prints the 'attacker' or 'observer' view:
        - '.' for unknown cells,
        - 'X' for known hits,
        - 'o' for known misses.
        
        If show_hidden_board is True, it prints the entire hidden grid:
        - 'S' for ships,
        - 'X' for hits,
        - 'o' for misses,
        - '.' for empty water.
        """
        # Decide which grid to print
        grid_to_print = self.hidden_grid if show_hidden_board else self.display_grid

        # Column headers (1 .. N)
        print("  " + "".join(str(i + 1).rjust(2) for i in range(self.size)))
        # Each row labeled with A, B, C, ...
        for r in range(self.size):
            row_label = chr(ord('A') + r)
            row_str = " ".join(grid_to_print[r][c] for c in range(self.size))
            print(f"{row_label:2} {row_str}")


def parse_coordinate(coord_str):
    """
    Convert something like 'B5' into zero-based (row, col).
    Example: 'A1' => (0, 0), 'C10' => (2, 9)
    HINT: you might want to add additional input validation here...
    """
    coord_str = coord_str.strip().upper()
    row_letter = coord_str[0]
    col_digits = coord_str[1:]

    row = ord(row_letter) - ord('A')
    col = int(col_digits) - 1  # zero-based

    return (row, col)


def run_single_player_game_locally():
    """
    A test harness for local single-player mode, demonstrating two approaches:
     1) place_ships_manually()
     2) place_ships_randomly()

    Then the player tries to sink them by firing coordinates.
    """
    board = Board(BOARD_SIZE)

    # Ask user how they'd like to place ships
    choice = input("Place ships manually (M) or randomly (R)? [M/R]: ").strip().upper()
    if choice == 'M':
        board.place_ships_manually(SHIPS)
    else:
        board.place_ships_randomly(SHIPS)

    print("\nNow try to sink all the ships!")
    moves = 0
    while True:
        board.print_display_grid()
        guess = input("\nEnter coordinate to fire at (or 'quit'): ").strip()
        if guess.lower() == 'quit':
            print("Thanks for playing. Exiting...")
            return

        try:
            row, col = parse_coordinate(guess)
            result, sunk_name = board.fire_at(row, col)
            moves += 1

            if result == 'hit':
                if sunk_name:
                    print(f"  >> HIT! You sank the {sunk_name}!")
                else:
                    print("  >> HIT!")
                if board.all_ships_sunk():
                    board.print_display_grid()
                    print(f"\nCongratulations! You sank all ships in {moves} moves.")
                    break
            elif result == 'miss':
                print("  >> MISS!")
            elif result == 'already_shot':
                print("  >> You've already fired at that location. Try again.")

        except ValueError as e:
            print("  >> Invalid input:", e)


def run_single_player_game_online(rfile, wfile):
    """
    A test harness for running the single-player game with I/O redirected to socket file objects.
    Expects:
      - rfile: file-like object to .readline() from client
      - wfile: file-like object to .write() back to client
    
    #####
    NOTE: This function is (intentionally) currently somewhat "broken", which will be evident if you try and play the game via server/client.
    You can use this as a starting point, or write your own.
    #####
    """
    def send(msg):
        
            wfile.write(msg + '\n')
            wfile.flush()
        
    def send_board(board):
            wfile.write("GRID\n")
            wfile.write("  " + " ".join(str(i + 1).rjust(2) for i in range(board.size)) + '\n')
            for r in range(board.size):
                row_label = chr(ord('A') + r)
                row_str = " ".join(board.display_grid[r][c] for c in range(board.size))
                wfile.write(f"{row_label:2} {row_str}\n")
            wfile.write('\n')
            wfile.flush()
    

    def recv():
        return rfile.readline().strip()

    board = Board(BOARD_SIZE)
    board.place_ships_randomly(SHIPS)

    send("Welcome to Online Single-Player Battleship! Try to sink all the ships. Type 'quit' to exit.")

    moves = 0
    while True:
        send_board(board)
        send("Enter coordinate to fire at (e.g. B5):")
        guess = recv()
        if guess.lower() == 'quit':
            send("Thanks for playing. Goodbye.")
            return

        try:
            row, col = parse_coordinate(guess)
            result, sunk_name = board.fire_at(row, col)
            moves += 1

            if result == 'hit':
                if sunk_name:
                    send(f"HIT! You sank the {sunk_name}!")
                else:
                    send("HIT!")
                if board.all_ships_sunk():
                    send_board(board)
                    send(f"Congratulations! You sank all ships in {moves} moves.")
                    return
            elif result == 'miss':
                send("MISS!")
            elif result == 'already_shot':
                send("You've already fired at that location.")
        except ValueError as e:
            send(f"Invalid input: {e}")

"""
battleship.py

Contains core data structures and logic for Battleship, including:
 - Board class for storing ship positions, hits, misses
 - Utility function parse_coordinate for translating e.g. 'B5' -> (row, col)
 - A test harness run_single_player_game() to demonstrate the logic in a local, single-player mode
 - GameSession class for managing multiplayer games with reconnection support
 - run_multiplayer_game_online function for online multiplayer

NOTE: Keep all your existing code and just add the new GameSession class
and run_multiplayer_game_online function at the bottom of the file.
"""

import random
import time
import select
import threading
key_manager = None

# Keep all your existing Board class, parse_coordinate function, etc.
# Just add these new parts at the end of your file:

class GameSession:
    """
    Represents an active game session between two players.
    Used for tracking game state and handling reconnections.
    """
    def __init__(self, username1, username2, board1, board2, current_player, timeouts):
        self.username1 = username1
        self.username2 = username2
        self.board1 = board1
        self.board2 = board2
        self.current_player = current_player
        self.timeouts = timeouts
        # Create events for tracking reconnections
        self.reconnect_events = {
            username1: threading.Event(),
            username2: threading.Event(),
        }
        # Store reconnected sockets
        self.reconnected_sockets = {}
        # print(f"[DEBUG] Created GameSession for {username1} and {username2}")
        self.auth_tokens = {
            username1: None,
            username2: None
        }
        
        # Flag to indicate if a reconnection is in progress
        self.reconnection_in_progress = {
            username1: False,
            username2: False
        }

    def set_auth_token(self, username, token):
        """Store the authentication token for a player"""
        if username in self.auth_tokens:
            self.auth_tokens[username] = token
            
    def verify_auth_token(self, username, token):
        """Verify the authentication token for reconnection"""
        return username in self.auth_tokens and self.auth_tokens[username] == token

    def mark_reconnected(self, username, sock, _):
        """
        Mark a player as reconnected and store their new socket.
        
        Args:
            username (str): Username of reconnected player
            sock (socket): New socket connection
            _: Unused parameter for backward compatibility
        """
        # print(f"[DEBUG] Marking {username} as reconnected in GameSession")
        
        # Store the socket and set the event
        self.reconnected_sockets[username] = sock
        self.reconnect_events[username].set()
        
        # Clear the reconnection in progress flag
        if username in self.reconnection_in_progress:
            self.reconnection_in_progress[username] = False
            
        # print(f"[DEBUG] Set reconnection event for {username}")

    def wait_for_reconnect(self, username, timeout=60):
        """
        Wait for a player to reconnect, with timeout.
        
        Args:
            username (str): Username to wait for
            timeout (int): Timeout in seconds
            
        Returns:
            tuple: (sock, sock) if reconnected, (None, None) on timeout
        """
        # print(f"[DEBUG] Waiting for {username} to reconnect with {timeout}s timeout")
        
        # Mark reconnection in progress
        if username in self.reconnection_in_progress:
            self.reconnection_in_progress[username] = True
        
        # Check if we have an event for this username
        if username not in self.reconnect_events:
            print(f"[ERROR] No reconnect event for {username} in GameSession")
            return None, None

        event = self.reconnect_events[username]

        # The player may already have logged back in before the game noticed they had
        # gone (for example they dropped out during the opponent's turn). Use that
        # connection instead of throwing it away. The event is left set on purpose so
        # the disconnect_timeout watcher in server.py also sees that they are back.
        early_sock = self.reconnected_sockets.pop(username, None) if event.is_set() else None
        if early_sock is not None and check_connection(early_sock):
            if username in self.reconnection_in_progress:
                self.reconnection_in_progress[username] = False
            return early_sock, early_sock

        # Reset the event in case it was set previously
        event.clear()
        
        # Wait for the event with timeout
        success = event.wait(timeout)
        
        if success:
            # Check if we have a socket
            if username not in self.reconnected_sockets:
                print(f"[ERROR] Event set but no socket for {username}")
                return None, None
                
            sock = self.reconnected_sockets.pop(username)
            # print(f"[DEBUG] {username} reconnected successfully with socket {sock}")
            return sock, sock  # Return socket twice to match old interface
            
        # Timeout occurred
        # print(f"[DEBUG] {username} did not reconnect within timeout")
        
        # Clear the reconnection in progress flag
        if username in self.reconnection_in_progress:
            self.reconnection_in_progress[username] = False
            
        return None, None
        
    def is_reconnecting(self, username):
        """
        Check if a player is in the process of reconnecting.
        
        Args:
            username (str): Username to check
            
        Returns:
            bool: True if reconnection in progress, False otherwise
        """
        return self.reconnection_in_progress.get(username, False)
    
def run_multiplayer_game_online(sock1, sock1_unused, sock2, sock2_unused, username1, username2,
                               waiting_queue=None, disconnected_players=None, disconnect_timeout=None,key_manager_param=None):
    """
    Run a multiplayer Battleship game between two players using the custom protocol.
    Supports spectators, reconnection, and transitioning to next match.
    
    Parameters:
    - sock1, _: Socket for player 1 (second parameter is unused, kept for compatibility)
    - sock2, _: Socket for player 2 (second parameter is unused, kept for compatibility)
    - username1, username2: Names of the players
    - waiting_queue: Queue of waiting players (used for spectators)
    - disconnected_players: Dict mapping usernames to game sessions
    - disconnect_timeout: Function to handle player disconnect timeouts
    
    Returns:
    - "quit": Both players quit or game ended
    - "rematch": Both players want to play again
    - ("one_left", player_num): Only one player wants to continue
    """
    global key_manager
    if key_manager_param is not None:
        key_manager = key_manager_param
    
    # Check if key_manager is still None - if so, create a new one with a fixed key
    if key_manager is None:
        print("[WARNING] key_manager is None, creating a new one")
        from Protocol import KeyManager
        key_manager = KeyManager() 
    # Keep track of sequence numbers for each player

    seq_counters = {username1: 0, username2: 0}

    
    
    
    def send_msg(sock, msg, username, msg_type=MessageType.DEFAULT):
        """Send a message to a player using the protocol"""
        global key_manager
        try:
            if key_manager is None:
                print("[WARNING] key_manager is None in send_msg, creating a default one")
                from Protocol import KeyManager
                key_manager = KeyManager()

            seq_num = seq_counters.get(username, 0)
            success = send_encrypted_packet(sock, msg_type, msg,key_manager.key, seq_num)
            if success:
                seq_counters[username] = (seq_num + 1) % 256
            return success
        except Exception as e:
            print(f"[ERROR] Failed to send message to {username}: {e}")
            raise ConnectionError("Failed to send message")

    def send_board(sock, username, board, board_type="GRID"):
        """Send board state to a player using the protocol"""
        board_text = f"{board_type}\n"
        board_text += "  " + " ".join(str(i + 1).rjust(2) for i in range(board.size)) + '\n'
        for r in range(board.size):
            row_label = chr(ord('A') + r)
            if board_type == "YOUR BOARD":
                row_str = " ".join(board.hidden_grid[r][c] for c in range(board.size))
            else:
                row_str = " ".join(board.display_grid[r][c] for c in range(board.size))
            board_text += f"{row_label:2} {row_str}\n"
        
        # Send the board using the protocol
        send_msg(sock, board_text, username, MessageType.GRID)

    def broadcast_to_spectators(message):
        """Send a message to all spectators (players in waiting queue)"""
        if waiting_queue:
            queue_list = list(waiting_queue.queue)
            for waiting_player in queue_list:
                # Only send to players marked as spectators
                if hasattr(waiting_player, 'is_spectator') and waiting_player.is_spectator:
                    try:
                        send_msg(waiting_player.conn, "[SPECTATOR] " + message, 
                                waiting_player.username, MessageType.DEFAULT)
                        
                        # Send board updates for important game events
                        if (message.endswith("'s turn") or 
                            "fired at" in message or 
                            "hit" in message.lower() or 
                            "miss" in message.lower() or
                            "sank" in message.lower()):
                            
                            try:
                                send_msg(waiting_player.conn, "[SPECTATOR] PLAYER 1's view:", 
                                       waiting_player.username, MessageType.DEFAULT)
                                send_board(waiting_player.conn, waiting_player.username, 
                                         players[2]['board'], "GRID")
                                
                                send_msg(waiting_player.conn, "[SPECTATOR] PLAYER 2's view:", 
                                       waiting_player.username, MessageType.DEFAULT)
                                send_board(waiting_player.conn, waiting_player.username, 
                                         players[1]['board'], "GRID")
                            except Exception:
                                # Continue if board sending fails
                                pass
                    except Exception:
                        # Silently handle disconnected spectators
                        continue

    def recv_msg(sock, username="Unknown", timeout=60):
        """
        Receive input from a player with timeout and improved error handling.
        
        Args:
            sock (socket): Socket to receive from
            username (str): Username of player (for logging)
            timeout (int): Timeout in seconds
            
        Returns:
            str: Message received, None on timeout, or "CORRUPTED_PACKET" on error
        """
        # print(f"[DEBUG] Waiting for message from {username} with {timeout}s timeout")
        
        try:
            # First check if connection is alive
            if not check_connection(sock):
                print(f"[WARNING] Connection check failed for {username}")
                raise ConnectionError(f"Player {username} disconnected")
                
            # Use select for high-level timeout without modifying socket timeout
            import select
            ready, _, _ = select.select([sock], [], [], timeout)
            
            if not ready:
                print(f"[INFO] Timeout waiting for message from {username}")
                return None  # Timeout
                
            # Try to receive the packet with sanitization
            try:
                is_valid, msg_type, payload = receive_encrypted_packet(sock,key_manager.key)
                
                if not is_valid:
                    print(f"[WARNING] Received corrupted packet from {username}")
                    return "CORRUPTED_PACKET"
                    
                # Sanitize payload - strip whitespace and control characters
                if payload:
                    # Remove control characters but keep newlines for board data
                    payload = ''.join(c for c in payload if c == '\n' or (c.isprintable() and c != '\r'))
                    # Limit length to prevent abuse
                    payload = payload[:4096]
                    
                # print(f"[DEBUG] Received '{payload}' from {username}")
                return payload
                
            except socket.timeout:
                print(f"[WARNING] Socket timeout receiving from {username}")
                return "SOCKET_TIMEOUT"  # Special case for socket timeout
                
        except ConnectionError:
            print(f"[ERROR] Connection lost with {username}")
            raise ConnectionError(f"Player {username} disconnected")
            
        except Exception as e:
            print(f"[ERROR] Unexpected error in recv_msg from {username}: {e}")
            import traceback
            traceback.print_exc()
            # Don't raise here to avoid game crash
            return "CORRUPTED_PACKET"

    def ask_play_again(sock, username, label):
        """Ask a player if they want to play again"""
        time.sleep(2)
        send_msg(sock, f"{label}, would you like to play again? (Y/N)", username)
        try:
            response = recv_msg(sock, timeout=30)
            return response.lower() if response else 'n'
        except Exception:
            return 'n'

    def send_dual_boards(sock, username, your_board, enemy_board):
        """Send both boards to a player (your board with ships, enemy board with hits/misses)"""
        # First send YOUR BOARD
        your_board_text = "YOUR BOARD\n"
        your_board_text += "  " + " ".join(str(i + 1).rjust(2) for i in range(your_board.size)) + '\n'
        for r in range(your_board.size):
            row_label = chr(ord('A') + r)
            row_str = " ".join(your_board.hidden_grid[r][c] for c in range(your_board.size))
            your_board_text += f"{row_label:2} {row_str}\n"
        
        # Then send ENEMY BOARD
        enemy_board_text = "ENEMY BOARD\n"
        enemy_board_text += "  " + " ".join(str(i + 1).rjust(2) for i in range(enemy_board.size)) + '\n'
        for r in range(enemy_board.size):
            row_label = chr(ord('A') + r)
            row_str = " ".join(enemy_board.display_grid[r][c] for c in range(enemy_board.size))
            enemy_board_text += f"{row_label:2} {row_str}\n"
        
        # Send both boards using the protocol
        send_msg(sock, your_board_text, username, MessageType.GRID)
        send_msg(sock, enemy_board_text, username, MessageType.GRID)

    # Initialize game boards
    board1 = Board(BOARD_SIZE)
    board2 = Board(BOARD_SIZE)

    board1.place_ships_randomly(SHIPS)
    board2.place_ships_randomly(SHIPS)

    # Store board references in active_game (for spectators)
    try:
        # Get the active_game global from the server
        import builtins
        if hasattr(builtins, 'active_game') and builtins.active_game is not None:
            builtins.active_game['board1'] = board1
            builtins.active_game['board2'] = board2
    except Exception:
        # Silently ignore if this fails
        pass

    send_msg(sock1, f"Welcome {username1}! You are playing against {username2}.", username1)
    send_msg(sock2, f"Welcome {username2}! You are playing against {username1}.", username2)

    # Set up player data
    players = {
        1: {'sock': sock1, 'board': board1, 'username': username1},
        2: {'sock': sock2, 'board': board2, 'username': username2}
    }

    # Track timeouts and current player
    timeouts = {1: 0, 2: 0}
    current_player = 1
    
    # Create game session for reconnection support
    if disconnected_players is not None:
        session = GameSession(username1, username2, board1, board2, current_player, timeouts)
        disconnected_players[username1] = session
        disconnected_players[username2] = session

    # Announce game to spectators
    broadcast_to_spectators(f"New game started: {username1} vs {username2}")

    try:
        while True:
            player = players[current_player]
            opponent = players[2 if current_player == 1 else 1]

            sock = player['sock']
            username = player['username']
            opponent_username = opponent['username']

            # ENHANCED DISCONNECTION HANDLING: Check both players' connection at start of turn
            
            # Check current player connection
            player_connected = check_connection(player['sock'])
            # Check opponent connection
            opponent_connected = check_connection(opponent['sock'])
            
            # Handle case where both players are disconnected
            if not player_connected and not opponent_connected:
                print(f"[INFO] Both players {username} and {opponent_username} are disconnected")
                
                # Create/ensure sessions exist for both players
                for p_username, p_sock in [(username, sock), (opponent_username, opponent['sock'])]:
                    if p_username not in disconnected_players:
                        print(f"[INFO] Creating reconnection session for {p_username}")
                        session = GameSession(
                            username1=players[1]['username'],
                            username2=players[2]['username'],
                            board1=players[1]['board'],
                            board2=players[2]['board'],
                            current_player=current_player,
                            timeouts=timeouts
                        )
                        disconnected_players[p_username] = session
                
                # Wait for either player to reconnect with shorter timeout
                print(f"[INFO] Waiting for either player to reconnect (30s)...")
                
                # Use two threads to wait for both players in parallel
                reconnect_results = {"player1": None, "player2": None}
                reconnect_lock = threading.Lock()
                
                def wait_for_player_reconnect(p_username, result_key):
                    session = disconnected_players.get(p_username)
                    if session:
                        sock_new, _ = session.wait_for_reconnect(p_username, timeout=30)
                        with reconnect_lock:
                            reconnect_results[result_key] = (p_username, sock_new)
                
                thread1 = threading.Thread(target=wait_for_player_reconnect, args=(username, "player1"), daemon=True)
                thread2 = threading.Thread(target=wait_for_player_reconnect, args=(opponent_username, "player2"), daemon=True)
                
                thread1.start()
                thread2.start()
                thread1.join(timeout=35)  # Extra time for thread completion
                thread2.join(timeout=1)   # Short join for second thread
                
                # Check if either player reconnected
                reconnected_player = None
                reconnected_sock = None
                
                if reconnect_results["player1"] and reconnect_results["player1"][1]:
                    reconnected_player = reconnect_results["player1"][0]
                    reconnected_sock = reconnect_results["player1"][1]
                elif reconnect_results["player2"] and reconnect_results["player2"][1]:
                    reconnected_player = reconnect_results["player2"][0]
                    reconnected_sock = reconnect_results["player2"][1]
                
                if reconnected_player:
                    print(f"[INFO] {reconnected_player} reconnected first")
                    
                    # Update the reconnected player's socket
                    if reconnected_player == username:
                        player['sock'] = reconnected_sock
                        player_connected = True
                        seq_counters[username] = 0
                        send_msg(player['sock'], "You have reconnected. Waiting for opponent...", username)
                    else:
                        opponent['sock'] = reconnected_sock
                        opponent_connected = True
                        seq_counters[opponent_username] = 0
                        send_msg(opponent['sock'], "You have reconnected. Waiting for opponent...", opponent_username)
                    
                    # Now wait longer for the other player
                    other_player = opponent_username if reconnected_player == username else username
                    print(f"[INFO] Waiting for {other_player} to reconnect (60s)...")
                    

                    
                    # Wait for the other player
                    session = disconnected_players[other_player]
                    sock_new, _ = session.wait_for_reconnect(other_player, timeout=60)
                    
                    if sock_new:
                        # Both players reconnected
                        print(f"[INFO] {other_player} also reconnected")
                        
                        if other_player == username:
                            player['sock'] = sock_new
                            player_connected = True
                            seq_counters[username] = 0
                        else:
                            opponent['sock'] = sock_new
                            opponent_connected = True
                            seq_counters[opponent_username] = 0
                        
                        # Notify both players
                        send_msg(player['sock'], "Both players have reconnected. Resuming game...", username)
                        send_msg(opponent['sock'], "Both players have reconnected. Resuming game...", opponent_username)
                        broadcast_to_spectators("Both players reconnected. Game resuming.")
                    else:
                        # Only one player reconnected, the other forfeit
                        forfeit_player = username if reconnected_player == opponent_username else opponent_username
                        winner_player = opponent_username if forfeit_player == username else username
                        winner_sock = opponent['sock'] if forfeit_player == username else player['sock']
                        
                        send_msg(winner_sock, f"{forfeit_player} did not reconnect. You win by forfeit.", winner_player)
                        broadcast_to_spectators(f"{forfeit_player} did not reconnect. {winner_player} wins by forfeit.")
                        return "quit"
                else:
                    # Neither player reconnected
                    print(f"[INFO] Neither player reconnected within the time limit")
                    broadcast_to_spectators("Neither player reconnected. Game abandoned.")
                    return "quit"
            
            # Handle current player disconnection
            elif not player_connected:
                print(f"[INFO] Current player {username} disconnected between turns")
                
                # Prevent further reads from this socket
                try:
                    player['sock'].shutdown(socket.SHUT_RDWR)
                except:
                    pass  # Socket might already be closed
                
                # Notify opponent
                send_msg(opponent['sock'], 
                    f"\n!!! OPPONENT DISCONNECTED !!!\n{username} has disconnected. Waiting 60 seconds to see if they return...",
                    opponent_username)
                broadcast_to_spectators(f"{username} disconnected. Waiting...")
                
                # Start disconnect timeout handler
                if disconnect_timeout:
                    threading.Thread(target=disconnect_timeout, 
                                args=(username, opponent['sock']), 
                                daemon=True).start()
                
                
                # Create session if it doesn't exist
                if username not in disconnected_players:
                    print(f"[INFO] Creating reconnection session for {username}")
                    session = GameSession(
                        username1=players[1]['username'],
                        username2=players[2]['username'],
                        board1=players[1]['board'],
                        board2=players[2]['board'],
                        current_player=current_player,
                        timeouts=timeouts
                    )
                    disconnected_players[username] = session
                else:
                    session = disconnected_players[username]
                
                # Wait for reconnection
                print(f"[INFO] Waiting for {username} to reconnect...")
                sock_new, _ = session.wait_for_reconnect(username, timeout=60)
                
                if sock_new is None:
                    # Player didn't reconnect in time
                    print(f"[INFO] {username} did not reconnect in time")
                    send_msg(opponent['sock'], 
                        f"{username} did not reconnect. You win by forfeit.",
                        opponent_username)
                    broadcast_to_spectators(f"{username} did not reconnect. {opponent_username} wins by forfeit.")
                    return "quit"
                
                # Player reconnected, update socket
                print(f"[INFO] {username} reconnected successfully")
                player['sock'] = sock_new
                seq_counters[username] = 0
                send_msg(player['sock'], "You have reconnected. Resuming game...", username)
                send_msg(opponent['sock'], f"\n{username} has reconnected! Game will now continue.", opponent_username)
                broadcast_to_spectators(f"{username} has reconnected. Game resuming.")
                continue  # Restart the turn
            
            # Handle opponent disconnection
            elif not opponent_connected:
                print(f"[INFO] Opponent {opponent_username} disconnected between turns")
                
                # Prevent further reads from opponent's socket
                try:
                    opponent['sock'].shutdown(socket.SHUT_RDWR)
                except:
                    pass  # Socket might already be closed
                
                # Notify current player
                send_msg(sock, 
                    f"\n!!! OPPONENT DISCONNECTED !!!\n{opponent_username} has disconnected. Waiting 60 seconds to see if they return...",
                    username)
                broadcast_to_spectators(f"{opponent_username} disconnected. Waiting...")
                
                # Start disconnect timeout handler
                if disconnect_timeout:
                    threading.Thread(target=disconnect_timeout, 
                                args=(opponent_username, sock), 
                                daemon=True).start()
                
                
                # Create session if it doesn't exist
                if opponent_username not in disconnected_players:
                    print(f"[INFO] Creating reconnection session for {opponent_username}")
                    session = GameSession(
                        username1=players[1]['username'],
                        username2=players[2]['username'],
                        board1=players[1]['board'],
                        board2=players[2]['board'],
                        current_player=current_player,
                        timeouts=timeouts
                    )
                    disconnected_players[opponent_username] = session
                else:
                    session = disconnected_players[opponent_username]
                
                # Wait for reconnection
                print(f"[INFO] Waiting for {opponent_username} to reconnect...")
                sock_new, _ = session.wait_for_reconnect(opponent_username, timeout=60)
                
                if sock_new is None:
                    # Opponent didn't reconnect in time
                    print(f"[INFO] {opponent_username} did not reconnect in time")
                    send_msg(sock, 
                        f"{opponent_username} did not reconnect. You win by forfeit.",
                        username)
                    broadcast_to_spectators(f"{opponent_username} did not reconnect. {username} wins by forfeit.")
                    return "quit"
                
                # Opponent reconnected, update socket
                print(f"[INFO] {opponent_username} reconnected successfully")
                opponent['sock'] = sock_new
                seq_counters[opponent_username] = 0
                send_msg(opponent['sock'], "You have reconnected. Resuming game...", opponent_username)
                send_msg(sock, f"\n{opponent_username} has reconnected! Game will now continue.", username)
                broadcast_to_spectators(f"{opponent_username} has reconnected. Game resuming.")

            # Send turn notification
            send_success = send_msg(sock, "\nYour turn!", username)
            if not send_success:
                # If sending fails, likely disconnected - restart loop for disconnection check
                print(f"[INFO] Failed to send turn notification to {username}, likely disconnected")
                continue
                
            broadcast_to_spectators(f"{player['username']}'s turn")
            
            # Show boards with connection check
            try:
                send_dual_boards(sock, username, player['board'], opponent["board"])
                send_msg(sock, "Enter coordinate to fire at (e.g. B5):", username)
            except Exception as e:
                print(f"[ERROR] Failed to send boards to {username}: {e}")
                # Likely disconnected, restart loop for disconnection check
                continue

            # Get player's guess
            try:
                guess = recv_msg(sock, username, timeout=60)  # Pass username to recv_msg
                
                # Handle different result types
                if guess == "CORRUPTED_PACKET":
                    send_msg(sock, "Invalid packet received. Please try again.", username, MessageType.RESULT)
                    continue
                elif guess == "SOCKET_TIMEOUT":  # Add handling for socket timeout
                    send_msg(sock, "Communication timed out. Please try again.", username, MessageType.RESULT)
                    continue
                elif guess == "CONNECTION_LOST":  # Add handling for this special case
                    # Restart loop to trigger disconnection handling
                    continue

            except ConnectionError:
                # Handle disconnection
                send_msg(opponent['sock'], 
                    f"\n!!! OPPONENT DISCONNECTED !!!\n{player['username']} has disconnected. Waiting 60 seconds to see if they return...\nPlease wait for reconnection or timeout.",
                    opponent_username)
                broadcast_to_spectators(f"{player['username']} disconnected. Waiting...")
                
                # Start disconnect timeout handler
                if disconnect_timeout:
                    threading.Thread(target=disconnect_timeout, 
                                args=(player['username'], opponent['sock']), 
                                daemon=True).start()

                # Wait for reconnection
                if player['username'] not in disconnected_players:
                    print(f"[INFO] Creating reconnection session for {player['username']}")
                    session = GameSession(
                        username1=players[1]['username'],
                        username2=players[2]['username'],
                        board1=players[1]['board'],
                        board2=players[2]['board'],
                        current_player=current_player,
                        timeouts=timeouts
                    )
                    disconnected_players[player['username']] = session
                else:
                    session = disconnected_players[player['username']]
                
                
                sock_new, _ = session.wait_for_reconnect(player['username'], timeout=60)

                if sock_new is None:
                    # Player didn't reconnect in time
                    send_msg(opponent['sock'], 
                        f"{player['username']} did not reconnect. You win by forfeit.",
                        opponent_username)
                    broadcast_to_spectators(f"{player['username']} did not reconnect. {opponent_username} wins by forfeit.")
                    return "quit"

                # Player reconnected, update socket
                player['sock'] = sock_new
                # Reset sequence counter for the reconnected player
                seq_counters[player['username']] = 0
                send_msg(player['sock'], "You have reconnected. Resuming game...", player['username'])
                send_msg(opponent['sock'], f"\n{player['username']} has reconnected! Game will now continue.", opponent_username)
                broadcast_to_spectators(f"{player['username']} has reconnected. Game resuming.")
                continue

            # Handle timeout
            if guess is None:
                timeouts[current_player] += 1
                if timeouts[current_player] >= 3:
                    # Player timed out 3 times -> forfeit
                    send_msg(sock, "You timed out 3 times. You have been auto-forfeited.", username)
                    try:
                        send_msg(opponent['sock'], 
                            f"{player['username']} timed out 3 times. You win!",
                            opponent_username)
                    except ConnectionError:
                        return "quit"
                    broadcast_to_spectators(f"{player['username']} forfeited by timeout.")

                    # Ask remaining player if they want to play again
                    try:
                        time.sleep(2)
                        send_msg(opponent['sock'], 
                            "Would you like to play again with a new player? (Y/N)",
                            opponent_username)
                    except ConnectionError:
                        return "quit"

                    response = ask_play_again(opponent['sock'], opponent_username, opponent_username)
                    if response == 'y':
                        return ("one_left", 2 if current_player == 1 else 1)
                    else:
                        return "quit"
                else:
                    # Skip this turn
                    send_msg(sock, 
                        f"Timeout! You took too long ({timeouts[current_player]}/3). Turn skipped.",
                        username)
                    try:
                        send_msg(opponent['sock'], 
                            f"{player['username']} took too long. Your turn next!",
                            opponent_username)
                    except ConnectionError:
                        return "quit"
                    broadcast_to_spectators(f"{player['username']} took too long. Turn skipped.")

                # Switch to other player
                current_player = 2 if current_player == 1 else 1
                continue

            # Handle player quitting
            if guess and guess.lower() == 'quit':
                send_msg(sock, "You quit. Game over.", username)
                try:
                    send_msg(opponent['sock'], 
                        f"{player['username']} quit. You win!",
                        opponent_username)
                except ConnectionError:
                    return "quit"
                broadcast_to_spectators(f"{player['username']} quit the game.")

                # Ask remaining player if they want to play again
                try:
                    time.sleep(2)
                    send_msg(opponent['sock'], 
                        "Would you like to play again with a new player? (Y/N)",
                        opponent_username)
                except ConnectionError:
                    return "quit"

                response = ask_play_again(opponent['sock'], opponent_username, opponent_username)
                if response == 'y':
                    return ("one_left", 2 if current_player == 1 else 1)
                else:
                    return "quit"

            # Process guess with improved validation
            try:
                if not guess:
                    send_msg(sock, "Invalid input: No coordinate provided", username, MessageType.RESULT)
                    continue
                
                # Basic format validation
                if not isinstance(guess, str):
                    send_msg(sock, f"Invalid input type. Please enter coordinates like A1, B5, etc.", username, MessageType.RESULT)
                    continue
                    
                if len(guess) < 2 or not guess[0].isalpha():
                    send_msg(sock, f"Invalid coordinate format: '{guess}'. Please use format like A1, B5, etc.", username, MessageType.RESULT)
                    continue
                    
                # Try to parse with bounds checking
                try:
                    row, col = parse_coordinate(guess)
                    
                    # Check board bounds
                    if row < 0 or row >= opponent['board'].size or col < 0 or col >= opponent['board'].size:
                        max_row = chr(ord('A') + opponent['board'].size - 1)
                        max_col = opponent['board'].size
                        send_msg(sock, f"Coordinates out of bounds. Valid range is A1 to {max_row}{max_col}.", username, MessageType.RESULT)
                        continue
                except ValueError as e:
                    send_msg(sock, f"Invalid coordinate: {e}", username, MessageType.RESULT)
                    continue
                    
                # Fire at the target with enhanced error handling
                try:
                    result, sunk_name = opponent['board'].fire_at(row, col)
                except Exception as e:
                    print(f"[ERROR] Error in fire_at: {e}")
                    send_msg(sock, "Error processing your shot. Please try again.", username, MessageType.RESULT)
                    continue
                    
                # Add handling for out of bounds result
                if result == 'out_of_bounds':
                    send_msg(sock, "Coordinates out of bounds. Try again.", username, MessageType.RESULT)
                    continue

                # Handle hit
                if result == 'hit':
                    if sunk_name:
                        send_msg(sock, f"HIT! You sank the {sunk_name}!", username, MessageType.RESULT)
                        try:
                            send_msg(opponent['sock'], 
                                f"{player['username']} sank your {sunk_name}!",
                                opponent_username, MessageType.RESULT)
                        except ConnectionError:
                            # Opponent likely disconnected
                            continue  # Restart loop to detect disconnection
                        broadcast_to_spectators(f"{player['username']} sank the {sunk_name}!")
                    else:
                        send_msg(sock, "HIT!", username, MessageType.RESULT)
                        try:
                            send_msg(opponent['sock'], 
                                f"{player['username']} hit your ship!",
                                opponent_username, MessageType.RESULT)
                        except ConnectionError:
                            continue  # Restart loop to detect disconnection
                        broadcast_to_spectators(f"{player['username']} scored a HIT.")
                
                # Handle miss
                elif result == 'miss':
                    send_msg(sock, "MISS!", username, MessageType.RESULT)
                    try:
                        send_msg(opponent['sock'], 
                            f"{player['username']} missed.",
                            opponent_username, MessageType.RESULT)
                    except ConnectionError:
                        continue  # Restart loop to detect disconnection
                    broadcast_to_spectators(f"{player['username']} MISSED.")
                
                # Handle already shot location
                elif result == 'already_shot':
                    send_msg(sock, "Already fired there. Try again.", username, MessageType.RESULT)
                    continue

                # Check for game over
                if opponent['board'].all_ships_sunk():
                    send_msg(sock, "You win! All enemy ships sunk.", username, MessageType.RESULT)
                    try:
                        send_msg(opponent['sock'], 
                            "You lose. All your ships are sunk.",
                            opponent_username, MessageType.RESULT)
                    except ConnectionError:
                        return "quit"
                    broadcast_to_spectators(f"{player['username']} WON the game!")

                    # Ask both players if they want to play again
                    time.sleep(2)
                    send_msg(sock, "Would you like to play again? (Y/N)", username)
                    try:
                        send_msg(opponent['sock'], 
                            "Would you like to play again? (Y/N)",
                            opponent_username)
                    except ConnectionError:
                        return "quit"

                    # Get responses
                    response1 = ask_play_again(players[1]['sock'], players[1]['username'], players[1]['username'])
                    response2 = ask_play_again(players[2]['sock'], players[2]['username'], players[2]['username'])

                    # Handle rematch options
                    if response1 == 'y' and response2 == 'y':
                        broadcast_to_spectators(f"Both players chose to play again.")
                        return "rematch"
                    elif response1 == 'y':
                        broadcast_to_spectators(f"{players[1]['username']} wants to play again, {players[2]['username']} does not.")
                        return ("one_left", 1)
                    elif response2 == 'y':
                        broadcast_to_spectators(f"{players[2]['username']} wants to play again, {players[1]['username']} does not.")
                        return ("one_left", 2)
                    else:
                        broadcast_to_spectators(f"Neither player chose to play again. Game ended.")
                        return "quit"

                # Switch players for next turn
                current_player = 2 if current_player == 1 else 1

            except ValueError as e:
                # Invalid coordinate
                send_msg(sock, f"Invalid input: {e}", username, MessageType.RESULT)
                continue
            except Exception as e:
                # Catch-all for unexpected errors
                print(f"[ERROR] Unexpected error processing guess: {e}")
                import traceback
                traceback.print_exc()
                
                # Check if it's a connection error
                if isinstance(e, (ConnectionError, BrokenPipeError)) or "connection" in str(e).lower():
                    continue  # Restart loop to detect disconnection
                    
                # Otherwise, just inform the player and continue
                send_msg(sock, "An error occurred processing your input. Please try again.", username, MessageType.RESULT)
                continue

    except ValueError as e:
        # General error handling
        try:
            send_msg(sock, f"Invalid input: {e}", username)
        except ConnectionError:
            pass
        return "quit"
    except Exception as e:
        # Catch-all for unexpected errors
        print(f"[ERROR] Unexpected error in game: {e}")
        import traceback
        traceback.print_exc()
        return "quit"

if __name__ == "__main__":
    # Optional: run this file as a script to test single-player mode
    run_single_player_game_locally()

