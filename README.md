Battleship Game with Encryption
A secure multiplayer Battleship game implementation featuring AES-256 encryption, authentication, and reconnection handling.

Overview

This project implements a client-server version of the classic Battleship game with robust security features:

AES-256 encryption in CTR mode for all communications
User registration and authentication system
Secure reconnection handling for disconnected players
Spectator mode for watching ongoing games

Requirements

Python 3.7+

pycryptodome 3.19.0+

Installation

Set up a virtual environment (recommended):

For Mac:
python3 -m venv venv
source venv/bin/activate  

On Windows:
python -m venv venv
venv\Scripts\activate

Install dependencies:

pip install -r requirements.txt

Running the Game

Start the server:

python server.py

Start the client in a separate terminal:

python client.py

Follow the on-screen instructions to register/login and play.

Game Features

Ship Placement: Arrange your ships strategically on a 10x10 grid

Turn-Based Gameplay: Take turns firing at your opponent's grid

Real-time Updates: See hits, misses, and sunk ships as they happen

Secure Communication: All messages are encrypted with AES-256

Security Features

Encryption

Algorithm: AES-256 in CTR mode

Key Management: Pre-shared key approach (testing) with secure storage

Packet Protection: Includes checksums for integrity verification

Replay Prevention: Sequence numbers and unique IVs for every packet

Authentication

Registration: Create an account with username and password

Login: Authenticate with password or token

Reconnection: Secure handling of disconnected players

Troubleshooting

Key Synchronization Issues: If you see "Corrupted Packet" warnings, ensure that both client and server have the same encryption key file (.battleship_key)

Connection Issues: Check that the server is running and firewall settings allow connections

Authentication Failures: Make sure username and password are correct

Project Structure

server.py: Main server implementation

client.py: Client implementation

game_session.py: Game state management

authenticator.py: User authentication system

encryption.py: Encryption and decryption utilities# beer-battleships
