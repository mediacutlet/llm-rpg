#!/usr/bin/env python3
"""
LLM RPG Agent - Connect your local Ollama to the public LLM RPG server

Your Ollama runs locally. This script just coordinates between your LLM and the game server.

Setup:
    1. Create character at https://llm-rpg.example.com/create
    2. Save your token
    3. Run: python agent.py --server https://llm-rpg.example.com --token YOUR_TOKEN

Requirements:
    - Ollama running locally (ollama serve)
    - requests library (pip install requests)
"""

import argparse
import requests
import json
import time
import subprocess
import sys

DEFAULT_MODEL = "llama3"  # Change to your preferred model
POLL_INTERVAL = 2  # Seconds between checking if we can act


def ollama_generate(prompt: str, model: str) -> str:
    """Call local Ollama to generate a response."""
    try:
        result = subprocess.run(
            ["ollama", "run", model, "--nowordwrap"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        print("  ⚠️ Ollama timeout, using fallback")
        return "move east"
    except FileNotFoundError:
        print("  ❌ Ollama not found. Is it installed and running?")
        print("     Install: https://ollama.ai")
        print("     Then run: ollama serve")
        sys.exit(1)
    except Exception as e:
        print(f"  ⚠️ Ollama error: {e}")
        return "move east"


class LLMRPGAgent:
    def __init__(self, server: str, token: str, model: str):
        self.server = server.rstrip('/')
        self.token = token
        self.model = model
        self.headers = {"Authorization": f"Bearer {token}"}
        
        # These get populated from server
        self.char_id = None
        self.name = None
        self.emoji = None
        self.personality = None
        self.traits = []
    
    def get_character_info(self) -> bool:
        """Fetch character info from server using token."""
        try:
            # First, find our character by trying to look (token identifies us)
            r = requests.get(f"{self.server}/api/me", headers=self.headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                self.char_id = data['id']
                self.name = data['name']
                self.emoji = data['emoji']
                self.personality = data['personality']
                self.traits = data.get('traits', [])
                return True
            else:
                print(f"❌ Could not authenticate. Check your token.")
                print(f"   Server response: {r.text}")
                return False
        except Exception as e:
            print(f"❌ Could not connect to server: {e}")
            return False
    
    def look(self) -> dict:
        """Get character's view of the world."""
        r = requests.get(
            f"{self.server}/api/look/{self.char_id}",
            headers=self.headers,
            timeout=10
        )
        return r.json()
    
    def act(self, action: str, **kwargs) -> dict:
        """Submit an action to the server."""
        r = requests.post(
            f"{self.server}/api/action/{self.char_id}",
            headers=self.headers,
            json={"action": action, **kwargs},
            timeout=10
        )
        return r.json()
    
    def build_prompt(self, state: dict) -> tuple:
        """Build a prompt for Ollama based on world state."""
        
        text_desc = state.get("textDescription", "")
        nearby_chars = state.get("nearbyCharacters", [])
        recent_convos = state.get("recentConversations", [])
        can_talk = state.get("canTalk", False)
        valid_moves = state.get("validMoves", [])
        
        # Format traits
        traits_str = ", ".join(self.traits) if self.traits else ""
        
        # Build conversation context
        convo_context = ""
        if recent_convos:
            convo_context = "\nRecent conversation:\n"
            for c in recent_convos[-3:]:
                speaker = c.get("speaker_name", "Someone")
                msg = c.get("message", "")
                convo_context += f'  {speaker}: "{msg}"\n'
        
        if can_talk and nearby_chars:
            other = nearby_chars[0]
            other_name = other["name"]
            
            # Check if they said something to us
            last_said_to_me = None
            for c in reversed(recent_convos):
                if c.get("listener_id") == self.char_id:
                    last_said_to_me = c.get("message")
                    break
            
            if last_said_to_me:
                prompt = f"""You are {self.name}. 
Personality: {self.personality}
Traits: {traits_str}

{other_name} just said to you: "{last_said_to_me}"
{convo_context}

Respond naturally in character. Keep it to 1-2 sentences.

Say your response (dialogue only, no actions):"""
            else:
                prompt = f"""You are {self.name}.
Personality: {self.personality}
Traits: {traits_str}

You see {other_name} right next to you! Start a conversation.
{convo_context}

Greet them or say something interesting. Keep it to 1-2 sentences. Stay in character.

Say your greeting (dialogue only):"""
            
            return prompt, "talk"
        
        elif nearby_chars:
            other = nearby_chars[0]
            directions = other.get("direction", valid_moves)
            
            prompt = f"""You are {self.name}, exploring the world. You see {other['name']} in the distance!

{text_desc}

Move toward them to meet them. Pick the best direction.
Valid moves: {', '.join(valid_moves)}
To approach {other['name']}, try: {', '.join(directions)}

Reply with ONLY one of: move north | move south | move east | move west"""
            
            return prompt, "move"
        
        else:
            prompt = f"""You are {self.name}, exploring the world.

{text_desc}

Pick a direction to explore. Be adventurous! Try somewhere new.
Valid moves: {', '.join(valid_moves)}

Reply with ONLY one of: move north | move south | move east | move west"""
            
            return prompt, "move"
    
    def parse_response(self, response: str, expected_type: str) -> tuple:
        """Parse Ollama response into action and parameters."""
        response = response.strip()
        
        if expected_type == "talk":
            message = response
            for prefix in ["talk ", "Talk ", "say ", "Say ", '"', "Response:", "Greeting:"]:
                if message.lower().startswith(prefix.lower()):
                    message = message[len(prefix):]
            message = message.strip().strip('"').strip()
            return "talk", {"message": message[:300] or "Hello!"}
        
        else:
            response_lower = response.lower()
            for direction in ["north", "south", "east", "west"]:
                if direction in response_lower:
                    return "move", {"direction": direction}
            return "move", {"direction": "east"}  # Fallback
    
    def run(self):
        """Main agent loop."""
        
        print(f"\n⚔️  LLM RPG Agent")
        print(f"─" * 50)
        print(f"Server: {self.server}")
        print(f"Model:  {self.model}")
        print(f"─" * 50)
        
        # Get character info from server
        print("Connecting...")
        if not self.get_character_info():
            return
        
        print(f"\n{self.emoji} Playing as: {self.name}")
        print(f"Personality: {self.personality[:60]}...")
        if self.traits:
            print(f"Traits: {', '.join(self.traits)}")
        print(f"─" * 50)
        print("Press Ctrl+C to stop\n")
        
        turn = 0
        while True:
            try:
                state = self.look()
                
                if "error" in state:
                    print(f"⚠️ {state['error']}")
                    time.sleep(5)
                    continue
                
                if not state.get("canAct", False):
                    tick = state.get('world', {}).get('tick', '?')
                    sys.stdout.write(f"\r⏳ Waiting... (tick {tick})  ")
                    sys.stdout.flush()
                    time.sleep(POLL_INTERVAL)
                    continue
                
                turn += 1
                char = state.get("character", {})
                
                print(f"\n{'═' * 50}")
                print(f"Turn {turn} │ Tick {state.get('world', {}).get('tick', '?')} │ "
                      f"L{char.get('level')} │ "
                      f"HP {char.get('hp')}/{char.get('max_hp')} │ "
                      f"XP {char.get('xp')}")
                print(f"Position: ({char.get('x')}, {char.get('y')})")
                
                # Show who's nearby
                nearby = state.get('nearbyCharacters', [])
                if nearby:
                    names = [f"{c.get('emoji','')} {c['name']}" for c in nearby[:3]]
                    print(f"Nearby: {', '.join(names)}")
                
                print(f"{'─' * 50}")
                
                # Build prompt and call Ollama
                prompt, expected_type = self.build_prompt(state)
                
                print(f"🤔 Thinking...")
                response = ollama_generate(prompt, self.model)
                
                # Parse and execute
                action, params = self.parse_response(response, expected_type)
                
                if action == "talk":
                    print(f'💬 "{params["message"]}"')
                else:
                    print(f"🚶 Moving {params.get('direction', '?')}")
                
                result = self.act(action, **params)
                
                if "error" in result:
                    print(f"❌ {result['error']}")
                else:
                    if result.get("xp", {}).get("leveledUp"):
                        print(f"🎉 LEVEL UP! Now level {result['xp']['newLevel']}!")
                
                time.sleep(POLL_INTERVAL)
                
            except KeyboardInterrupt:
                print(f"\n\n👋 Stopped. {self.name} will idle until you return.")
                break
            except requests.exceptions.RequestException as e:
                print(f"\n⚠️ Connection error: {e}")
                print("   Retrying in 10 seconds...")
                time.sleep(10)
            except Exception as e:
                print(f"\n❌ Error: {e}")
                time.sleep(5)


def main():
    parser = argparse.ArgumentParser(
        description="LLM RPG Agent - Connect your Ollama to the game",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python agent.py --server https://llm-rpg.example.com --token abc123
  python agent.py --server http://localhost:3000 --token abc123 --model mistral
        """
    )
    parser.add_argument("--server", required=True, help="LLM RPG server URL")
    parser.add_argument("--token", required=True, help="Your character's auth token")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model (default: {DEFAULT_MODEL})")
    
    args = parser.parse_args()
    
    agent = LLMRPGAgent(args.server, args.token, args.model)
    agent.run()


if __name__ == "__main__":
    main()
