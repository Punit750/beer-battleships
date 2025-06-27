import struct
import zlib
import socket
import os
import base64
from enum import IntEnum
from Crypto.Cipher import AES
from Crypto.Util import Counter
import random
import pickle

# Protocol constants
MAGIC_BYTES = b'BEER'
MAX_PAYLOAD_SIZE = 8192  # Increased size for board data
IV_SIZE = 16  # 16 bytes (128 bits) for AES
KEY_SIZE = 32  # 32 bytes (256 bits) for AES-256

# Message types for the Battleship game
class MessageType(IntEnum):
    PLACE = 0
    FIRE = 1
    RESULT = 2
    DEFAULT = 3
    GRID = 4
    CHAT_GLOBAL = 5

class EncryptedBattleshipPacket:
    def __init__(self):
        self.magic = MAGIC_BYTES
        self.type = MessageType.DEFAULT
        self.seq = 0              # Sequence number (helps prevent replay attacks)
        self.iv = os.urandom(IV_SIZE)  # Random IV for each packet
        self.checksum = 0         # Checksum for data integrity
        self.payload_len = 0      # Length of encrypted payload
        self.payload = bytes()    # Encrypted message data

    def encrypt_payload(self, payload, key):
        """Encrypt the payload using AES-256 in CTR mode"""
        # Convert string payload to bytes if needed
        if isinstance(payload, str):
            payload = payload.encode('utf-8')
        
        # Create an integer counter from the IV
        counter = int.from_bytes(self.iv, byteorder='big')
        
        # Create a counter function for CTR mode
        ctr = Counter.new(128, initial_value=counter)
        
        # Create the cipher
        cipher = AES.new(key, AES.MODE_CTR, counter=ctr)
        
        # Encrypt the payload
        self.payload = cipher.encrypt(payload)
        self.payload_len = len(self.payload)
        
    def decrypt_payload(self, key):
        """Decrypt the payload using AES-256 in CTR mode"""
        # Create an integer counter from the IV
        counter = int.from_bytes(self.iv, byteorder='big')
        
        # Create a counter function for CTR mode
        ctr = Counter.new(128, initial_value=counter)
        
        # Create the cipher
        cipher = AES.new(key, AES.MODE_CTR, counter=ctr)
        
        # Decrypt the payload
        decrypted_payload = cipher.decrypt(self.payload)
        return decrypted_payload
        
    def pack(self):
        """Pack the encrypted packet for transmission"""
        # First pack with checksum=0 for checksum calculation
        header = struct.pack('!4sHH16sII', 
                          self.magic,       # 4s - 4 bytes string for magic
                          self.type,        # H - unsigned short (2 bytes) for type
                          self.seq,         # H - unsigned short (2 bytes) for sequence
                          self.iv,          # 16s - 16 bytes for IV
                          0,                # I - unsigned int (4 bytes) for checksum (initially 0)
                          self.payload_len) # I - unsigned int (4 bytes) for payload length
        
        # Create the initial packet without checksum
        initial_packet = header + self.payload
        
        # Calculate checksum
        self.checksum = zlib.crc32(initial_packet)
        
        # Repack with the actual checksum
        final_header = struct.pack('!4sHH16sII', 
                                 self.magic,
                                 self.type,
                                 self.seq,
                                 self.iv,
                                 self.checksum,
                                 self.payload_len)
        
        # Return complete packet
        return final_header + self.payload

    def unpack(self, packet_bytes):
        """Unpack and verify an encrypted packet"""
        # Check if packet is long enough to contain a header
        header_format = '!4sHH16sII'
        header_size = struct.calcsize(header_format)
        
        if len(packet_bytes) < header_size:
            return False
        
        # Extract header fields
        self.magic, self.type, self.seq, self.iv, received_checksum, self.payload_len = struct.unpack_from(header_format, packet_bytes)
        
        # Validate magic bytes
        if self.magic != MAGIC_BYTES:
            return False
        
        # Safety check for payload length
        if self.payload_len > MAX_PAYLOAD_SIZE:
            print(f"[WARNING] Payload too large: {self.payload_len}")
            return False
            
        # Extract payload
        if len(packet_bytes) < header_size + self.payload_len:
            return False
            
        self.payload = packet_bytes[header_size:header_size + self.payload_len]
        
        # Verify payload length
        if len(self.payload) != self.payload_len:
            return False
        
        # Verify checksum
        # Create a copy of the packet with checksum set to 0
        self.checksum = 0
        temp_packet = bytearray(packet_bytes)
        # Set checksum bytes to 0 in the copy
        checksum_offset = struct.calcsize('!4sHH16s')
        for i in range(checksum_offset, checksum_offset + 4):
            temp_packet[i] = 0
        
        # Calculate checksum on the modified packet
        calculated_checksum = zlib.crc32(bytes(temp_packet))
        
        # Set the actual checksum for this object
        self.checksum = received_checksum
        
        # Return whether the checksums match
        return calculated_checksum == received_checksum

# Key management class
class KeyManager:
    def __init__(self, pre_shared_key=None):
        """Initialize with either a pre-shared key or generate a new one"""
        if pre_shared_key:
            # Ensure key is exactly 32 bytes
            if len(pre_shared_key) != KEY_SIZE:
                print(f"[WARNING] Provided key size ({len(pre_shared_key)} bytes) is not valid for AES-256")
                print(f"[INFO] Using first {KEY_SIZE} bytes or padding with zeros")
                # Either truncate or pad the key to ensure it's exactly 32 bytes
                if len(pre_shared_key) > KEY_SIZE:
                    self.key = pre_shared_key[:KEY_SIZE]
                else:
                    self.key = pre_shared_key + b'\0' * (KEY_SIZE - len(pre_shared_key))
            else:
                self.key = pre_shared_key
        else:
            # Generate a cryptographically secure random key
            self.key = os.urandom(KEY_SIZE)
            print(f"[INFO] Generated new secure random key ({KEY_SIZE} bytes)")
    
    @classmethod
    def from_base64(cls, base64_key):
        """Create a KeyManager from a base64-encoded key"""
        key = base64.b64decode(base64_key)
        return cls(key)
    
    def to_base64(self):
        """Convert the key to a base64-encoded string for storage/sharing"""
        return base64.b64encode(self.key).decode('utf-8')
    
    def save_to_file(self, filename='.battleship_key'):
        """Save the key to a file (for development purposes)"""
        print(f"[INFO] Saving key ({len(self.key)} bytes) to {filename}")
        with open(filename, 'wb') as f:
            f.write(self.key)
    
    @classmethod
    def load_from_file(cls, filename='.battleship_key'):
        """Load the key from a file"""
        try:
            with open(filename, 'rb') as f:
                key_data = f.read()
            
            print(f"[INFO] Loaded key from {filename} ({len(key_data)} bytes)")
            
            # Check if key is the correct size for AES-256
            if len(key_data) == KEY_SIZE:
                return cls(key_data)
            elif len(key_data) > KEY_SIZE:
                # Key might be encoded or in wrong format
                print(f"[WARNING] Key in {filename} has incorrect size ({len(key_data)} bytes)")
                print(f"[INFO] Using first {KEY_SIZE} bytes of key")
                return cls(key_data[:KEY_SIZE])
            else:
                print(f"[WARNING] Key in {filename} is too small ({len(key_data)} bytes)")
                print("[INFO] Padding key with zeros")
                padded_key = key_data + b'\0' * (KEY_SIZE - len(key_data))
                return cls(padded_key)
        except FileNotFoundError:
            print(f"[WARNING] Key file {filename} not found. Generating new key.")
            return cls()

# Enhanced protocol functions for encryption
def create_encrypted_packet(message_type, payload, key, seq_num=0):
    """Create an encrypted packet with the specified message type and payload"""
    packet = EncryptedBattleshipPacket()
    packet.type = message_type
    packet.seq = seq_num
    packet.encrypt_payload(payload, key)
    
    return packet.pack()

def parse_encrypted_packet(packet_bytes, key):
    """Parse, verify, and decrypt a packet from raw bytes"""
    packet = EncryptedBattleshipPacket()
    is_valid = packet.unpack(packet_bytes)
    
    if is_valid:
        # Decrypt the payload
        decrypted_payload = packet.decrypt_payload(key)
        
        # Try to decode as UTF-8 if it's text data
        try:
            decrypted_payload = decrypted_payload.decode('utf-8')
        except UnicodeDecodeError:
            # Keep as bytes if it can't be decoded
            pass
            
        return True, packet.type, decrypted_payload, packet.seq
    else:
        return False, None, None, None

def send_encrypted_packet(sock, message_type, payload, key, seq_num=0):
    """Send an encrypted packet through a socket"""
    try:
        packet_bytes = create_encrypted_packet(message_type, payload, key, seq_num)
        sock.sendall(packet_bytes)
        return True
    except Exception as e:
        print(f"[ERROR] Error sending encrypted packet: {e}")
        return False

def receive_encrypted_packet(sock, key):
    """Receive and decrypt a packet from a socket"""
    # Set a timeout for socket operations
    original_timeout = sock.gettimeout()
    sock.settimeout(15)  # 15 second timeout
    
    try:
        # Check if socket is closed before trying to read
        if not check_connection(sock):
            return False, None, None
            
        # Read header in chunks to handle TCP fragmentation
        header_format = '!4sHH16sII'  # Modified to include IV
        header_size = struct.calcsize(header_format)
        header_data = b''
        
        while len(header_data) < header_size:
            chunk = sock.recv(header_size - len(header_data))
            if not chunk:  # Connection closed
                return False, None, None
            header_data += chunk
        
        # Extract payload length
        magic, msg_type, seq, iv, checksum, payload_len = struct.unpack(header_format, header_data)
        
        # Validate magic bytes and payload length
        if magic != MAGIC_BYTES or payload_len > MAX_PAYLOAD_SIZE:
            return False, None, None
        
        # Read the payload in chunks
        payload_data = b''
        while len(payload_data) < payload_len:
            chunk = sock.recv(min(1024, payload_len - len(payload_data)))
            if not chunk:  # Connection closed
                return False, None, None
            payload_data += chunk
        
        # Combine and parse
        packet_bytes = header_data + payload_data
        is_valid, msg_type, decrypted_payload, seq_num = parse_encrypted_packet(packet_bytes, key)
        
        if is_valid:
            return True, msg_type, decrypted_payload
        else:
            return False, None, None
    
    except socket.timeout:
        print("[ERROR] Socket timeout during receive")
        return False, None, None
    except Exception as e:
        print(f"[ERROR] Error receiving encrypted packet: {e}")
        return False, None, None
    finally:
        # Restore original timeout
        sock.settimeout(original_timeout)

# Keep original functions like check_connection
def check_connection(sock):
    """
    Check if a connection is still alive using multiple methods.
    
    Args:
        sock (socket): Socket to check
        
    Returns:
        bool: True if connection is alive, False otherwise
    """
    try:
        # Method 1: Try getpeername() - most reliable but doesn't check if connection is responsive
        sock.getpeername()
        
        # Method 2: Non-blocking peek to check if data is available or connection closed
        try:
            # Set socket to non-blocking temporarily
            blocking = sock.getblocking()
            sock.setblocking(False)
            
            # Try to peek at data
            try:
                data = sock.recv(1, socket.MSG_PEEK)
                if not data:  # Empty data means socket was closed
                    sock.setblocking(blocking)  # Restore blocking state
                    return False
            except (BlockingIOError, socket.error):
                # No data available but socket is still open
                pass
                
            # Restore blocking state
            sock.setblocking(blocking)
            
        except Exception:
            # Ignore errors from this method and rely on getpeername
            pass
            
        return True
        
    except Exception:
        # Any error means the connection is dead
        return False
# Keep the original functions but mark them as deprecated
# This allows for backward compatibility during transition
def send_packet(sock, message_type, payload, seq_num=0):
    """[DEPRECATED] Use send_encrypted_packet instead"""
    try:
        # Convert string payload to bytes if needed
        if isinstance(payload, str):
            payload = payload.encode('utf-8')
            
        # Create packet
        packet = BattleshipPacket()
        packet.type = message_type
        packet.seq = seq_num
        packet.payload = payload
        packet.payload_len = len(payload)
        
        # Pack and send
        packet_bytes = packet.pack()
        sock.sendall(packet_bytes)
        return True
    except Exception as e:
        print(f"[ERROR] Error sending packet: {e}")
        return False

class BattleshipPacket:
    def __init__(self):
        self.magic = MAGIC_BYTES  # Use the constant
        self.type = MessageType.DEFAULT  # Default type
        self.seq = 0              # Sequence number
        self.checksum = 0         # Checksum for data integrity
        self.payload_len = 0      # Length of payload
        self.payload = bytes()    # Actual message data

    def pack(self):
        # Convert string payload to bytes if needed
        if isinstance(self.payload, str):
            self.payload = self.payload.encode('utf-8')
            self.payload_len = len(self.payload)
            
        # First pack with checksum=0 for checksum calculation
        header = struct.pack('!4sHHII', 
                          self.magic,       # 4s - 4 bytes string for magic
                          self.type,        # H - unsigned short (2 bytes) for type
                          self.seq,         # H - unsigned short (2 bytes) for sequence
                          0,                # I - unsigned int (4 bytes) for checksum (initially 0)
                          self.payload_len) # I - unsigned int (4 bytes) for payload length
        
        # Create the initial packet without checksum
        initial_packet = header + self.payload
        
        # Calculate checksum
        self.checksum = zlib.crc32(initial_packet)
        
        # Repack with the actual checksum
        final_header = struct.pack('!4sHHII', 
                                 self.magic,
                                 self.type,
                                 self.seq,
                                 self.checksum,
                                 self.payload_len)
        
        # Return complete packet
        return final_header + self.payload

    def unpack(self, packet_bytes):
        # Check if packet is long enough to contain a header
        header_format = '!4sHHII'
        header_size = struct.calcsize(header_format)
        
        if len(packet_bytes) < header_size:
            return False
        
        # Extract header fields
        self.magic, self.type, self.seq, received_checksum, self.payload_len = struct.unpack_from(header_format, packet_bytes)
        
        # Validate magic bytes
        if self.magic != MAGIC_BYTES:
            return False
        
        # Safety check for payload length
        if self.payload_len > MAX_PAYLOAD_SIZE:
            print(f"[WARNING] Payload too large: {self.payload_len}")
            return False
            
        # Extract payload
        if len(packet_bytes) < header_size + self.payload_len:
            return False
            
        self.payload = packet_bytes[header_size:header_size + self.payload_len]
        
        # Verify payload length
        if len(self.payload) != self.payload_len:
            return False
        
        # Verify checksum
        # Create a copy of the packet with checksum set to 0
        self.checksum = 0
        temp_packet = bytearray(packet_bytes)
        # Set checksum bytes to 0 in the copy
        checksum_offset = struct.calcsize('!4sHH')
        for i in range(checksum_offset, checksum_offset + 4):
            temp_packet[i] = 0
        
        # Calculate checksum on the modified packet
        calculated_checksum = zlib.crc32(bytes(temp_packet))
        
        # Set the actual checksum for this object
        self.checksum = received_checksum
        
        # Return whether the checksums match
        return calculated_checksum == received_checksum

def create_packet(message_type, payload, seq_num=0):
    """
    Create a BattleshipPacket with the specified message type and payload.
    
    Args:
        message_type (MessageType): Type of message
        payload (bytes or str): Message payload
        seq_num (int): Sequence number
        
    Returns:
        bytes: Serialized packet ready for transmission
    """
    # Convert string payload to bytes if needed
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    
    packet = BattleshipPacket()
    packet.type = message_type
    packet.seq = seq_num
    packet.payload = payload
    packet.payload_len = len(payload)
    
    return packet.pack()

def parse_packet(packet_bytes):
    """Parse a packet from raw bytes"""
    packet = BattleshipPacket()
    is_valid = packet.unpack(packet_bytes)
    
    if is_valid:
        return True, packet
    else:
        return False, None



def test_corruption():
    """
    Test function that demonstrates packet corruption detection.
    Creates a packet, corrupts it, and verifies that the corruption is detected.
    
    Returns:
        tuple: (total_tests, successful_detections)
    """
    test_payload = "Hello Battleship World!"
    original_packet = create_packet(MessageType.DEFAULT, test_payload, 1)
    
    # Test 1: No corruption (should pass)
    is_valid, _ = parse_packet(original_packet)
    test1 = is_valid
    
    # Test 2: Corrupt magic bytes
    corrupt_packet = bytearray(original_packet)
    corrupt_packet[0] = (corrupt_packet[0] + 1) % 256  # Change first byte of magic
    is_valid, _ = parse_packet(bytes(corrupt_packet))
    test2 = not is_valid  # Should be invalid
    
    # Test 3: Corrupt checksum
    corrupt_packet = bytearray(original_packet)
    checksum_offset = struct.calcsize('!4sHH')
    corrupt_packet[checksum_offset] = (corrupt_packet[checksum_offset] + 1) % 256
    is_valid, _ = parse_packet(bytes(corrupt_packet))
    test3 = not is_valid  # Should be invalid
    
    # Test 4: Corrupt payload
    corrupt_packet = bytearray(original_packet)
    header_size = struct.calcsize('!4sHHII')
    corrupt_packet[header_size] = (corrupt_packet[header_size] + 1) % 256
    is_valid, _ = parse_packet(bytes(corrupt_packet))
    test4 = not is_valid  # Should be invalid
    
    return (4, sum([test1, test2, test3, test4]))


def check_connection(sock):
    """
    Check if a connection is still alive using multiple methods.
    
    Args:
        sock (socket): Socket to check
        
    Returns:
        bool: True if connection is alive, False otherwise
    """
    try:
        # Method 1: Try getpeername() - most reliable but doesn't check if connection is responsive
        sock.getpeername()
        
        # Method 2: Non-blocking peek to check if data is available or connection closed
        try:
            # Set socket to non-blocking temporarily
            blocking = sock.getblocking()
            sock.setblocking(False)
            
            # Try to peek at data
            try:
                data = sock.recv(1, socket.MSG_PEEK)
                if not data:  # Empty data means socket was closed
                    sock.setblocking(blocking)  # Restore blocking state
                    return False
            except (BlockingIOError, socket.error):
                # No data available but socket is still open
                pass
                
            # Restore blocking state
            sock.setblocking(blocking)
            
        except Exception:
            # Ignore errors from this method and rely on getpeername
            pass
            
        return True
        
    except Exception:
        # Any error means the connection is dead
        return False
    

# Add to Protocol.py
import hmac
import hashlib
import time

class UserAuthenticator:
    def __init__(self, secret_key=None):
        """Initialize with a secret key or generate a new one"""
        self.secret_key = secret_key or os.urandom(32)
        self.registered_users = {}  # username -> auth_token
        
    def register_user(self, username, password):
        """Register a new user with a password"""
        # Create a secure hash of the password
        password_hash = hashlib.sha256(password.encode()).digest()
        
        # Generate a unique auth token for this user
        auth_token = hmac.new(self.secret_key, username.encode(), hashlib.sha256).hexdigest()
        
        # Store the user credentials
        self.registered_users[username] = {
            'password_hash': password_hash,
            'auth_token': auth_token,
            'last_login': 0
        }
        
        return auth_token
    
    def authenticate_user(self, username, password):
        """Authenticate a user with password, return auth token if successful"""
        if username not in self.registered_users:
            return None
        
        # Check password
        password_hash = hashlib.sha256(password.encode()).digest()
        if password_hash != self.registered_users[username]['password_hash']:
            return None
        
        # Update last login
        self.registered_users[username]['last_login'] = time.time()
        
        # Return auth token
        return self.registered_users[username]['auth_token']
    
    def verify_token(self, username, token):
        """Verify a user's authentication token"""
        if username not in self.registered_users:
            return False
        
        return hmac.compare_digest(token, self.registered_users[username]['auth_token'])
    
    def save_to_file(self, filename='.user_auth.dat'):
        """Save registered users to a file"""
        with open(filename, 'wb') as f:
            pickle.dump(self.registered_users, f)
        
    def load_from_file(self, filename='.user_auth.dat'):
        """Load registered users from a file"""
        try:
            with open(filename, 'rb') as f:
                self.registered_users = pickle.load(f)
            return True
        except FileNotFoundError:
            return False
        
    def check_password(self, username, password):
        """Verify a user's password"""
        if username not in self.registered_users:  # Changed from self.users
            return False
        
        # Create hash from provided password
        password_hash = hashlib.sha256(password.encode()).digest()
        
        # Compare to stored hash
        return password_hash == self.registered_users[username]['password_hash']
    
    def generate_token(self, username):
        """Generate a new token for a user"""
        # Generate a unique auth token for this user
        auth_token = hmac.new(self.secret_key, username.encode(), hashlib.sha256).hexdigest()
        
        # Update the stored token
        if username in self.registered_users:
            self.registered_users[username]['auth_token'] = auth_token
        
        return auth_token


