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
import sys
import random
import re

DEFAULT_MODEL = "llama3"  # Change to your preferred model
POLL_INTERVAL = 2  # Seconds between checking if we can act


def ollama_generate(prompt: str, model: str, temperature: float = 0.8) -> str:
    """Call local Ollama API to generate a response with temperature control."""
    try:
        import requests
        response = requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_predict": 150  # Limit response length
                }
            },
            timeout=120
        )
        if response.status_code == 200:
            return response.json().get("response", "").strip()
        else:
            print(f"  ⚠️ Ollama API error: {response.status_code}")
            return "move east"
    except requests.exceptions.ConnectionError:
        print("  ❌ Cannot connect to Ollama. Is it running? (ollama serve)")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print("  ⚠️ Ollama timeout, using fallback")
        return "move east"
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
        
        # CONVERSATION STATE MACHINE
        self.conversation_state = {}  # {char_id: "idle" | "waiting" | "talking"}
        self.last_seen_msg = {}  # {char_id: tick of last message we processed from them}
        self.waiting_since = {}  # {key: tick when we started waiting}
        
        # Track when we last talked to each character (for greetings)
        self.last_talked_tick = {}  # {char_id: tick}
        
        # Traveling state - keep moving in same direction after goodbye
        self.traveling_direction = None
        self.traveling_turns = 0
        
        # Track recently blocked directions to avoid repeating failed moves
        self.blocked_directions = {}  # {direction: tick_when_blocked}
        
        # Resting state - stay at rest spot until fully rested
        self.is_resting = False
        self.resting_at = None  # Name of rest spot
        
        # Eating state - stay at market until fully fed
        self.is_eating = False
    
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
    
    def send_think(self, about: str = ""):
        """Send a 'thinking' action to show thought bubble on frontend."""
        try:
            self.act("think", target=about)
        except Exception:
            pass  # Non-critical, don't fail if this errors
    
    def save_summary(self, other_id: str, title: str, summary: str, topics: list) -> bool:
        """Save a conversation summary to the server."""
        try:
            r = requests.post(
                f"{self.server}/api/summary/{self.char_id}",
                headers=self.headers,
                json={
                    "otherId": other_id,
                    "title": title,
                    "summary": summary,
                    "topics": topics
                },
                timeout=10
            )
            return r.status_code == 200
        except Exception as e:
            print(f"  ⚠️ Failed to save summary: {e}")
            return False
    
    def generate_summary(self, other_name: str, other_id: str, recent_convos: list) -> dict:
        """Ask LLM to summarize the conversation."""
        # Filter to only messages between us and the other character
        relevant_convos = []
        for c in recent_convos:
            speaker = c.get("speaker_name", "")
            listener_id = c.get("listener_id", "")
            speaker_id = c.get("speaker_id", "")
            
            # Include if: we said to them, or they said to us
            is_relevant = (speaker == self.name and listener_id == other_id) or \
                         (speaker == other_name and listener_id == self.char_id) or \
                         (speaker_id == other_id and listener_id == self.char_id) or \
                         (speaker_id == self.char_id and listener_id == other_id)
            
            if is_relevant:
                relevant_convos.append(c)
        
        # Need at least 4 back-and-forth messages for a meaningful summary
        if len(relevant_convos) < 4:
            return None
        
        # Build conversation text from last 12 relevant messages
        convo_text = ""
        for c in relevant_convos[-12:]:
            speaker = c.get("speaker_name", "Someone")
            msg = c.get("message", "")[:300]  # More context per message
            convo_text += f'{speaker}: "{msg}"\n'
        
        if not convo_text.strip():
            return None
        
        prompt = f"""You are summarizing a conversation between {self.name} and {other_name}.

THE CONVERSATION:
{convo_text}

Create a memory of this conversation. Write:
1. TITLE: A descriptive 4-6 word title capturing the main theme (not just "Conversation")
2. SUMMARY: 2-3 sentences describing what they discussed, any revelations, shared interests, or notable moments
3. TOPICS: 3-5 key topics or themes from the discussion

Format your response EXACTLY like this:
TITLE: [your title here]
SUMMARY: [your summary here]
TOPICS: [topic1, topic2, topic3]"""
        
        try:
            response = ollama_generate(prompt, self.model)
            
            # Parse response
            title = ""
            summary = ""
            topics = []
            
            for line in response.split('\n'):
                line = line.strip()
                if line.upper().startswith('TITLE:'):
                    title = line[6:].strip().strip('"').strip("'")
                elif line.upper().startswith('SUMMARY:'):
                    summary = line[8:].strip()
                elif line.upper().startswith('TOPICS:'):
                    topics = [t.strip() for t in line[7:].split(',') if t.strip()]
            
            # Validate we got meaningful content
            if not title or title.lower() in ['conversation', 'a conversation', 'chat']:
                title = f"Discussion with {other_name}"
            if not summary or len(summary) < 20:
                return None  # Not a real summary
            
            return {
                "title": title[:100],
                "summary": summary[:500],
                "topics": topics[:10]
            }
        except Exception as e:
            print(f"  ⚠️ Failed to generate summary: {e}")
            return None
    
    def build_prompt(self, state: dict) -> tuple:
        """Build a prompt for Ollama based on world state and action history."""
        
        text_desc = state.get("textDescription", "")
        recent_convos = state.get("recentConversations", [])
        can_talk = state.get("canTalk", False)
        valid_moves = state.get("validMoves", [])
        blocked_moves = state.get("blockedMoves", [])
        rest_spots = state.get("restSpotsNearby", [])
        food_spots = state.get("foodSpotsNearby", [])
        is_night = state.get("world", {}).get("isNight", False)
        current_tick = int(state.get("world", {}).get("tick", 0) or 0)
        
        # Filter nearby characters to ONLY include online characters (acted in last 10 ticks)
        all_nearby = state.get("nearbyCharacters", [])
        nearby_chars = []
        for nc in all_nearby:
            nc_last_action = int(nc.get("last_action_tick", 0) or 0)
            if current_tick - nc_last_action <= 10:
                nearby_chars.append(nc)
        
        # Environmental context
        current_location = state.get("currentLocation", "open meadow")
        nearby_objects = state.get("nearbyObjects", [])
        world_events = state.get("recentWorldEvents", [])
        
        # Build environment description
        env_context = ""
        if current_location and current_location != "open meadow":
            env_context = f"You are at: {current_location}. "
        
        # Add nearby notable features
        notable_features = [o.get("name") for o in nearby_objects[:3] if o.get("distance", 10) <= 3]
        if notable_features:
            env_context += f"Nearby: {', '.join(notable_features)}. "
        
        # Add time of day
        if is_night:
            env_context += "It's nighttime. "
        
        # Add recent world events
        if world_events:
            env_context += f"[{world_events[0]}] "
        
        # Get energy and hunger info
        char_data = state.get("character", {})
        energy = char_data.get("energy", 100)
        hunger = char_data.get("hunger", 100)
        current_tick = int(state.get("world", {}).get("tick", 0) or 0)
        
        # Clear old blocked directions (blocked more than 5 ticks ago = we've probably moved)
        for direction in list(self.blocked_directions.keys()):
            if current_tick - self.blocked_directions[direction] > 5:
                del self.blocked_directions[direction]
        
        # Filter valid_moves to prefer non-recently-blocked directions
        unblocked_moves = [d for d in valid_moves if d not in self.blocked_directions]
        if not unblocked_moves:
            # All directions are blocked, reset and try again
            self.blocked_directions = {}
            unblocked_moves = valid_moves
        
        # Use unblocked_moves as our preferred valid moves
        preferred_moves = unblocked_moves if unblocked_moves else valid_moves
        
        # Check needs (from server or local thresholds)
        needs_rest = energy < 40 or state.get("needsRest", False)
        needs_food = hunger < 40 or state.get("needsFood", False)
        max_energy = char_data.get("max_energy", 100)
        max_hunger = char_data.get("max_hunger", 100)
        
        # If we're eating, keep eating until hunger is FULL
        if self.is_eating:
            if hunger >= max_hunger:
                # Fully fed! Stop eating
                print(f"   🍖 Fully fed! Hunger: {hunger}/{max_hunger}")
                self.is_eating = False
            elif food_spots:
                closest_food = food_spots[0]
                closest_dist = closest_food.get("distance", 10)
                if closest_dist <= 2.5:
                    # Still at market - keep eating
                    return None, "eat", None  # Direct eat action
                else:
                    # Somehow moved away from market
                    self.is_eating = False
        
        # If we're resting, keep resting until energy is FULL
        if self.is_resting:
            if energy >= max_energy:
                # Fully rested! Stop resting
                print(f"   💪 Fully rested! Energy: {energy}/{max_energy}")
                self.is_resting = False
                self.resting_at = None
            elif rest_spots:
                # Keep resting
                closest_rest = rest_spots[0]
                closest_dist = closest_rest.get("distance", 10)
                if closest_dist <= 2.5:
                    # Still at rest spot - keep resting
                    return None, "rest", None  # Direct rest action
                else:
                    # Somehow moved away from rest spot
                    self.is_resting = False
                    self.resting_at = None
        
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
        
        # Check conversation fatigue with nearby characters (just for display info)
        conversation_warning = ""
        if nearby_chars:
            for char in nearby_chars:
                fatigue = char.get("conversationFatigue", {})
                exchanges = int(fatigue.get("exchanges", 0) or 0)
                cooldown = int(fatigue.get("cooldownUntil", 0) or 0)
                current_tick = int(state.get("world", {}).get("tick", 0) or 0)
                
                if cooldown > current_tick:
                    # Server has cooldown, but DON'T override can_talk here
                    # Let the server reject the action if we try to talk
                    # This prevents the "approach adjacent person" bug
                    conversation_warning = f"\n⛔ Note: Server cooldown active ({cooldown - current_tick} ticks remaining)\n"
                elif exchanges >= 10:
                    conversation_warning = f"\n😴 Conversation getting stale with {char['name']}.\n"
                elif exchanges >= 5:
                    conversation_warning = f"\n💤 ({exchanges} exchanges so far)\n"
        
        # Build conversation context
        convo_context = ""
        if recent_convos:
            convo_context = "\nRecent conversation:\n"
            for c in recent_convos[-6:]:  # Last 6 messages for better context
                speaker = c.get("speaker_name", "Someone")
                msg = c.get("message", "")
                convo_context += f'  {speaker}: "{msg}"\n'
        
        # Get current tick for timing checks
        current_tick = int(state.get("world", {}).get("tick", 0) or 0)
        
        # PRIORITY 0: If we're next to someone and REALLY need to leave, say goodbye first
        # (Only for urgent needs - otherwise let conversations flow naturally)
        if can_talk and nearby_chars and not self.must_leave:
            other = nearby_chars[0]
            other_name = other["name"]
            other_id = other.get("id", other_name)
            
            # Skip if already in cooldown (already said goodbye)
            if other_id not in self.goodbye_cooldown:
                # Use LOCAL memory for THIS session's exchange count
                # Server data is stale and persists across sessions
                local_messages = self.conversation_memory.get(other_id, [])
                exchange_count = len(local_messages)
                
                # Reasons to leave:
                # - Critically low energy (<30) or hunger (<25) - urgent, leave after 3 exchanges  
                # - Moderately low energy (<40) or hunger (<35) - leave after 6 exchanges
                # - Had 10+ exchanges - natural conversation endpoint
                urgent_need = (energy < 30 or hunger < 25) and exchange_count >= 3
                moderate_need = (energy < 40 or hunger < 35) and exchange_count >= 6
                talked_enough = exchange_count >= 10
                
                should_leave = urgent_need or moderate_need or talked_enough
                
                if should_leave:
                    if energy < 40:
                        reason = "need to find somewhere to rest"
                    elif hunger < 35:
                        reason = "need to get some food"
                    else:
                        reason = "should explore more of the world"
                    
                    prompt = f"""You are {self.name}.
You've {reason} and want to say goodbye to {other_name}.

Say goodbye warmly in one sentence. Use "goodbye", "farewell", "I should go", or "take care":"""
                    return prompt, "goodbye", other_id
        
        # PRIORITY 1: Need food and near market (within interaction range) - only if alone
        if needs_food and food_spots and not (can_talk and nearby_chars):
            closest_food = food_spots[0]
            closest_name = closest_food["name"]
            closest_dist = closest_food.get("distance", 10)
            
            if closest_dist <= 2.5:
                # Close enough to interact - start eating (will continue until full)
                self.is_eating = True
                prompt = f"""You are {self.name}. 

🍖 You're hungry ({hunger})! There's a {closest_name} right here.
Eat something to restore your hunger and get some energy!

Reply with ONLY: interact market"""
                return prompt, "eat", None
            else:
                # Need to move closer to market
                directions = closest_food.get("direction", valid_moves)
                safe_dirs = [d for d in directions if d in preferred_moves]
                dir_hint = safe_dirs[0] if safe_dirs else (directions[0] if directions else "south")
                prompt = f"""You are {self.name}, exploring the world.
{text_desc}

🍖 You're getting hungry ({hunger})! There's a {closest_name} nearby ({closest_dist:.0f} tiles away).
Move {dir_hint.upper()} toward it to eat!

Reply with ONLY: move {dir_hint}"""
                return prompt, "move", None
        
        # PRIORITY 2: Need rest and near rest spot (within interaction range) - only if alone
        if needs_rest and rest_spots and not (can_talk and nearby_chars):
            closest_rest = rest_spots[0]
            closest_name = closest_rest["name"]
            closest_dist = closest_rest.get("distance", 10)
            
            if closest_dist <= 2.5:
                # Close enough to interact - start resting (will continue until full)
                self.is_resting = True
                self.resting_at = closest_name
                prompt = f"""You are {self.name}. 
Personality: {self.personality}

⚠️ CRITICAL: Your energy is very low ({energy})! You MUST rest!
There is a {closest_name} right here where you can rest.

Use: interact {closest_name.lower().split()[0]}

Reply with ONLY: interact campfire OR interact cottage OR interact pond"""
                return prompt, "rest", None
            else:
                # Need to move closer to rest spot
                directions = closest_rest.get("direction", valid_moves)
                dir_hint = directions[0] if directions else "south"
                prompt = f"""You are {self.name}, exploring the world.
{text_desc}

⚠️ Your energy is low ({energy})! There's a {closest_name} nearby ({closest_dist:.0f} tiles away).
Move {dir_hint.upper()} toward it to rest!

Reply with ONLY: move {dir_hint}"""
                return prompt, "move", None
        
        # PRIORITY 3: Need rest but no spot nearby (and not talking to someone) - find one
        if needs_rest and not (can_talk and nearby_chars):
            prompt = f"""You are {self.name}, exploring the world.
{history_context}
{text_desc}

⚠️ Your energy is low ({energy})! You need to find a campfire, cottage, or pond to rest!
Move toward a rest spot. Look for 🔥 campfire, 🏠 cottage, or 💧 pond.

✅ Valid moves: {', '.join(valid_moves)}

Reply with ONLY one of: move north | move south | move east | move west"""
            return prompt, "move", None
        
        # PRIORITY 4: Need food but no market nearby (and not talking) - find the market
        if needs_food and not food_spots and not (can_talk and nearby_chars):
            prompt = f"""You are {self.name}, exploring the world.
{history_context}
{text_desc}

🍖 You're getting hungry ({hunger})! You need to find the Market to eat!
Look for 🏪 Market - it's usually near the center of the meadow.

✅ Valid moves: {', '.join(valid_moves)}

Reply with ONLY one of: move north | move south | move east | move west"""
            return prompt, "move", None
        
        # PRIORITY 5: Must leave after saying goodbye
        if self.must_leave:
            # Set cooldown so we don't re-engage immediately
            if self.leaving_from:
                self.goodbye_cooldown[self.leaving_from] = 30  # 30 turns before we can talk to them again
            self.must_leave = False
            self.leaving_from = None
            
            # Pick a random direction and commit to it
            # random imported at top
            if valid_moves:
                self.traveling_direction = random.choice(valid_moves)
                self.traveling_turns = 10  # Keep going this direction for 10 turns
            
            prompt = f"""You are {self.name}, exploring the world.
{history_context}
{text_desc}

You just said goodbye. Time to explore far away!
Go {self.traveling_direction.upper()} to find new places!
✅ Valid moves: {', '.join(valid_moves)}

Reply with ONLY: move {self.traveling_direction}"""
            return prompt, "move", None
        
        # PRIORITY 5: Currently traveling away from someone
        # BUT if someone new spoke to us, cancel traveling to respond
        if self.traveling_turns > 0:
            # Check if anyone spoke to us recently (worth stopping for)
            current_tick = int(state.get("world", {}).get("tick", 0) or 0)
            someone_spoke = False
            for c in reversed(recent_convos[-5:]):
                if c.get("listener_id") == self.char_id:
                    msg_tick = int(c.get("tick", 0) or 0)
                    speaker_id = c.get("speaker_id")
                    # Someone spoke to us in the last 5 ticks AND they're not in cooldown
                    if current_tick - msg_tick <= 5 and speaker_id not in self.goodbye_cooldown:
                        someone_spoke = True
                        print(f"   📨 Someone spoke to us while traveling, stopping to respond")
                        self.traveling_turns = 0
                        break
            
            if self.traveling_turns > 0:  # Still traveling
                self.traveling_turns -= 1
                
                # Try to keep going same direction, or pick new one if blocked
                if self.traveling_direction in valid_moves:
                    direction = self.traveling_direction
                else:
                    # random imported at top
                    direction = random.choice(valid_moves) if valid_moves else "south"
                    self.traveling_direction = direction
                
                prompt = f"""You are {self.name}, exploring the world.
{text_desc}

Keep exploring! Go {direction.upper()}.
✅ Valid moves: {', '.join(valid_moves)}

Reply with ONLY: move {direction}"""
                return prompt, "move", None
        
        # PRIORITY 6: Portal travel - if standing on a portal
        current_portal = state.get("currentPortal")
        nearby_portals = state.get("nearbyPortals", [])
        zone = state.get("zone", {})
        
        if current_portal and not (can_talk and nearby_chars):
            # Standing on a portal with no one to talk to
            dest_name = current_portal.get("destinationName", "unknown")
            portal_name = current_portal.get("name", "portal")
            
            # Higher chance to travel if we've been exploring
            travel_chance = 0.4 if self.traveling_turns > 0 else 0.25
            
            if random.random() < travel_chance:
                print(f"   🚪 Traveling through {portal_name} to {dest_name}!")
                return None, "travel", None
        
        # PRIORITY 7: Move toward nearby portals occasionally (exploration)
        if nearby_portals and not nearby_chars and not current_portal:
            closest_portal = nearby_portals[0]
            if closest_portal.get("distance", 100) < 8 and random.random() < 0.15:
                # Move toward the portal
                directions = closest_portal.get("direction", valid_moves)
                if directions:
                    direction = directions[0] if isinstance(directions, list) else directions
                    print(f"   🧭 Curious about {closest_portal.get('name')} to {closest_portal.get('destination_name')}...")
                    return None, f"direct_move_{direction}", None
        
        # Decrement all goodbye cooldowns
        for char_id in list(self.goodbye_cooldown.keys()):
            self.goodbye_cooldown[char_id] -= 1
            if self.goodbye_cooldown[char_id] <= 0:
                del self.goodbye_cooldown[char_id]
                # Also clear memory when cooldown expires so next meeting feels fresh
                if char_id in self.conversation_memory:
                    self.conversation_memory[char_id] = []
                    self.discussed_topics[char_id] = set()
                # Clear conversation state
                if char_id in self.conversation_state:
                    del self.conversation_state[char_id]
                if char_id in self.last_seen_msg:
                    del self.last_seen_msg[char_id]
                self.waiting_since.pop(char_id, None)
                self.waiting_since.pop(f"init_{char_id}", None)
                # Clear last_talked so we greet them again
                if char_id in self.last_talked_tick:
                    del self.last_talked_tick[char_id]
        
        if can_talk and nearby_chars:
            current_tick = int(state.get("world", {}).get("tick", 0) or 0)
            current_time = time.time()
            
            # FIRST: Check if we're already in conversation with someone
            in_convo_with_id = None
            in_convo_state = None
            for char_id, state_val in self.conversation_state.items():
                if state_val in ("waiting", "talking"):
                    in_convo_with_id = char_id
                    in_convo_state = state_val
                    break
            
            # Check who spoke to us recently
            speaker_to_respond_to = None
            their_message_tick = 0
            for c in reversed(recent_convos[-10:]):
                if c.get("listener_id") == self.char_id:
                    speaker_id = c.get("speaker_id")
                    msg_tick = int(c.get("tick", 0) or 0)
                    if current_tick - msg_tick <= 10:
                        for nearby in nearby_chars:
                            if nearby.get("id") == speaker_id:
                                speaker_to_respond_to = nearby
                                their_message_tick = msg_tick
                                break
                    break
            
            # Decide who to talk to
            other = None
            
            # If we're already in conversation with someone...
            if in_convo_with_id:
                # Check if our conversation partner is still nearby
                convo_partner_nearby = None
                for nc in nearby_chars:
                    if nc.get("id") == in_convo_with_id:
                        convo_partner_nearby = nc
                        break
                
                if convo_partner_nearby:
                    # Continue with our existing conversation partner
                    other = convo_partner_nearby
                    
                    # If someone ELSE spoke to us, we need to ignore them (we're busy)
                    if speaker_to_respond_to and speaker_to_respond_to.get("id") != in_convo_with_id:
                        interrupter_name = speaker_to_respond_to.get("name", "someone")
                        print(f"   🙅 Ignoring {interrupter_name} - already in conversation with {other.get('name')}")
                else:
                    # Our conversation partner left - clear state and respond to new speaker
                    print(f"   ❌ Lost sight of {in_convo_with_id[:8]}..., clearing convo state")
                    self.conversation_state[in_convo_with_id] = "idle"
                    self.waiting_since.pop(in_convo_with_id, None)
                    
                    # Now we can respond to the new speaker
                    if speaker_to_respond_to:
                        other = speaker_to_respond_to
            
            # If we're NOT in a conversation, respond to whoever spoke to us
            elif speaker_to_respond_to:
                other = speaker_to_respond_to
            
            # If no one spoke to us and we're not in conversation, find someone to talk to
            if other is None:
                for nc in nearby_chars:
                    nc_id = nc.get("id", nc["name"])
                    nc_name = nc.get("name", nc_id)
                    if nc_id in self.goodbye_cooldown:
                        continue
                    # Check if they're online (acted in last 10 ticks)
                    nc_last_action = int(nc.get("last_action_tick", 0) or 0)
                    if current_tick - nc_last_action > 10:
                        continue  # Skip offline characters
                    
                    # Check if they're busy with someone else
                    nc_is_busy = False
                    for c in reversed(recent_convos[-10:]):
                        speaker_id = c.get("speaker_id")
                        listener_id = c.get("listener_id")
                        msg_tick = int(c.get("tick", 0) or 0)
                        if current_tick - msg_tick > 8:
                            continue  # Old message
                        # If they spoke to someone else recently
                        if speaker_id == nc_id and listener_id != self.char_id:
                            nc_is_busy = True
                            print(f"   🚶 Skipping {nc_name} - busy talking to someone else")
                            break
                    
                    if nc_is_busy:
                        continue
                    
                    other = nc
                    break
                
                # If no one is online and not in cooldown, just move around
                if other is None:
                    return None, "move", None
                
            other_name = other["name"]
            other_id = other.get("id", other_name)
            other_distance = other.get("distance", 10)
            
            # Get conversation state FIRST
            conv_state = self.conversation_state.get(other_id, "idle")
            last_seen_from_them = self.last_seen_msg.get(other_id, 0)
            
            # CRITICAL: If we've never tracked this person before, check for recent messages first
            if last_seen_from_them == 0:
                # Check if they spoke to us recently (last 10 ticks) - don't ignore those!
                recent_msg_from_them = 0
                for c in recent_convos:
                    if c.get("speaker_id") == other_id and c.get("listener_id") == self.char_id:
                        msg_tick = int(c.get("tick", 0) or 0)
                        if current_tick - msg_tick <= 15:
                            recent_msg_from_them = msg_tick
                            break
                
                if recent_msg_from_them > 0:
                    # They spoke to us recently - set last_seen to BEFORE that message
                    self.last_seen_msg[other_id] = recent_msg_from_them - 1
                    last_seen_from_them = recent_msg_from_them - 1
                    print(f"   🆕 First encounter with {other_name}, found recent message at tick {recent_msg_from_them}")
                else:
                    # No recent messages found - but don't be too aggressive!
                    # Set last_seen to 15 ticks ago so we can still catch messages
                    # that might arrive due to race conditions
                    safe_last_seen = max(0, current_tick - 15)
                    self.last_seen_msg[other_id] = safe_last_seen
                    last_seen_from_them = safe_last_seen
                    print(f"   🆕 First encounter with {other_name}, watching for messages since tick {safe_last_seen}")
            
            # Check for NEW message from them FIRST (before distance check)
            their_new_msg_tick = 0
            for c in recent_convos:
                if c.get("speaker_id") == other_id and c.get("listener_id") == self.char_id:
                    msg_tick = int(c.get("tick", 0) or 0)
                    if msg_tick > last_seen_from_them and msg_tick > their_new_msg_tick:
                        their_new_msg_tick = msg_tick
            
            # If we're WAITING and they responded, transition to talking
            if conv_state == "waiting" and their_new_msg_tick > 0:
                print(f"   ✅ {other_name} responded at tick {their_new_msg_tick}")
                self.last_seen_msg[other_id] = their_new_msg_tick
                self.conversation_state[other_id] = "talking"
                self.waiting_since.pop(other_id, None)
                conv_state = "talking"  # Continue to conversation logic
            
            # Must be adjacent (distance <= 1.5) to talk
            if other_distance > 1.5:
                # If we're WAITING for their response but they moved away, keep waiting
                if conv_state == "waiting":
                    wait_start = self.waiting_since.get(other_id, current_tick)
                    ticks_waiting = current_tick - wait_start
                    if ticks_waiting < 12:
                        print(f"   ⏳ WAITING for {other_name}'s response ({ticks_waiting}/12 ticks) - they're at dist={other_distance:.1f}")
                        time.sleep(3)
                        return None, "skip", None
                    else:
                        # Timeout - give up
                        print(f"   😤 TIMEOUT: {other_name} not responding (moved away)")
                        self.conversation_state[other_id] = "idle"
                        self.waiting_since.pop(other_id, None)
                        self.goodbye_cooldown[other_id] = 15
                        return None, "move", None
                
                # Not in conversation - approach them
                directions = other.get("direction", valid_moves)
                safe_directions = [d for d in directions if d in preferred_moves]
                if not safe_directions:
                    safe_directions = [d for d in directions if d in valid_moves]
                if not safe_directions:
                    safe_directions = preferred_moves if preferred_moves else valid_moves
                best_direction = safe_directions[0] if safe_directions else 'south'
                return None, f"direct_move_{best_direction}", None
            
            # ═══════════════════════════════════════════════════════════════
            # CONVERSATION STATE MACHINE (continued from above)
            # At this point: we're adjacent (dist <= 1.5) and conv_state is either
            # "idle", "talking", or "waiting" (if no response yet)
            # ═══════════════════════════════════════════════════════════════
            
            print(f"   📊 State: {conv_state}, last_seen={last_seen_from_them}, new_msg={their_new_msg_tick}, now={current_tick}")
            
            # Check if they're busy talking to someone else
            they_are_busy_with = None
            for c in reversed(recent_convos[-10:]):
                speaker_id = c.get("speaker_id")
                listener_id = c.get("listener_id")
                msg_tick = int(c.get("tick", 0) or 0)
                
                # If they spoke to someone else recently (not us)
                if speaker_id == other_id and listener_id != self.char_id:
                    if current_tick - msg_tick <= 8:  # Recent message
                        they_are_busy_with = c.get("listener_name", listener_id[:8] if listener_id else "someone")
                        break
                # Or if someone else spoke to them recently and they responded
                if listener_id == other_id and speaker_id != self.char_id:
                    if current_tick - msg_tick <= 8:
                        # Check if other_id responded to this
                        for c2 in recent_convos:
                            if c2.get("speaker_id") == other_id and c2.get("listener_id") == speaker_id:
                                c2_tick = int(c2.get("tick", 0) or 0)
                                if c2_tick > msg_tick:  # They responded
                                    they_are_busy_with = c.get("speaker_name", speaker_id[:8] if speaker_id else "someone")
                                    break
                        if they_are_busy_with:
                            break
            
            # STATE: WAITING - Still waiting for response (adjacent case)
            if conv_state == "waiting":
                # Check if they started talking to someone else - give up
                if they_are_busy_with:
                    print(f"   🚶 {other_name} is busy with {they_are_busy_with}, moving on")
                    self.conversation_state[other_id] = "idle"
                    self.waiting_since.pop(other_id, None)
                    self.goodbye_cooldown[other_id] = 15  # Short cooldown
                    return None, "move", None
                
                # We already checked for their response above, so if we're here, they haven't responded
                wait_start = self.waiting_since.get(other_id, current_tick)
                ticks_waiting = current_tick - wait_start
                if ticks_waiting < 12:
                    print(f"   ⏳ WAITING for {other_name}'s response ({ticks_waiting}/12 ticks)")
                    time.sleep(3)
                    return None, "skip", None
                else:
                    # Timeout - end conversation
                    print(f"   😤 TIMEOUT: {other_name} not responding")
                    self.conversation_state[other_id] = "idle"
                    self.waiting_since.pop(other_id, None)
                    self.goodbye_cooldown[other_id] = 30
                    prompt = f"""You are {self.name}. {other_name} seems distracted.
Say a brief goodbye. One sentence:"""
                    return prompt, "goodbye", other_id
            
            # STATE: IDLE - Not in conversation
            elif conv_state == "idle":
                # Before initiating, check if they're busy with someone else
                if they_are_busy_with and their_new_msg_tick == 0:
                    print(f"   🚶 {other_name} is busy with {they_are_busy_with}, finding someone else")
                    # Don't initiate - find someone else or move
                    return None, "move", None
                
                if their_new_msg_tick > 0:
                    # They initiated! Transition to TALKING to respond
                    print(f"   📨 {other_name} initiated at tick {their_new_msg_tick}")
                    self.last_seen_msg[other_id] = their_new_msg_tick
                    self.conversation_state[other_id] = "talking"
                    # Fall through to conversation logic
                else:
                    # Neither talking - determine who initiates (lower ID)
                    if self.char_id > other_id:
                        # Check if they're online
                        other_last_action = int(other.get("last_action_tick", 0) or 0)
                        if current_tick - other_last_action > 10:
                            print(f"   💤 {other_name} is offline")
                            return None, "move", None
                        
                        # Before waiting, do a fresh check for their message
                        # (handles race condition where message arrived after our state fetch)
                        for c in recent_convos:
                            if c.get("speaker_id") == other_id and c.get("listener_id") == self.char_id:
                                msg_tick = int(c.get("tick", 0) or 0)
                                if current_tick - msg_tick <= 10:  # Recent message
                                    print(f"   📨 {other_name} already initiated at tick {msg_tick}")
                                    self.last_seen_msg[other_id] = msg_tick
                                    self.conversation_state[other_id] = "talking"
                                    their_new_msg_tick = msg_tick  # For conversation logic below
                                    break
                        
                        # If we found their message, skip the waiting logic
                        if self.conversation_state.get(other_id) == "talking":
                            pass  # Fall through to conversation logic
                        else:
                            # Wait for them to initiate (with timeout)
                            init_key = f"init_{other_id}"
                            wait_start = self.waiting_since.get(init_key, 0)
                            if wait_start == 0:
                                self.waiting_since[init_key] = current_tick
                                print(f"   ⏳ Letting {other_name} initiate (lower ID) - tick 1/6")
                                time.sleep(2)
                                return None, "skip", None
                            elif current_tick - wait_start < 6:
                                print(f"   ⏳ Letting {other_name} initiate ({current_tick - wait_start}/6 ticks)")
                                time.sleep(2)
                                return None, "skip", None
                            else:
                                # Timeout - we initiate instead
                                print(f"   🗣️ Initiating (waited {current_tick - wait_start} ticks)")
                                self.waiting_since.pop(init_key, None)
                                self.conversation_state[other_id] = "talking"
                                # Fall through to conversation logic
                    else:
                        # We have lower ID - we initiate
                        print(f"   🗣️ Initiating (we have lower ID)")
                        self.conversation_state[other_id] = "talking"
                        # Fall through to conversation logic
            
            # STATE: TALKING - Continue below to generate response
            # (Already in talking state, or just transitioned)
            
            # Check if we're in goodbye cooldown with this person
            if other_id in self.goodbye_cooldown:
                remaining = self.goodbye_cooldown[other_id]
                
                # Use traveling direction if set, otherwise pick one
                if not self.traveling_direction or self.traveling_direction not in valid_moves:
                    if valid_moves:
                        self.traveling_direction = random.choice(valid_moves)
                
                prompt = f"""You are {self.name}, exploring the world.
{history_context}
{text_desc}

You recently said goodbye to {other_name}. Keep moving away!
Go {self.traveling_direction.upper()} to explore new areas.
✅ Valid moves: {', '.join(valid_moves)}

Reply with ONLY: move {self.traveling_direction}"""
                return prompt, "move", None
            
            # Build actual conversation history from server
            convo_history = ""
            last_convo_tick = 0
            if recent_convos:
                convo_history = "\n💬 RECENT CONVERSATION:\n"
                for c in recent_convos[-6:]:  # Last 6 messages for context
                    speaker = c.get("speaker_name", "Someone")
                    msg = c.get("message", "")
                    msg_tick = int(c.get("tick", 0) or 0)
                    last_convo_tick = max(last_convo_tick, msg_tick)
                    if speaker == self.name:
                        convo_history += f'  YOU: "{msg}"\n'
                    else:
                        convo_history += f'  {speaker}: "{msg}"\n'
            
            # Get current tick and check how long since last conversation
            current_tick = int(state.get("world", {}).get("tick", 0) or 0)
            ticks_since_last_convo = current_tick - last_convo_tick if last_convo_tick > 0 else 999
            
            # Find what they JUST said to us (find the NEWEST message from them to us)
            last_said_to_me = None
            last_said_tick = 0
            for c in recent_convos:  # Check all, find newest
                if c.get("listener_id") == self.char_id and c.get("speaker_id") == other_id:
                    msg_tick = int(c.get("tick", 0) or 0)
                    # Consider messages from the last 10 ticks, keep the newest one
                    if current_tick - msg_tick <= 10 and msg_tick > last_said_tick:
                        last_said_to_me = c.get("message")
                        last_said_tick = msg_tick
            
            # Check if they said goodbye RECENTLY - we should acknowledge and leave too
            goodbye_phrases = ['goodbye', 'farewell', 'adieu', 'bye', 'see you', 'until next', 'take care', 'been delightful', 'bid you', 'should go', 'must go', 'heading off']
            
            # Check if THEY said goodbye at all recently (even if not directly to us)
            they_said_goodbye = False
            they_goodbye_tick = 0
            for c in reversed(recent_convos[-15:]):
                if c.get("speaker_name") == other_name:
                    if any(phrase in c.get("message", "").lower() for phrase in goodbye_phrases):
                        they_said_goodbye = True
                        they_goodbye_tick = int(c.get("tick", 0) or 0)
                        break
            
            # If they said goodbye recently, set cooldown and leave
            if they_said_goodbye and current_tick - they_goodbye_tick < 30:
                # Check if we already acknowledged
                we_acknowledged = False
                for c in reversed(recent_convos[-10:]):
                    if c.get("speaker_id") == self.char_id:
                        c_tick = int(c.get("tick", 0) or 0)
                        if c_tick > they_goodbye_tick:
                            if any(phrase in c.get("message", "").lower() for phrase in goodbye_phrases):
                                we_acknowledged = True
                        break
                
                if we_acknowledged:
                    # Already said goodbye back, just set cooldown and move away
                    self.goodbye_cooldown[other_id] = 30
                    if not self.traveling_direction or self.traveling_direction not in valid_moves:
                        if valid_moves:
                            self.traveling_direction = random.choice(valid_moves)
                    return None, f"direct_move_{self.traveling_direction or 'south'}", None
                else:
                    # Need to say goodbye back
                    prompt = f"""You are {self.name}.
{other_name} said goodbye to you. Acknowledge and say a brief farewell.
One short sentence - use "goodbye", "farewell", or "take care":"""
                    return prompt, "goodbye", other_id
            
            if last_said_to_me:
                goodbye_phrases = ['goodbye', 'farewell', 'adieu', 'bye', 'see you', 'until next', 'take care', 'been delightful', 'bid you', 'should go', 'must go', 'heading off']
                said_goodbye = any(phrase in last_said_to_me.lower() for phrase in goodbye_phrases)
                
                # Check if WE already said goodbye recently (avoid goodbye ping-pong)
                we_said_goodbye_recently = False
                for c in reversed(recent_convos):
                    if c.get("speaker_id") == self.char_id:
                        msg_tick = int(c.get("tick", 0) or 0)
                        if current_tick - msg_tick <= 5:
                            if any(phrase in c.get("message", "").lower() for phrase in goodbye_phrases):
                                we_said_goodbye_recently = True
                        break
                
                if said_goodbye and not we_said_goodbye_recently:
                    # They said goodbye - acknowledge it and leave
                    prompt = f"""You are {self.name}.
{other_name} is saying goodbye: "{last_said_to_me}"

Say goodbye back warmly. One short sentence - use "goodbye", "farewell", or "take care":"""
                    return prompt, "goodbye", other_id
            
            # Count our local exchanges for goodbye/greeting logic
            our_local_messages = self.conversation_memory.get(other_id, [])
            local_exchange_count = len(our_local_messages)
            last_talked = self.last_talked_tick.get(other_id, 0)
            ticks_since_we_talked = current_tick - last_talked if last_talked > 0 else 999
            
            # Check SERVER data to see if we've ever met before (survives agent restarts)
            fatigue = other.get("conversationFatigue", {})
            server_exchanges = int(fatigue.get("exchanges", 0) or 0)
            past_summaries = fatigue.get("summaries", [])
            have_met_before = server_exchanges > 0 or len(recent_convos) > 0 or len(past_summaries) > 0
            
            # Build memory context from past summaries
            # Use LAST 3 (most recent) + up to 3 RANDOM older ones
            memory_context = ""
            if past_summaries:
                recent_memories = past_summaries[-3:] if len(past_summaries) >= 3 else past_summaries
                older_memories = past_summaries[:-3] if len(past_summaries) > 3 else []
                
                # Pick up to 3 random older memories
                random_older = random.sample(older_memories, min(3, len(older_memories))) if older_memories else []
                
                memory_context = f"\n📚 YOUR SHARED HISTORY WITH {other_name.upper()} ({len(past_summaries)} memories total):\n"
                
                if random_older:
                    memory_context += "  [From the past...]\n"
                    for s in random_older:
                        memory_context += f'  • "{s.get("title", "Past conversation")}": {s.get("summary", "")}\n'
                    memory_context += "  [More recently...]\n"
                
                for s in recent_memories:
                    memory_context += f'  • "{s.get("title", "Past conversation")}": {s.get("summary", "")}\n'
                
                # Debug: show memory stats
                print(f"   📚 Loaded {len(recent_memories)} recent + {len(random_older)} random older = {len(recent_memories) + len(random_older)}/{len(past_summaries)} memories with {other_name}")
            
            # After 8 local exchanges, say goodbye
            if local_exchange_count >= 8:
                prompt = f"""You are {self.name}.
You've had a nice conversation with {other_name}, but it's time to move on and explore.

Say goodbye warmly. Use phrases like "goodbye", "farewell", "I should go explore", or "take care". Keep it to one sentence:"""
                return prompt, "goodbye", other_id
            
            # GREETING LOGIC: Greet if no local memory OR been 30+ ticks since we talked
            should_greet = local_exchange_count == 0 or ticks_since_we_talked > 30
            
            if should_greet:
                personality_hint = f"\nYour personality: {self.personality[:100]}" if self.personality else ""
                
                if have_met_before:
                    prompt = f"""You are {self.name}. You see your friend {other_name}.
{personality_hint}
{memory_context}

Say hi naturally! ONE casual sentence:

{self.name}:"""
                else:
                    prompt = f"""You are {self.name}. You meet {other_name} for the first time.
{personality_hint}

Introduce yourself in ONE sentence - be yourself:

{self.name}:"""
                return prompt, "talk", other_id
            
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
                
                # Detect if we're echoing their structure
                our_last = ""
                for c in reversed(recent_convos):
                    if c.get("speaker_name") == self.name:
                        our_last = c.get("message", "")
                        break
                
                # Check for structural echoing (both starting with same words)
                echo_warning = ""
                if our_last:
                    # Get first few words of both
                    our_start = ' '.join(our_last.split()[:4]).lower()
                    their_start = ' '.join(last_said_to_me.split()[:4]).lower()
                    if our_start == their_start or (our_last[:20].lower() == last_said_to_me[:20].lower()):
                        echo_warning = "\n🚫 You've been echoing their style! Switch it up - ask a question, disagree, share a personal memory, or change the topic entirely."
                    else:
                        echo_warning = "\n⚠️ Don't start with the same words they used."
                
                # Add memory context for personality
                memory_hint = ""
                memory_prompt_addition = ""
                if memory_context:
                    memory_hint = f"\n{memory_context}"
                    # Occasionally suggest referencing an old memory
                    if past_summaries and len(past_summaries) > 3 and random.random() < 0.3:
                        random_memory = random.choice(past_summaries[:-3])
                        memory_prompt_addition = f'\n💡 Maybe reference your shared memory: "{random_memory.get("title", "something from the past")}"'
                
                # Only show their LAST message, not full thread (prevents pattern-matching)
                # Include personality to give character their own voice
                personality_hint = f"\nYour personality: {self.personality[:150]}" if self.personality else ""
                
                prompt = f"""You are {self.name} talking with {other_name}.
{personality_hint}
{memory_hint}
{other_name} said: "{last_said_to_me}"
{echo_warning}{memory_prompt_addition}

Reply in your own voice - be genuine, not performative. 1-2 sentences.
{"Answer their question." if asked_question else "React honestly or take the conversation somewhere new."}

{self.name}:"""
            else:
                # Add memory context for personality
                memory_hint = ""
                memory_prompt_addition = ""
                if memory_context:
                    memory_hint = f"\n{memory_context}"
                    # Occasionally suggest referencing an old memory
                    if past_summaries and len(past_summaries) > 3 and random.random() < 0.3:
                        random_memory = random.choice(past_summaries[:-3])
                        memory_prompt_addition = f'\n💡 Maybe bring up your shared memory: "{random_memory.get("title", "something from the past")}"'
                
                personality_hint = f"\nYour personality: {self.personality[:150]}" if self.personality else ""
                
                prompt = f"""You are {self.name} chatting with {other_name}.
{personality_hint}
{memory_hint}
Start a new topic or ask them a genuine question. Be curious about THEM.{memory_prompt_addition}
1-2 sentences only.

{self.name}:"""
            
            return prompt, "talk", other_id
        
        elif nearby_chars:
            other = nearby_chars[0]
            other_id = other.get("id", other["name"])
            directions = other.get("direction", valid_moves)
            
            # Filter directions to prefer non-blocked ones
            safe_directions = [d for d in directions if d in preferred_moves]
            if not safe_directions:
                safe_directions = [d for d in directions if d in valid_moves]
            if not safe_directions:
                safe_directions = preferred_moves if preferred_moves else valid_moves
            
            # If we're in goodbye cooldown with this person, DON'T approach - explore elsewhere
            if other_id in self.goodbye_cooldown:
                # Pick opposite direction from them
                avoid_directions = []
                for d in directions:
                    opposites = {'north': 'south', 'south': 'north', 'east': 'west', 'west': 'east'}
                    if opposites.get(d) in preferred_moves:
                        avoid_directions.append(opposites[d])
                if not avoid_directions:
                    avoid_directions = [d for d in preferred_moves if d not in directions]
                if not avoid_directions:
                    avoid_directions = preferred_moves if preferred_moves else valid_moves
                
                prompt = f"""You are {self.name}, exploring the world.
{history_context}
{text_desc}

You recently talked with {other['name']}. Explore somewhere NEW!
Go AWAY from them - try: {', '.join(avoid_directions)}
✅ Valid moves: {', '.join(valid_moves)}

Reply with ONLY: move {avoid_directions[0] if avoid_directions else 'south'}"""
                # Use direct move to avoid LLM ignoring our direction
                return None, f"direct_move_{avoid_directions[0] if avoid_directions else 'south'}", None
            
            best_direction = safe_directions[0] if safe_directions else 'south'
            # Skip LLM for simple approach - just move directly
            return None, f"direct_move_{best_direction}", None
        
        else:
            time_note = "🌙 It's night time. The world is quiet." if is_night else ""
            
            # Suggest a preferred direction
            suggested = preferred_moves[0] if preferred_moves else (valid_moves[0] if valid_moves else 'south')
            
            prompt = f"""You are {self.name}, exploring the world.
{history_context}
{time_note}
{text_desc}

GOAL: Explore new areas! Find other characters to meet!
✅ Valid moves: {', '.join(valid_moves)}
🚫 Recently blocked: {', '.join(self.blocked_directions.keys()) if self.blocked_directions else 'none'}
💡 Suggested: {suggested}

Reply with ONLY one of: move north | move south | move east | move west"""
            
            return prompt, "move", None
    
    def parse_response(self, response: str, expected_type: str) -> tuple:
        """Parse Ollama response into action and parameters."""
        response = response.strip()
        
        if expected_type == "talk":
            message = response
            for prefix in ["talk ", "Talk ", "say ", "Say ", '"', "Response:", "Greeting:", "What you say:", "Dialogue:"]:
                if message.lower().startswith(prefix.lower()):
                    message = message[len(prefix):]
            message = message.strip().strip('"').strip()
            
            # Clean up excessive asterisks - this model goes overboard
            # Remove asterisks around individual words in dialogue (not actions)
            # Keep *action phrases* but remove *"text"* patterns
            message = re.sub(r'\*"', '"', message)  # *" -> "
            message = re.sub(r'"\*', '"', message)  # "* -> "
            message = re.sub(r'\*\*+', '*', message)  # ** or more -> *
            
            # If the whole message is wrapped in asterisks, remove them
            if message.startswith('*') and message.endswith('*') and message.count('*') == 2:
                message = message[1:-1]
            
            # Remove asterisks around single words that aren't actions
            # (keep things like *grins* but remove *x* or *do* or *solve*)
            message = re.sub(r'\*(\w{1,4})\*', r'\1', message)  # Short words aren't actions
            
            # Clean up extra whitespace
            message = re.sub(r'\s+', ' ', message).strip()
            
            # Truncate to max 2 sentences for natural dialogue
            sentences = re.split(r'(?<=[.!?])\s+', message)
            if len(sentences) > 2:
                message = ' '.join(sentences[:2])
            
            return "talk", {"message": message or "Hello!"}
        
        elif expected_type == "goodbye":
            # Parse as a talk action but flag for leaving
            message = response
            for prefix in ["talk ", "Talk ", "say ", "Say ", '"', "Farewell:", "Goodbye:"]:
                if message.lower().startswith(prefix.lower()):
                    message = message[len(prefix):]
            message = message.strip().strip('"').strip()
            message = re.sub(r'\s+', ' ', message).strip()
            
            if not message or len(message) < 3:
                message = "It was nice talking! I should explore more. Goodbye!"
            return "talk", {"message": message, "is_goodbye": True}
        
        elif expected_type == "rest":
            # Parse interact command for resting
            response_lower = response.lower()
            for target in ["campfire", "cottage", "pond"]:
                if target in response_lower:
                    return "interact", {"target": target}
            return "interact", {"target": "campfire"}  # Fallback
        
        elif expected_type == "eat":
            # Parse interact command for eating at market
            return "interact", {"target": "market"}
        
        elif expected_type.startswith("direct_move_"):
            # Direct movement - no LLM needed, just use the specified direction
            direction = expected_type.replace("direct_move_", "")
            return "move", {"direction": direction}
        
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
                hunger = char.get('hunger', 100)
                is_night = state.get('world', {}).get('isNight', False)
                
                # Zone info
                zone = state.get('zone', {})
                zone_name = zone.get('name', 'Unknown')
                zone_safe = zone.get('isSafe', True)
                current_portal = state.get('currentPortal')
                
                print(f"\n{'═' * 50}")
                time_icon = '🌙' if is_night else '☀️'
                zone_icon = '🏠' if zone_safe else '⚔️'
                print(f"Turn {turn} │ Tick {state.get('world', {}).get('tick', '?')} {time_icon} │ "
                      f"L{char.get('level')} │ "
                      f"HP {char.get('hp')}/{char.get('max_hp')} │ "
                      f"XP {char.get('xp')} │ "
                      f"⚡{energy} │ 🍖{hunger}")
                print(f"Position: {current_pos} │ {zone_icon} {zone_name}")
                
                # Show portal if standing on one
                if current_portal:
                    print(f"   🚪 Portal: {current_portal.get('name')} → {current_portal.get('destinationName')}")
                
                # Show who's nearby (only online characters)
                nearby = state.get('nearbyCharacters', [])
                current_tick_display = int(state.get('world', {}).get('tick', 0) or 0)
                online_nearby = [c for c in nearby if current_tick_display - int(c.get('last_action_tick', 0) or 0) <= 10]
                if online_nearby:
                    names = [f"{c.get('emoji','')} {c['name']}" for c in online_nearby[:3]]
                    print(f"Nearby: {', '.join(names)}")
                
                # Show nearby portals
                nearby_portals = state.get('nearbyPortals', [])
                if nearby_portals and not current_portal:
                    portal_info = [f"{p.get('emoji','')} {p.get('name')} ({p.get('distance', 0):.0f} tiles)" for p in nearby_portals[:2]]
                    print(f"Portals: {', '.join(portal_info)}")
                
                print(f"{'─' * 50}")
                
                # Build prompt with action history
                prompt, expected_type, talk_target_id = self.build_prompt(state)
                
                # Debug: show decision state (only online characters)
                nearby = state.get("nearbyCharacters", [])
                current_tick = int(state.get("world", {}).get("tick", 0) or 0)
                for n in nearby:
                    nid = n.get("id", n["name"])
                    last_action = int(n.get("last_action_tick", 0) or 0)
                    # Skip offline characters in debug display
                    if current_tick - last_action > 10:
                        continue
                    in_cooldown = nid in self.goodbye_cooldown
                    cd_remaining = self.goodbye_cooldown.get(nid, 0)
                    fatigue = n.get("conversationFatigue", {})
                    summaries = fatigue.get("summaries", [])
                    sum_count = len(summaries) if summaries else 0
                    conv_state = self.conversation_state.get(nid, "idle")
                    print(f"   👁️ {n['name']} (dist={n.get('distance', '?'):.1f}) state={conv_state} cooldown={in_cooldown}({cd_remaining}) tick={last_action}")
                
                print(f"🤔 Thinking... (mode: {expected_type})")
                
                # Initialize for state tracking
                pending_state_change = None
                
                # Handle direct actions (no LLM needed)
                if prompt is None and expected_type == "skip":
                    # Waiting for response - skip this turn
                    continue
                elif prompt is None and expected_type.startswith("direct_move_"):
                    action, params = self.parse_response("", expected_type)
                    response = f"[direct: {params.get('direction')}]"
                elif prompt is None and expected_type == "rest":
                    # Keep resting - no LLM call needed
                    action, params = "interact", {"target": self.resting_at.lower().split()[0] if self.resting_at else "campfire"}
                    response = f"[resting at {self.resting_at}]"
                elif prompt is None and expected_type == "eat":
                    # Keep eating - no LLM call needed
                    action, params = "interact", {"target": "market"}
                    response = "[eating at market]"
                elif prompt is None and expected_type == "travel":
                    # Travel through portal - no LLM call needed
                    action, params = "travel", {}
                    response = "[traveling through portal]"
                else:
                    response = ollama_generate(prompt, self.model)
                    # Parse and execute
                    action, params = self.parse_response(response, expected_type)
                
                if action == "travel":
                    # Traveling to a new zone
                    current_portal = state.get("currentPortal", {})
                    dest_name = current_portal.get("destinationName", "unknown")
                    print(f"🚪 Traveling to {dest_name}...")
                    result = self.act("travel")
                    
                    if "error" in result:
                        print(f"❌ {result['error']}")
                    else:
                        new_zone = result.get("newZoneName", "unknown")
                        print(f"✨ Arrived in {new_zone}!")
                        # Reset traveling state since we're in a new area
                        self.traveling_turns = 0
                        self.traveling_direction = None
                    
                    time.sleep(POLL_INTERVAL)
                    continue
                
                if action == "talk":
                    message = params["message"]
                    is_goodbye = params.get("is_goodbye", False)
                    
                    # Note: Don't change state yet - wait for server confirmation
                    # We'll update state after we know the action succeeded
                    if talk_target_id:
                        current_tick = int(state.get("world", {}).get("tick", 0) or 0)
                        if is_goodbye:
                            pending_state_change = "goodbye"
                        else:
                            pending_state_change = "waiting"
                    
                    if is_goodbye:
                        print(f'👋 "{message}"')
                        # Set flag to force movement on next turn
                        self.must_leave = True
                        self.leaving_from = talk_target_id
                        
                        # Generate and save conversation summary ONLY if we had a real conversation (5+ exchanges)
                        if talk_target_id:
                            our_messages = self.conversation_memory.get(talk_target_id, [])
                            exchange_count = len(our_messages)
                            
                            if exchange_count >= 5:
                                other_name = None
                                for n in state.get("nearbyCharacters", []):
                                    if n.get("id") == talk_target_id:
                                        other_name = n.get("name", "Someone")
                                        break
                                
                                if other_name:
                                    print(f"📝 Summarizing conversation ({exchange_count} exchanges)...")
                                    # Show thought bubble on frontend
                                    self.send_think(other_name)
                                    summary_data = self.generate_summary(
                                        other_name, 
                                        talk_target_id, 
                                        state.get("recentConversations", [])
                                    )
                                    if summary_data and summary_data.get("summary"):
                                        if self.save_summary(talk_target_id, 
                                                            summary_data["title"],
                                                            summary_data["summary"],
                                                            summary_data["topics"]):
                                            print(f"   ✅ Saved: \"{summary_data['title']}\"")
                            else:
                                print(f"   (Conversation too short for summary: {exchange_count} exchanges)")
                    else:
                        print(f'💬 "{message}"')
                    
                    # Remember what we said to avoid repetition
                    if talk_target_id:
                        self.remember_our_message(talk_target_id, message)
                        # Track when we talked for greeting logic
                        current_tick = int(state.get("world", {}).get("tick", 0) or 0)
                        self.last_talked_tick[talk_target_id] = current_tick
                    action_desc = f"talk: {message[:50]}"
                    
                    # Add delay after talking to give conversation a natural pace
                    time.sleep(2)  # Extra pause after dialogue
                elif action == "interact":
                    target = params.get('target', 'something')
                    if 'market' in target.lower():
                        print(f"🍖 Eating at {target}")
                        action_desc = f"eat at {target}"
                    else:
                        print(f"💤 Resting at {target}")
                        action_desc = f"rest at {target}"
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
                        # Server rejected talk - we should move away
                        self.must_leave = True
                        self.leaving_from = talk_target_id
                    if result.get('consecutiveLimit'):
                        # Server says wait for response - stay in talking state to retry
                        print(f"   ⏳ Waiting for their response...")
                        if talk_target_id:
                            self.conversation_state[talk_target_id] = "talking"
                    if result.get('rateLimited'):
                        print(f"   ⏳ Rate limited, slowing down...")
                        time.sleep(2)  # Extra delay on rate limit
                    if result.get('goodbyeCooldown'):
                        # Server says we're in goodbye cooldown - set local cooldown
                        if talk_target_id:
                            self.goodbye_cooldown[talk_target_id] = result.get('ticksLeft', 25)
                            print(f"   🚶 Goodbye cooldown - find someone else")
                    
                    # Track blocked move directions
                    if action == "move" and "blocks" in result.get('error', '').lower():
                        direction = params.get('direction')
                        if direction:
                            current_tick = int(state.get("world", {}).get("tick", 0) or 0)
                            self.blocked_directions[direction] = current_tick
                            print(f"   🚫 Remembering: {direction} is blocked")
                    
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
                    if result.get('hunger') is not None:
                        print(f"   🍖 Hunger: {result['hunger']}")
                    
                    # NOW apply state change since action succeeded
                    if action == "talk" and talk_target_id and pending_state_change:
                        current_tick = int(state.get("world", {}).get("tick", 0) or 0)
                        if pending_state_change == "goodbye":
                            self.conversation_state[talk_target_id] = "idle"
                            self.waiting_since.pop(talk_target_id, None)
                            self.last_seen_msg.pop(talk_target_id, None)
                        else:
                            self.conversation_state[talk_target_id] = "waiting"
                            self.waiting_since[talk_target_id] = current_tick
                    
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
                print(f"\n\n👋 Logging out {self.name}...")
                try:
                    # Call logout endpoint to remove from world
                    r = requests.post(f"{self.server}/api/logout", headers=self.headers, timeout=5)
                    if r.status_code == 200:
                        print(f"✅ {self.name} has left the world.")
                    else:
                        print(f"⚠️ Logout failed: {r.text}")
                except:
                    print(f"⚠️ Could not reach server for logout.")
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
