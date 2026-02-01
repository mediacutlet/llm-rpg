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
        
        # Conversation memory - track what WE said to avoid repetition
        self.conversation_memory = {}  # {other_char_id: [list of our messages]}
        self.max_convo_memory = 20  # Remember last 20 things we said to each person
        
        # Topic tracking
        self.discussed_topics = {}  # {other_char_id: set of topic keywords}
        
        # Force leave after goodbye
        self.must_leave = False
        self.leaving_from = None  # ID of character we said goodbye to
        
        # Post-goodbye cooldown - don't talk to same person for X turns
        self.goodbye_cooldown = {}  # {char_id: turns_remaining}
    
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
                print(f"❌ Error fetching character: {r.status_code}")
                print(r.text)
                return False
        except Exception as e:
            print(f"❌ Connection error: {e}")
            return False
    
    def remember_our_message(self, other_id: str, message: str):
        """Track what we said to avoid repetition."""
        if other_id not in self.conversation_memory:
            self.conversation_memory[other_id] = []
        
        self.conversation_memory[other_id].append(message)
        
        # Keep only last N messages
        if len(self.conversation_memory[other_id]) > self.max_convo_memory:
            self.conversation_memory[other_id] = self.conversation_memory[other_id][-self.max_convo_memory:]
        
        # Extract and track topics
        self.extract_topics(other_id, message)
    
    def extract_topics(self, other_id: str, message: str):
        """Extract key topics from a message to avoid repetition."""
        if other_id not in self.discussed_topics:
            self.discussed_topics[other_id] = set()
        
        # Simple keyword extraction
        keywords = ['coffee', 'quantum', 'glitch', 'code', 'debug', 'universe', 
                   'loop', 'firewall', 'static', 'rewrite', 'chaos', 'paradox',
                   'screen', 'firmware', 'syntax', 'reboot', 'matrix', 'system']
        
        message_lower = message.lower()
        for kw in keywords:
            if kw in message_lower:
                self.discussed_topics[other_id].add(kw)
    
    def get_conversation_context(self, other_id: str, other_name: str) -> str:
        """Build context about our conversation history to avoid repetition."""
        context = ""
        
        # What we've already said
        if other_id in self.conversation_memory and self.conversation_memory[other_id]:
            recent = self.conversation_memory[other_id][-5:]  # Last 5 things we said
            context += f"\n🚫 THINGS YOU ALREADY SAID TO {other_name.upper()} (DO NOT REPEAT THESE):\n"
            for msg in recent:
                # Truncate for prompt space
                context += f'  - "{msg[:80]}..."\n'
        
        # Topics we've covered
        if other_id in self.discussed_topics and self.discussed_topics[other_id]:
            topics = list(self.discussed_topics[other_id])
            context += f"\n📝 TOPICS ALREADY DISCUSSED: {', '.join(topics)}\n"
            context += "→ Try a COMPLETELY DIFFERENT topic! Ask about their past, their dreams, the world around you, etc.\n"
        
        return context
    
    def suggest_new_topics(self, other_id: str) -> str:
        """Suggest topics we haven't discussed yet."""
        all_topics = [
            'your past', 'your dreams', 'this place', 'where you came from',
            'what you seek', 'favorite memory', 'fears', 'hopes', 
            'the weather', 'nearby landmarks', 'other travelers',
            'food', 'music', 'stories', 'adventures', 'home'
        ]
        
        discussed = self.discussed_topics.get(other_id, set())
        
        # Filter out topics similar to what we've discussed
        available = []
        for topic in all_topics:
            # Check if any discussed keyword is in this topic
            dominated = False
            for kw in discussed:
                if kw in topic.lower():
                    dominated = True
                    break
            if not dominated:
                available.append(topic)
        
        if not available:
            available = ['something completely unexpected', 'a random observation', 'saying goodbye']
        
        import random
        random.shuffle(available)
        return ', '.join(available[:3])
    
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
                exchanges = int(fatigue.get("exchanges", 0) or 0)
                cooldown = int(fatigue.get("cooldownUntil", 0) or 0)
                current_tick = int(state.get("world", {}).get("tick", 0) or 0)
                
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
            return prompt, "rest", None
        
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
            return prompt, "move", None
        
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
            return prompt, "move", None
        
        # PRIORITY 4: Must leave after saying goodbye (force 5 moves away)
        if self.must_leave:
            # Set cooldown so we don't re-engage immediately
            if self.leaving_from:
                self.goodbye_cooldown[self.leaving_from] = 8  # 8 turns before we can talk to them again
            self.must_leave = False
            self.leaving_from = None
            
            prompt = f"""You are {self.name}, exploring the world.
{history_context}
{text_desc}

You just said goodbye to someone. Time to explore NEW areas far away!
Pick a direction and KEEP GOING that way.
✅ Valid moves: {', '.join(valid_moves)}
❌ Blocked: {', '.join(blocked_moves) if blocked_moves else 'none'}

Reply with ONLY one of: move north | move south | move east | move west"""
            return prompt, "move", None
        
        # Decrement all goodbye cooldowns
        for char_id in list(self.goodbye_cooldown.keys()):
            self.goodbye_cooldown[char_id] -= 1
            if self.goodbye_cooldown[char_id] <= 0:
                del self.goodbye_cooldown[char_id]
                # Also clear memory when cooldown expires so next meeting feels fresh
                if char_id in self.conversation_memory:
                    self.conversation_memory[char_id] = []
                    self.discussed_topics[char_id] = set()
        
        if can_talk and nearby_chars:
            other = nearby_chars[0]
            other_name = other["name"]
            other_id = other.get("id", other_name)
            
            # Check if we're in goodbye cooldown with this person
            if other_id in self.goodbye_cooldown:
                remaining = self.goodbye_cooldown[other_id]
                prompt = f"""You are {self.name}, exploring the world.
{history_context}
{text_desc}

You recently said goodbye to {other_name}. Keep exploring elsewhere for now!
✅ Valid moves: {', '.join(valid_moves)}
❌ Blocked: {', '.join(blocked_moves) if blocked_moves else 'none'}

Reply with ONLY one of: move north | move south | move east | move west"""
                return prompt, "move", None
            
            # Build actual conversation history from server
            convo_history = ""
            if recent_convos:
                convo_history = "\n💬 RECENT CONVERSATION:\n"
                for c in recent_convos[-6:]:  # Last 6 messages for context
                    speaker = c.get("speaker_name", "Someone")
                    msg = c.get("message", "")[:200]
                    if speaker == self.name:
                        convo_history += f'  YOU: "{msg}"\n'
                    else:
                        convo_history += f'  {speaker}: "{msg}"\n'
            
            # Find what they JUST said to us
            last_said_to_me = None
            for c in reversed(recent_convos):
                if c.get("listener_id") == self.char_id:
                    last_said_to_me = c.get("message")
                    break
            
            # Check if they said goodbye - we should acknowledge and leave too
            if last_said_to_me:
                goodbye_phrases = ['goodbye', 'farewell', 'adieu', 'bye', 'see you', 'until next', 'take care', 'been delightful', 'bid you', 'should go', 'must go', 'heading off']
                said_goodbye = any(phrase in last_said_to_me.lower() for phrase in goodbye_phrases)
                if said_goodbye:
                    # They said goodbye - acknowledge it and leave
                    prompt = f"""You are {self.name}.
{other_name} is saying goodbye: "{last_said_to_me[:100]}"

Say goodbye back warmly. Use "goodbye", "farewell", or "take care". One sentence only:"""
                    return prompt, "goodbye", other_id
            
            # Count our local exchanges for goodbye logic
            our_messages = self.conversation_memory.get(other_id, [])
            exchange_count = len(our_messages)
            
            # After 12 exchanges, say goodbye
            if exchange_count >= 12:
                prompt = f"""You are {self.name}.
You've been talking to {other_name} for a while and it's time to explore elsewhere.

Say goodbye to them. Be warm but clear that you're leaving. Use a phrase like "goodbye", "farewell", "I should go", or "take care". One sentence only:"""
                return prompt, "goodbye", other_id
            
            # Get topics we've covered to suggest variety
            discussed = self.discussed_topics.get(other_id, set())
            topic_hint = ""
            if len(discussed) > 3:
                unused = [t for t in ['your past', 'dreams', 'fears', 'home', 'adventures', 'food', 'music'] 
                         if t not in ' '.join(discussed)]
                if unused:
                    topic_hint = f"\n(Maybe ask about: {', '.join(unused[:2])})"
            
            if last_said_to_me:
                # Check if they asked a question
                asked_question = '?' in last_said_to_me
                
                prompt = f"""You are {self.name}. 
Personality: {self.personality[:200]}
Traits: {traits_str}
{convo_history}

{other_name} just said: "{last_said_to_me}"

{"They asked you a question - ANSWER IT FIRST with a specific response, then you can add your own thought." if asked_question else "React to what they shared, then continue the conversation."}

IMPORTANT:
- Actually ANSWER if they asked something (don't just ask another question back)
- Share something SPECIFIC about yourself or your experiences  
- Keep it to 2-3 sentences, just dialogue, no *asterisk actions*{topic_hint}

Your response:"""
            else:
                prompt = f"""You are {self.name}.
Personality: {self.personality[:200]}
Traits: {traits_str}
{convo_history}

You see {other_name} next to you. {"Continue the conversation." if convo_history else "Start a conversation."}

- Share something interesting about yourself or ask them about their experiences
- Keep it to 2-3 sentences, just dialogue, no *asterisk actions*{topic_hint}

What you say:"""
            
            return prompt, "talk", other_id
        
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
            
            return prompt, "move", None
        
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
            
            return prompt, "move", None
    
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
        
        elif expected_type == "goodbye":
            # Parse as a talk action but flag for leaving
            message = response
            for prefix in ["talk ", "Talk ", "say ", "Say ", '"', "Farewell:", "Goodbye:"]:
                if message.lower().startswith(prefix.lower()):
                    message = message[len(prefix):]
            message = message.strip().strip('"').strip()
            if not message or len(message) < 3:
                message = "It was nice talking! I should explore more. Goodbye!"
            return "talk", {"message": message[:200], "is_goodbye": True}
        
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
                prompt, expected_type, talk_target_id = self.build_prompt(state)
                
                print(f"🤔 Thinking...")
                response = ollama_generate(prompt, self.model)
                
                # Parse and execute
                action, params = self.parse_response(response, expected_type)
                
                if action == "talk":
                    message = params["message"]
                    is_goodbye = params.get("is_goodbye", False)
                    
                    if is_goodbye:
                        print(f'👋 "{message}"')
                        # Set flag to force movement on next turn
                        self.must_leave = True
                        self.leaving_from = talk_target_id
                    else:
                        print(f'💬 "{message}"')
                    
                    # Remember what we said to avoid repetition
                    if talk_target_id:
                        self.remember_our_message(talk_target_id, message)
                    action_desc = f"talk: {message[:50]}"
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
