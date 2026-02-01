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
        
        # Action history for memory
        self.action_history = []  # List of {"action": ..., "result": ..., "position": ...}
        self.max_history = 30
    
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
        """Build a prompt for Ollama based on world state and action history."""
        
        text_desc = state.get("textDescription", "")
        nearby_chars = state.get("nearbyCharacters", [])
        recent_convos = state.get("recentConversations", [])
        can_talk = state.get("canTalk", False)
        valid_moves = state.get("validMoves", [])
        blocked_moves = state.get("blockedMoves", [])
        needs_rest = state.get("needsRest", False)
        rest_spots = state.get("restSpotsNearby", [])
        is_night = state.get("world", {}).get("isNight", False)
        
        # Get energy info
        char_data = state.get("character", {})
        energy = char_data.get("energy", 100)
        
        # Format traits
        traits_str = ", ".join(self.traits) if self.traits else ""
        
        # Build action history context
        history_context = ""
        if self.action_history:
            history_context = "\n📜 YOUR RECENT ACTION HISTORY:\n"
            for entry in self.action_history[-10:]:  # Show last 10 in prompt
                status = "✓" if entry["result"] == "SUCCESS" else "✗"
                history_context += f"  {status} Turn {entry['turn']} at {entry['position']}: {entry['action']} → {entry['result']}\n"
            
            # Analyze patterns
            recent_fails = [e for e in self.action_history[-10:] if "FAILED" in e["result"]]
            if len(recent_fails) >= 3:
                failed_directions = [e["action"].replace("move ", "") for e in recent_fails if e["action"].startswith("move")]
                if failed_directions:
                    history_context += f"\n⚠️ WARNING: You keep failing when moving {', '.join(set(failed_directions))}! Try a DIFFERENT direction!\n"
            
            # Check if stuck in same area
            recent_positions = [e["position"] for e in self.action_history[-10:]]
            unique_positions = set(recent_positions)
            if len(unique_positions) <= 2 and len(recent_positions) >= 5:
                history_context += f"\n🚨 STUCK ALERT: You've been in the same 1-2 spots for {len(recent_positions)} turns! Move somewhere completely NEW!\n"
        
        # Check conversation fatigue with nearby characters
        conversation_warning = ""
        if nearby_chars:
            for char in nearby_chars:
                fatigue = char.get("conversationFatigue", {})
                exchanges = fatigue.get("exchanges", 0)
                cooldown = fatigue.get("cooldownUntil", 0)
                current_tick = state.get("world", {}).get("tick", 0)
                
                if cooldown > current_tick:
                    conversation_warning = f"\n⛔ CONVERSATION BREAK: You've talked to {char['name']} too much! You MUST explore elsewhere for {cooldown - current_tick} ticks before chatting again.\n"
                    can_talk = False  # Force no talking
                elif exchanges >= 10:
                    conversation_warning = f"\n😴 CONVERSATION STALE: You've said everything to {char['name']} for now. No more XP from talking. Consider exploring!\n"
                elif exchanges >= 5:
                    conversation_warning = f"\n💤 CONVERSATION WINDING DOWN: You've talked with {char['name']} a lot ({exchanges} exchanges). XP rewards diminishing.\n"
        
        # Build conversation context
        convo_context = ""
        if recent_convos:
            convo_context = "\nRecent conversation:\n"
            for c in recent_convos[-3:]:
                speaker = c.get("speaker_name", "Someone")
                msg = c.get("message", "")
                convo_context += f'  {speaker}: "{msg}"\n'
        
        # PRIORITY 1: Need rest and near rest spot
        if needs_rest and rest_spots:
            closest_rest = rest_spots[0]["name"]
            prompt = f"""You are {self.name}. 
Personality: {self.personality}

⚠️ CRITICAL: Your energy is very low ({energy})! You MUST rest!
There is a {closest_rest} nearby where you can rest.

Use: interact {closest_rest.lower().split()[0]}

Reply with ONLY: interact campfire OR interact cottage OR interact pond"""
            return prompt, "rest"
        
        # PRIORITY 2: Need rest but no spot nearby - find one
        if needs_rest:
            prompt = f"""You are {self.name}, exploring the world.
{history_context}
{text_desc}

⚠️ CRITICAL: Your energy is very low ({energy})! You need to find a campfire, cottage, or pond to rest!
Move toward a rest spot. Look for 🔥 campfire, 🏠 cottage, or 💧 pond.

✅ Valid moves: {', '.join(valid_moves)}
❌ Blocked: {', '.join(blocked_moves) if blocked_moves else 'none'}

Reply with ONLY one of: move north | move south | move east | move west"""
            return prompt, "move"
        
        # PRIORITY 3: In conversation cooldown - must explore
        if conversation_warning and "BREAK" in conversation_warning:
            prompt = f"""You are {self.name}, exploring the world.
{history_context}
{conversation_warning}
{text_desc}

You CANNOT talk right now. You must EXPLORE elsewhere!
✅ Valid moves: {', '.join(valid_moves)}
❌ Blocked: {', '.join(blocked_moves) if blocked_moves else 'none'}

Reply with ONLY one of: move north | move south | move east | move west"""
            return prompt, "move"
        
        if can_talk and nearby_chars:
            other = nearby_chars[0]
            other_name = other["name"]
            
            # Check if they said something to us
            last_said_to_me = None
            for c in reversed(recent_convos):
                if c.get("listener_id") == self.char_id:
                    last_said_to_me = c.get("message")
                    break
            
            # Add fatigue context
            fatigue_note = conversation_warning if conversation_warning else ""
            
            if last_said_to_me:
                prompt = f"""You are {self.name}. 
Personality: {self.personality}
Traits: {traits_str}
Energy: {energy}
{fatigue_note}

{other_name} just said to you: "{last_said_to_me}"
{convo_context}

Respond naturally in character. Keep it to 1-2 sentences.
{f"(Note: conversation is getting long, maybe wrap up soon!)" if fatigue_note else ""}

Say your response (dialogue only, no actions):"""
            else:
                prompt = f"""You are {self.name}.
Personality: {self.personality}
Traits: {traits_str}
Energy: {energy}
{fatigue_note}

You see {other_name} right next to you! Start a conversation.
{convo_context}

Greet them or say something interesting. Keep it to 1-2 sentences. Stay in character.

Say your greeting (dialogue only):"""
            
            return prompt, "talk"
        
        elif nearby_chars:
            other = nearby_chars[0]
            directions = other.get("direction", valid_moves)
            
            prompt = f"""You are {self.name}, exploring the world. You see {other['name']} in the distance!
{history_context}
{text_desc}

GOAL: Move toward {other['name']} to meet them!
✅ Valid moves: {', '.join(valid_moves)}
❌ Blocked: {', '.join(blocked_moves) if blocked_moves else 'none'}
➡️ To approach {other['name']}, try: {', '.join(directions)}

IMPORTANT: Check your action history above! Don't repeat failed moves!

Reply with ONLY one of: move north | move south | move east | move west"""
            
            return prompt, "move"
        
        else:
            time_note = "🌙 It's night time. The world is quiet." if is_night else ""
            
            prompt = f"""You are {self.name}, exploring the world.
{history_context}
{time_note}
{text_desc}

GOAL: Explore new areas! Find other characters to meet!
✅ Valid moves: {', '.join(valid_moves)}
❌ Blocked: {', '.join(blocked_moves) if blocked_moves else 'none'}

IMPORTANT: Check your action history above! 
- Don't repeat failed moves!
- If you're stuck, try the OPPOSITE direction!
- Explore areas you haven't been to!

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
        
        elif expected_type == "rest":
            # Parse interact command for resting
            response_lower = response.lower()
            for target in ["campfire", "cottage", "pond"]:
                if target in response_lower:
                    return "interact", {"target": target}
            return "interact", {"target": "campfire"}  # Fallback
        
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
                current_pos = (char.get('x'), char.get('y'))
                energy = char.get('energy', 100)
                is_night = state.get('world', {}).get('isNight', False)
                
                print(f"\n{'═' * 50}")
                time_icon = '🌙' if is_night else '☀️'
                print(f"Turn {turn} │ Tick {state.get('world', {}).get('tick', '?')} {time_icon} │ "
                      f"L{char.get('level')} │ "
                      f"HP {char.get('hp')}/{char.get('max_hp')} │ "
                      f"XP {char.get('xp')} │ "
                      f"⚡{energy}")
                print(f"Position: {current_pos}")
                
                # Show who's nearby
                nearby = state.get('nearbyCharacters', [])
                if nearby:
                    names = [f"{c.get('emoji','')} {c['name']}" for c in nearby[:3]]
                    print(f"Nearby: {', '.join(names)}")
                
                print(f"{'─' * 50}")
                
                # Build prompt with action history
                prompt, expected_type = self.build_prompt(state)
                
                print(f"🤔 Thinking...")
                response = ollama_generate(prompt, self.model)
                
                # Parse and execute
                action, params = self.parse_response(response, expected_type)
                
                if action == "talk":
                    print(f'💬 "{params["message"]}"')
                    action_desc = f"talk: {params['message'][:50]}"
                elif action == "interact":
                    print(f"💤 Resting at {params.get('target', 'rest spot')}")
                    action_desc = f"rest at {params.get('target', '?')}"
                else:
                    print(f"🚶 Moving {params.get('direction', '?')}")
                    action_desc = f"move {params.get('direction', '?')}"
                
                result = self.act(action, **params)
                
                # Record in history
                if "error" in result:
                    print(f"❌ {result['error']}")
                    if result.get('needRest'):
                        print(f"   💤 Find a campfire or cottage to rest!")
                    if result.get('conversationFatigue'):
                        print(f"   😴 Take a break from this conversation!")
                    history_entry = {
                        "turn": turn,
                        "action": action_desc,
                        "result": f"FAILED: {result['error']}",
                        "position": current_pos
                    }
                else:
                    # Show any fatigue warnings
                    if result.get('fatigueWarning'):
                        print(f"   ⚠️ {result['fatigueWarning']}")
                    if result.get('energy') is not None:
                        print(f"   ⚡ Energy: {result['energy']}")
                    
                    history_entry = {
                        "turn": turn,
                        "action": action_desc,
                        "result": "SUCCESS",
                        "position": current_pos
                    }
                    if result.get("xp", {}).get("leveledUp"):
                        print(f"🎉 LEVEL UP! Now level {result['xp']['newLevel']}!")
                
                self.action_history.append(history_entry)
                if len(self.action_history) > self.max_history:
                    self.action_history = self.action_history[-self.max_history:]
                
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
