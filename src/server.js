const express = require('express');
const cors = require('cors');
const { Pool } = require('pg');
const { v4: uuidv4 } = require('uuid');
const path = require('path');

const app = express();
app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, '../public')));

// Database connection
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.NODE_ENV === 'production' ? { rejectUnauthorized: false } : false
});

// SSE clients for real-time updates
let sseClients = [];

// Broadcast to all SSE clients
function broadcast(event, data) {
  sseClients.forEach(client => {
    client.res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  });
}

// ============== LEVELING SYSTEM ==============

// XP required for each level (exponential curve)
function xpForLevel(level) {
  return Math.floor(100 * Math.pow(1.5, level - 1));
}

// Calculate level from total XP
function calculateLevel(totalXp) {
  let level = 1;
  let xpNeeded = xpForLevel(level);
  let xpAccumulated = 0;
  
  while (xpAccumulated + xpNeeded <= totalXp) {
    xpAccumulated += xpNeeded;
    level++;
    xpNeeded = xpForLevel(level);
  }
  
  return {
    level,
    currentLevelXp: totalXp - xpAccumulated,
    xpToNextLevel: xpNeeded,
    totalXp
  };
}

// Award XP and check for level up
async function awardXp(charId, amount, reason) {
  const char = await pool.query('SELECT id, name, emoji, xp, level FROM characters WHERE id = $1', [charId]);
  if (char.rows.length === 0) return null;
  
  const me = char.rows[0];
  const oldLevel = me.level;
  const newXp = me.xp + amount;
  const levelInfo = calculateLevel(newXp);
  
  // Update XP and level
  await pool.query(
    'UPDATE characters SET xp = $1, level = $2, max_hp = $3 WHERE id = $4',
    [newXp, levelInfo.level, 100 + (levelInfo.level - 1) * 10, charId]
  );
  
  // Check for level up
  if (levelInfo.level > oldLevel) {
    const tick = (await pool.query('SELECT tick FROM world WHERE id = 1')).rows[0].tick;
    const msg = `🎉 ${me.name} leveled up to ${levelInfo.level}!`;
    
    await pool.query(
      'INSERT INTO activity_log (tick, character_id, action, message) VALUES ($1, $2, $3, $4)',
      [tick, charId, 'level_up', msg]
    );
    
    // Heal on level up
    await pool.query('UPDATE characters SET hp = max_hp WHERE id = $1', [charId]);
    
    broadcast('level_up', { 
      id: charId, 
      name: me.name, 
      emoji: me.emoji,
      newLevel: levelInfo.level,
      maxHp: 100 + (levelInfo.level - 1) * 10
    });
    
    return { leveledUp: true, newLevel: levelInfo.level, xpGained: amount };
  }
  
  return { leveledUp: false, xpGained: amount, ...levelInfo };
}

// XP rewards for different actions
const XP_REWARDS = {
  move: 1,
  talk: 5,
  examine: 2,
  interact: 10,
  first_meeting: 20
};

// ============== LIFE STORY EVOLUTION ==============

// Add a significant moment to a character's life story
async function addSignificantMoment(charId, moment, category = 'general') {
  const tick = (await pool.query('SELECT tick FROM world WHERE id = 1')).rows[0].tick;
  
  // Get current character data
  const char = await pool.query('SELECT name, significant_moments, life_story FROM characters WHERE id = $1', [charId]);
  if (char.rows.length === 0) return;
  
  const me = char.rows[0];
  let moments = me.significant_moments || [];
  
  // Add new moment
  moments.push({
    tick,
    category,
    moment,
    timestamp: new Date().toISOString()
  });
  
  // Keep only last 50 significant moments
  if (moments.length > 50) {
    moments = moments.slice(-50);
  }
  
  // Update life story periodically (every 10 moments)
  let lifeStory = me.life_story || '';
  if (moments.length % 10 === 0) {
    // Summarize recent moments into life story
    const recentMoments = moments.slice(-10).map(m => m.moment).join('. ');
    lifeStory += `\n\n[After ${tick} ticks] ${recentMoments}`;
    
    // Cap life story length
    if (lifeStory.length > 5000) {
      lifeStory = lifeStory.slice(-4000);
    }
  }
  
  await pool.query(
    'UPDATE characters SET significant_moments = $1, life_story = $2 WHERE id = $3',
    [JSON.stringify(moments), lifeStory, charId]
  );
}

// Categories of significant moments
const MOMENT_TRIGGERS = {
  first_meeting: (name) => `Met ${name} for the first time`,
  level_up: (level) => `Reached level ${level}`,
  long_conversation: (name, count) => `Had a deep conversation with ${name} (${count} exchanges)`,
  exploration: (location) => `Discovered ${location}`,
  friendship: (name) => `Became friends with ${name}`,
  conflict: (name) => `Had a disagreement with ${name}`
};

// ============== TICK SYSTEM ==============

async function worldTick() {
  try {
    // Increment tick
    const result = await pool.query(`
      UPDATE world SET tick = tick + 1, last_tick_at = NOW()
      WHERE id = 1
      RETURNING tick, is_night, day_length, night_length
    `);
    const { tick, is_night, day_length, night_length } = result.rows[0];
    const cycleLength = (day_length || 200) + (night_length || 50);
    const cyclePosition = tick % cycleLength;
    
    // Check for day/night transition
    const shouldBeNight = cyclePosition >= (day_length || 200);
    if (shouldBeNight !== (is_night || false)) {
      await pool.query('UPDATE world SET is_night = $1 WHERE id = 1', [shouldBeNight]);
      
      const transitionMsg = shouldBeNight 
        ? '🌙 Night falls over the meadow. The world grows quiet...'
        : '☀️ Dawn breaks! A new day begins in the meadow.';
      
      await pool.query(
        'INSERT INTO activity_log (tick, action, message) VALUES ($1, $2, $3)',
        [tick, 'world_event', transitionMsg]
      );
      broadcast('day_night', { tick, isNight: shouldBeNight, message: transitionMsg });
    }
    
    // Broadcast tick to viewers
    broadcast('tick', { tick, timestamp: new Date().toISOString(), isNight: shouldBeNight });
    
    // Every 10 ticks, regenerate a bit of energy for resting characters
    if (tick % 10 === 0) {
      await pool.query(`
        UPDATE characters 
        SET energy = LEAST(COALESCE(max_energy, 100), COALESCE(energy, 100) + 5)
        WHERE last_action_tick < $1 - 20
      `, [tick]); // Regen for idle characters
    }
    
    // Every 50 ticks, decay conversation cooldowns
    if (tick % 50 === 0) {
      await pool.query(`
        UPDATE relationships 
        SET recent_exchanges = GREATEST(0, recent_exchanges - 3)
        WHERE recent_exchanges > 0
      `);
    }
    
    // World events
    if (tick % 100 === 0) {
      const dayEvents = [
        'A gentle breeze blows through the meadow.',
        'Birds sing in the distance.',
        'The sun shifts in the sky.',
        'A butterfly flutters past.',
        'Leaves rustle softly.'
      ];
      const nightEvents = [
        'An owl hoots in the distance.',
        'Fireflies dance through the darkness.',
        'The stars twinkle overhead.',
        'A cool breeze whispers through the trees.',
        'The moon casts long shadows.'
      ];
      const events = shouldBeNight ? nightEvents : dayEvents;
      const event = events[Math.floor(Math.random() * events.length)];
      await pool.query(
        'INSERT INTO activity_log (tick, action, message) VALUES ($1, $2, $3)',
        [tick, 'world_event', event]
      );
      broadcast('event', { tick, message: event });
    }
    
  } catch (err) {
    console.error('Tick error:', err.message);
  }
}

// ============== HELPERS ==============

function getDistance(x1, y1, x2, y2) {
  return Math.sqrt(Math.pow(x2 - x1, 2) + Math.pow(y2 - y1, 2));
}

function getDirection(dx, dy) {
  if (dy < 0) return dx < 0 ? 'northwest' : dx > 0 ? 'northeast' : 'north';
  if (dy > 0) return dx < 0 ? 'southwest' : dx > 0 ? 'southeast' : 'south';
  return dx < 0 ? 'west' : dx > 0 ? 'east' : 'here';
}

function getMoveToward(dx, dy) {
  const moves = [];
  if (dy < 0) moves.push('north');
  if (dy > 0) moves.push('south');
  if (dx > 0) moves.push('east');
  if (dx < 0) moves.push('west');
  return moves;
}

// Generate a visual map of surroundings (6-tile radius)
function generateVisualMap(myX, myY, characters, objects, worldWidth, worldHeight) {
  const radius = 6;
  const size = radius * 2 + 1; // 13x13 grid
  
  // Initialize grid with terrain
  const grid = [];
  for (let row = 0; row < size; row++) {
    grid[row] = [];
    for (let col = 0; col < size; col++) {
      const worldX = myX - radius + col;
      const worldY = myY - radius + row;
      
      // Check boundaries
      if (worldX < 0 || worldX >= worldWidth || worldY < 0 || worldY >= worldHeight) {
        grid[row][col] = '##'; // Wall/boundary
      } else {
        grid[row][col] = '··'; // Empty grass
      }
    }
  }
  
  // Place objects
  for (const obj of objects) {
    const gridX = obj.x - myX + radius;
    const gridY = obj.y - myY + radius;
    if (gridX >= 0 && gridX < size && gridY >= 0 && gridY < size) {
      grid[gridY][gridX] = obj.emoji?.slice(0, 2) || '??';
    }
  }
  
  // Place characters
  for (const char of characters) {
    const gridX = char.x - myX + radius;
    const gridY = char.y - myY + radius;
    if (gridX >= 0 && gridX < size && gridY >= 0 && gridY < size) {
      grid[gridY][gridX] = char.emoji?.slice(0, 2) || '👤';
    }
  }
  
  // Place self at center
  grid[radius][radius] = '⭐';
  
  // Build text representation with coordinates
  let map = '\n📍 YOUR SURROUNDINGS (6-tile view):\n';
  map += '   '; 
  // Column headers (relative X: -6 to +6)
  for (let col = 0; col < size; col++) {
    const relX = col - radius;
    if (relX === 0) map += ' ⬇ ';
    else if (col % 2 === 0) map += `${relX >= 0 ? '+' : ''}${relX}`.padStart(3);
    else map += '   ';
  }
  map += '\n';
  
  for (let row = 0; row < size; row++) {
    const relY = row - radius;
    // Row label
    if (relY === 0) map += '➡️ ';
    else map += '   ';
    
    map += grid[row].join(' ') + '\n';
  }
  
  // Legend
  map += '\nLegend: ⭐=You, ##=Boundary, ··=Empty\n';
  
  // List what's visible
  const visible = [];
  for (const char of characters) {
    const dx = char.x - myX;
    const dy = char.y - myY;
    if (Math.abs(dx) <= radius && Math.abs(dy) <= radius) {
      const dirHint = [];
      if (dy < 0) dirHint.push('north');
      if (dy > 0) dirHint.push('south');
      if (dx > 0) dirHint.push('east');
      if (dx < 0) dirHint.push('west');
      visible.push(`${char.emoji} ${char.name} at (${dx >= 0 ? '+' : ''}${dx}, ${dy >= 0 ? '+' : ''}${dy}) - go ${dirHint.join(' then ') || 'nowhere, RIGHT HERE!'}`);
    }
  }
  for (const obj of objects) {
    const dx = obj.x - myX;
    const dy = obj.y - myY;
    if (Math.abs(dx) <= radius && Math.abs(dy) <= radius) {
      visible.push(`${obj.emoji} ${obj.name} at (${dx >= 0 ? '+' : ''}${dx}, ${dy >= 0 ? '+' : ''}${dy})`);
    }
  }
  
  if (visible.length > 0) {
    map += '\nVisible:\n' + visible.map(v => `  ${v}`).join('\n') + '\n';
  }
  
  return map;
}

async function canAct(charId) {
  const result = await pool.query(`
    SELECT c.turn_interval, c.last_action_tick, w.tick
    FROM characters c, world w
    WHERE c.id = $1 AND w.id = 1
  `, [charId]);
  
  if (result.rows.length === 0) return false;
  
  const { turn_interval, last_action_tick, tick } = result.rows[0];
  return (tick - last_action_tick) >= turn_interval;
}

async function updateLastAction(charId) {
  const world = await pool.query('SELECT tick FROM world WHERE id = 1');
  await pool.query(
    'UPDATE characters SET last_action_tick = $1, last_seen_at = NOW() WHERE id = $2',
    [world.rows[0].tick, charId]
  );
}

// ============== API ROUTES ==============

// Health check
app.get('/api/health', (req, res) => {
  res.json({ status: 'ok', timestamp: new Date().toISOString() });
});

// Get current character by token (for agents)
app.get('/api/me', async (req, res) => {
  try {
    const token = req.headers.authorization?.replace('Bearer ', '');
    
    if (!token) {
      return res.status(401).json({ error: 'No token provided' });
    }
    
    const char = await pool.query(`
      SELECT id, name, emoji, personality, traits, origin_story,
             x, y, hp, max_hp, xp, level, turn_interval
      FROM characters WHERE token = $1
    `, [token]);
    
    if (char.rows.length === 0) {
      return res.status(401).json({ error: 'Invalid token' });
    }
    
    const me = char.rows[0];
    res.json({
      id: me.id,
      name: me.name,
      emoji: me.emoji,
      personality: me.personality,
      traits: me.traits || [],
      originStory: me.origin_story,
      position: { x: me.x, y: me.y },
      hp: me.hp,
      maxHp: me.max_hp,
      xp: me.xp,
      level: me.level,
      turnInterval: me.turn_interval
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Get world state (public - for viewer)
app.get('/api/world', async (req, res) => {
  try {
    const world = await pool.query('SELECT * FROM world WHERE id = 1');
    const characters = await pool.query(`
      SELECT id, name, emoji, x, y, hp, max_hp, xp, level, is_active, 
             turn_interval, last_action_tick, created_at,
             COALESCE(energy, 100) as energy, COALESCE(max_energy, 100) as max_energy
      FROM characters WHERE is_active = true
      ORDER BY created_at
    `);
    const objects = await pool.query('SELECT * FROM objects');
    const recentConvos = await pool.query(`
      SELECT c.*, 
             s.name as speaker_name, s.emoji as speaker_emoji,
             l.name as listener_name, l.emoji as listener_emoji
      FROM conversations c
      JOIN characters s ON c.speaker_id = s.id
      JOIN characters l ON c.listener_id = l.id
      ORDER BY c.tick DESC LIMIT 20
    `);
    const recentActivity = await pool.query(`
      SELECT a.*, ch.name, ch.emoji
      FROM activity_log a
      LEFT JOIN characters ch ON a.character_id = ch.id
      ORDER BY a.tick DESC LIMIT 30
    `);
    
    res.json({
      world: world.rows[0],
      characters: characters.rows,
      objects: objects.rows,
      conversations: recentConvos.rows,
      activity: recentActivity.rows
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Register new character
app.post('/api/register', async (req, res) => {
  try {
    const { name, emoji, personality, origin_story, traits, turn_interval, is_hatched } = req.body;
    
    if (!name) {
      return res.status(400).json({ error: 'Name is required' });
    }
    
    if (!personality || personality.trim().length < 10) {
      return res.status(400).json({ error: 'Personality description is required (at least 10 characters)' });
    }
    
    // Check for duplicate name
    const existing = await pool.query(
      'SELECT id FROM characters WHERE LOWER(name) = LOWER($1)',
      [name.trim()]
    );
    
    if (existing.rows.length > 0) {
      return res.status(409).json({ error: 'A character with this name already exists. Please choose a different name.' });
    }
    
    const id = name.toLowerCase().replace(/[^a-z0-9]/g, '-').slice(0, 30) + '-' + uuidv4().slice(0, 8);
    const token = uuidv4();
    
    // Find a spawn point (random position not on a blocking object)
    const world = await pool.query('SELECT width, height FROM world WHERE id = 1');
    const { width, height } = world.rows[0];
    const blocking = await pool.query('SELECT x, y FROM objects WHERE blocking = true');
    const blockedSet = new Set(blocking.rows.map(o => `${o.x},${o.y}`));
    
    let x, y;
    do {
      x = Math.floor(Math.random() * width);
      y = Math.floor(Math.random() * height);
    } while (blockedSet.has(`${x},${y}`));
    
    await pool.query(`
      INSERT INTO characters (id, token, name, emoji, personality, origin_story, traits, turn_interval, x, y, is_hatched, hatched_at, energy, max_energy)
      VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, 100, 100)
    `, [
      id, 
      token, 
      name, 
      emoji || '🤖', 
      personality.trim(),
      origin_story || null,
      JSON.stringify(traits || []),
      turn_interval || 1,
      x, 
      y,
      is_hatched || false,
      is_hatched ? new Date() : null
    ]);
    
    // Log spawn
    const tick = (await pool.query('SELECT tick FROM world WHERE id = 1')).rows[0].tick;
    await pool.query(
      'INSERT INTO activity_log (tick, character_id, action, message) VALUES ($1, $2, $3, $4)',
      [tick, id, 'spawn', `${name} has entered the world at ${x},${y}`]
    );
    
    broadcast('spawn', { id, name, emoji: emoji || '🤖', x, y });
    
    res.json({
      success: true,
      character: { id, name, emoji: emoji || '🤖', x, y },
      token,
      message: 'Save this token! You need it to control your character.'
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Look around (character's perspective)
app.get('/api/look/:charId', async (req, res) => {
  try {
    const { charId } = req.params;
    const token = req.headers.authorization?.replace('Bearer ', '');
    
    // Verify token
    const char = await pool.query(
      'SELECT * FROM characters WHERE id = $1 AND token = $2',
      [charId, token]
    );
    
    if (char.rows.length === 0) {
      return res.status(401).json({ error: 'Invalid character or token' });
    }
    
    const me = char.rows[0];
    const world = await pool.query('SELECT * FROM world WHERE id = 1');
    const { width, height, tick, is_night, day_length, night_length } = world.rows[0];
    const isNight = is_night || false;
    
    // Get conversation fatigue info for nearby characters
    const fatigueInfo = await pool.query(`
      SELECT char2_id, recent_exchanges, cooldown_until_tick 
      FROM relationships 
      WHERE char1_id = $1
    `, [charId]);
    const fatigueMap = {};
    fatigueInfo.rows.forEach(r => {
      fatigueMap[r.char2_id] = { 
        exchanges: r.recent_exchanges || 0, 
        cooldownUntil: r.cooldown_until_tick || 0 
      };
    });
    
    // Get nearby characters (within 10 tiles)
    const others = await pool.query(`
      SELECT id, name, emoji, x, y, hp, xp, level
      FROM characters
      WHERE id != $1 AND is_active = true
        AND ABS(x - $2) <= 10 AND ABS(y - $3) <= 10
    `, [charId, me.x, me.y]);
    
    // Get nearby objects (within 5 tiles)
    const objects = await pool.query(`
      SELECT * FROM objects
      WHERE ABS(x - $1) <= 6 AND ABS(y - $2) <= 6
    `, [me.x, me.y]);
    
    // Get recent memories
    const memories = await pool.query(`
      SELECT content, tick FROM memories
      WHERE character_id = $1
      ORDER BY tick DESC LIMIT 5
    `, [charId]);
    
    // Get recent conversations involving this character
    const convos = await pool.query(`
      SELECT c.*, s.name as speaker_name, l.name as listener_name
      FROM conversations c
      JOIN characters s ON c.speaker_id = s.id
      JOIN characters l ON c.listener_id = l.id
      WHERE c.speaker_id = $1 OR c.listener_id = $1
      ORDER BY c.tick DESC LIMIT 5
    `, [charId]);
    
    // Build output similar to local version
    const energy = me.energy || 100;
    const maxEnergy = me.max_energy || 100;
    
    let output = `[${me.name} at position ${me.x},${me.y}]\n`;
    output += `HP: ${me.hp}/${me.max_hp} | XP: ${me.xp} | Level: ${me.level}\n`;
    output += `⚡ Energy: ${energy}/${maxEnergy}${energy < 30 ? ' ⚠️ LOW - find a campfire or cottage to rest!' : ''}\n`;
    output += `World tick: ${tick} | ${isNight ? '🌙 NIGHT' : '☀️ DAY'}\n\n`;
    
    if (isNight) {
      output += '🌙 It is night time. The world is quieter.\n\n';
    }
    
    // Boundaries
    const blocked = [];
    const open = [];
    if (me.y === 0) blocked.push('north'); else open.push('north');
    if (me.y === height - 1) blocked.push('south'); else open.push('south');
    if (me.x === width - 1) blocked.push('east'); else open.push('east');
    if (me.x === 0) blocked.push('west'); else open.push('west');
    
    if (blocked.length > 0) {
      output += `⚠️ BOUNDARY: Cannot move ${blocked.join(' or ')}\n\n`;
    }
    
    // Other characters
    const nearbyChars = others.rows.map(o => ({
      ...o,
      distance: getDistance(me.x, me.y, o.x, o.y),
      direction: getMoveToward(o.x - me.x, o.y - me.y)
    })).sort((a, b) => a.distance - b.distance);
    
    if (nearbyChars.length > 0) {
      output += '👥 OTHER CHARACTERS:\n';
      for (const other of nearbyChars) {
        const fatigue = fatigueMap[other.id] || { exchanges: 0, cooldownUntil: 0 };
        let fatigueNote = '';
        if (fatigue.cooldownUntil > tick) {
          fatigueNote = ' [⏳ Taking a break from chatting]';
        } else if (fatigue.exchanges >= 10) {
          fatigueNote = ' [😴 Conversation getting stale]';
        } else if (fatigue.exchanges >= 5) {
          fatigueNote = ' [💤 Talked a bit already]';
        }
        
        if (other.distance < 1.5) {
          output += `  ${other.emoji} ${other.name} - RIGHT NEXT TO YOU! You can TALK!${fatigueNote}\n`;
        } else if (other.distance < 3) {
          output += `  ${other.emoji} ${other.name} - nearby (${Math.round(other.distance)} tiles). Move ${other.direction.join(' or ')} to approach.${fatigueNote}\n`;
        } else {
          output += `  ${other.emoji} ${other.name} - in the distance (${Math.round(other.distance)} tiles). Move ${other.direction.join(' or ')} to approach.\n`;
        }
      }
      output += '\n';
    }
    
    // Objects
    const nearbyObjs = objects.rows.map(o => ({
      ...o,
      distance: getDistance(me.x, me.y, o.x, o.y)
    })).sort((a, b) => a.distance - b.distance);
    
    if (nearbyObjs.length > 0) {
      output += '🌿 NEARBY OBJECTS:\n';
      for (const obj of nearbyObjs.slice(0, 5)) {
        const dir = getDirection(obj.x - me.x, obj.y - me.y);
        const dist = obj.distance < 1 ? 'here' : `${dir} (${Math.round(obj.distance)} tiles)`;
        const isRestSpot = obj.name.toLowerCase().includes('campfire') || 
                          obj.name.toLowerCase().includes('cottage') ||
                          obj.name.toLowerCase().includes('pond');
        const restNote = isRestSpot ? ' 💤 [Can rest here to recover energy]' : '';
        output += `  ${obj.emoji} ${obj.name} - ${dist}${restNote}\n`;
      }
      output += '\n';
    }
    
    output += `✅ VALID MOVES: ${open.join(', ')}\n`;
    if (blocked.length > 0) {
      output += `❌ BLOCKED: ${blocked.join(', ')}\n`;
    }
    
    const canTalk = nearbyChars.some(c => c.distance < 2);
    if (canTalk) {
      output += '\n💬 Someone is close enough to TALK!';
    }
    
    // Generate visual map of surroundings
    const visualMap = generateVisualMap(
      me.x, me.y, 
      others.rows, 
      objects.rows, 
      width, height
    );
    output += visualMap;
    
    // Check if can act
    const actAllowed = await canAct(charId);
    
    // Find rest spots nearby
    const restSpots = nearbyObjs.filter(o => 
      o.name.toLowerCase().includes('campfire') || 
      o.name.toLowerCase().includes('cottage') ||
      o.name.toLowerCase().includes('pond')
    );
    
    res.json({
      character: { ...me, energy, maxEnergy },
      world: { ...world.rows[0], isNight },
      nearbyCharacters: nearbyChars.map(c => ({
        ...c,
        conversationFatigue: fatigueMap[c.id] || { exchanges: 0, cooldownUntil: 0 }
      })),
      nearbyObjects: nearbyObjs,
      restSpotsNearby: restSpots.map(r => ({ name: r.name, distance: r.distance })),
      memories: memories.rows,
      recentConversations: convos.rows,
      canAct: actAllowed,
      canTalk,
      needsRest: energy < 30,
      validMoves: open,
      blockedMoves: blocked,
      visualMap,
      textDescription: output
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Submit action
app.post('/api/action/:charId', async (req, res) => {
  try {
    const { charId } = req.params;
    const token = req.headers.authorization?.replace('Bearer ', '');
    const { action, direction, target, message } = req.body;
    
    // Verify token
    const char = await pool.query(
      'SELECT * FROM characters WHERE id = $1 AND token = $2',
      [charId, token]
    );
    
    if (char.rows.length === 0) {
      return res.status(401).json({ error: 'Invalid character or token' });
    }
    
    const me = char.rows[0];
    
    // Check if can act
    if (!(await canAct(charId))) {
      return res.status(429).json({ 
        error: 'Not your turn yet',
        message: `Your turn interval is ${me.turn_interval} ticks. Wait a bit.`
      });
    }
    
    const world = await pool.query('SELECT * FROM world WHERE id = 1');
    const { width, height, tick } = world.rows[0];
    
    let result = { success: false };
    
    switch (action) {
      case 'move': {
        const dirs = {
          north: [0, -1], south: [0, 1],
          east: [1, 0], west: [-1, 0]
        };
        const delta = dirs[direction?.toLowerCase()];
        
        if (!delta) {
          return res.status(400).json({ error: 'Invalid direction' });
        }
        
        const newX = me.x + delta[0];
        const newY = me.y + delta[1];
        
        // Boundary check
        if (newX < 0 || newX >= width || newY < 0 || newY >= height) {
          return res.status(400).json({ error: 'Cannot move there - edge of world' });
        }
        
        // Blocking object check
        const blocking = await pool.query(
          'SELECT * FROM objects WHERE x = $1 AND y = $2 AND blocking = true',
          [newX, newY]
        );
        if (blocking.rows.length > 0) {
          return res.status(400).json({ error: `${blocking.rows[0].name} blocks your path` });
        }
        
        await pool.query(
          'UPDATE characters SET x = $1, y = $2 WHERE id = $3',
          [newX, newY, charId]
        );
        
        const msg = `${me.name} moves ${direction}`;
        await pool.query(
          'INSERT INTO activity_log (tick, character_id, action, message) VALUES ($1, $2, $3, $4)',
          [tick, charId, 'move', msg]
        );
        
        broadcast('move', { id: charId, name: me.name, emoji: me.emoji, x: newX, y: newY, direction });
        
        // Small XP for exploring
        await awardXp(charId, XP_REWARDS.move, 'move');
        
        result = { success: true, message: msg, position: { x: newX, y: newY } };
        break;
      }
      
      case 'talk': {
        if (!message) {
          return res.status(400).json({ error: 'Message is required' });
        }
        
        // Check energy
        const currentEnergy = me.energy || 100;
        const energyCost = 3;
        if (currentEnergy < energyCost) {
          return res.status(400).json({ 
            error: 'Too tired to talk. Rest at a campfire or cottage to recover energy.',
            energy: currentEnergy,
            needRest: true
          });
        }
        
        // Find nearby character to talk to
        const nearby = await pool.query(`
          SELECT id, name FROM characters
          WHERE id != $1 AND is_active = true
            AND ABS(x - $2) <= 2 AND ABS(y - $3) <= 2
          ORDER BY SQRT(POWER(x - $2, 2) + POWER(y - $3, 2))
          LIMIT 1
        `, [charId, me.x, me.y]);
        
        if (nearby.rows.length === 0) {
          return res.status(400).json({ error: 'No one nearby to talk to' });
        }
        
        const listener = nearby.rows[0];
        
        // Check conversation fatigue/cooldown
        const relCheck = await pool.query(`
          SELECT recent_exchanges, cooldown_until_tick 
          FROM relationships 
          WHERE char1_id = $1 AND char2_id = $2
        `, [charId, listener.id]);
        
        let recentExchanges = 0;
        let cooldownUntil = 0;
        
        if (relCheck.rows.length > 0) {
          recentExchanges = relCheck.rows[0].recent_exchanges || 0;
          cooldownUntil = relCheck.rows[0].cooldown_until_tick || 0;
        }
        
        // Check if in cooldown
        if (cooldownUntil > tick) {
          const ticksLeft = cooldownUntil - tick;
          return res.status(400).json({ 
            error: `You've talked with ${listener.name} a lot recently. Take a break and explore! (${ticksLeft} ticks until you can chat again)`,
            conversationFatigue: true,
            cooldownTicks: ticksLeft
          });
        }
        
        // Deduct energy (use COALESCE to handle NULL)
        await pool.query('UPDATE characters SET energy = COALESCE(energy, 100) - $1 WHERE id = $2', [energyCost, charId]);
        
        await pool.query(
          'INSERT INTO conversations (tick, speaker_id, listener_id, message) VALUES ($1, $2, $3, $4)',
          [tick, charId, listener.id, message]
        );
        
        // Add to both characters' memories
        await pool.query(
          'INSERT INTO memories (character_id, tick, content) VALUES ($1, $2, $3)',
          [charId, tick, `I said to ${listener.name}: "${message}"`]
        );
        await pool.query(
          'INSERT INTO memories (character_id, tick, content) VALUES ($1, $2, $3)',
          [listener.id, tick, `${me.name} said to me: "${message}"`]
        );
        
        // Update relationship with exchange tracking
        const existingRel = await pool.query(
          'SELECT * FROM relationships WHERE char1_id = $1 AND char2_id = $2',
          [charId, listener.id]
        );
        
        const isFirstMeeting = existingRel.rows.length === 0;
        const newExchangeCount = recentExchanges + 1;
        
        // Calculate cooldown - after 15 exchanges, enforce 30-tick break
        const newCooldown = newExchangeCount >= 15 ? tick + 30 : 0;
        
        await pool.query(`
          INSERT INTO relationships (char1_id, char2_id, sentiment, interactions, first_met_tick, last_interaction_tick, recent_exchanges, cooldown_until_tick)
          VALUES ($1, $2, 5, 1, $3, $3, 1, 0)
          ON CONFLICT (char1_id, char2_id) 
          DO UPDATE SET interactions = relationships.interactions + 1, 
                        sentiment = LEAST(100, relationships.sentiment + 1),
                        last_interaction_tick = $3,
                        recent_exchanges = $4,
                        cooldown_until_tick = $5
        `, [charId, listener.id, tick, newExchangeCount, newCooldown]);
        
        // Record significant moment if first meeting
        if (isFirstMeeting) {
          await addSignificantMoment(charId, MOMENT_TRIGGERS.first_meeting(listener.name), 'social');
          await addSignificantMoment(listener.id, MOMENT_TRIGGERS.first_meeting(me.name), 'social');
        }
        
        // Diminishing XP returns based on recent exchanges
        // First 5: full XP (5), next 5: reduced (2), after 10: none (0)
        let xpReward;
        if (isFirstMeeting) {
          xpReward = XP_REWARDS.first_meeting;
        } else if (newExchangeCount <= 5) {
          xpReward = XP_REWARDS.talk;
        } else if (newExchangeCount <= 10) {
          xpReward = 2;
        } else {
          xpReward = 0;
        }
        
        const xpResult = xpReward > 0 ? await awardXp(charId, xpReward, 'talk') : { xpGained: 0 };
        
        const msg = `${me.name} says to ${listener.name}: "${message}"`;
        await pool.query(
          'INSERT INTO activity_log (tick, character_id, action, message) VALUES ($1, $2, $3, $4)',
          [tick, charId, 'talk', msg]
        );
        
        broadcast('talk', { 
          speaker: { id: charId, name: me.name, emoji: me.emoji },
          listener: { id: listener.id, name: listener.name },
          message: message,
          speakerEnergy: currentEnergy - energyCost
        });
        
        // Build response with fatigue info
        let fatigueWarning = null;
        if (newExchangeCount >= 12) {
          fatigueWarning = `You've been talking to ${listener.name} for a while. Consider exploring elsewhere soon!`;
        } else if (newExchangeCount >= 8) {
          fatigueWarning = `Conversation getting long. XP rewards diminishing.`;
        }
        
        result = { 
          success: true, 
          message: msg, 
          listener: listener.name, 
          xp: xpResult,
          energy: currentEnergy - energyCost,
          exchangeCount: newExchangeCount,
          fatigueWarning,
          cooldownIn: newCooldown > 0 ? 0 : (15 - newExchangeCount)
        };
        break;
      }
      
      case 'examine': {
        const obj = await pool.query(`
          SELECT * FROM objects
          WHERE (LOWER(name) LIKE $1 OR LOWER(id) LIKE $1)
            AND ABS(x - $2) <= 3 AND ABS(y - $3) <= 3
          LIMIT 1
        `, [`%${(target || '').toLowerCase()}%`, me.x, me.y]);
        
        if (obj.rows.length === 0) {
          return res.status(400).json({ error: `Don't see "${target}" nearby` });
        }
        
        const o = obj.rows[0];
        result = { success: true, object: o, description: o.description };
        
        await pool.query(
          'INSERT INTO activity_log (tick, character_id, action, message) VALUES ($1, $2, $3, $4)',
          [tick, charId, 'examine', `${me.name} examines the ${o.name}`]
        );
        break;
      }
      
      case 'interact': {
        const obj = await pool.query(`
          SELECT * FROM objects
          WHERE (LOWER(name) LIKE $1 OR LOWER(id) LIKE $1)
            AND ABS(x - $2) <= 2 AND ABS(y - $3) <= 2
            AND can_interact = true
          LIMIT 1
        `, [`%${(target || '').toLowerCase()}%`, me.x, me.y]);
        
        if (obj.rows.length === 0) {
          return res.status(400).json({ error: `Can't interact with "${target}"` });
        }
        
        const o = obj.rows[0];
        
        // Special handling for rest spots (campfire, cottage)
        const isRestSpot = o.name.toLowerCase().includes('campfire') || 
                          o.name.toLowerCase().includes('cottage') ||
                          o.name.toLowerCase().includes('pond');
        
        if (isRestSpot) {
          // Restore energy (use COALESCE to handle NULL)
          const energyRestored = o.name.toLowerCase().includes('cottage') ? 50 : 30;
          await pool.query(
            'UPDATE characters SET energy = LEAST(COALESCE(max_energy, 100), COALESCE(energy, 0) + $1) WHERE id = $2',
            [energyRestored, charId]
          );
          
          const newEnergy = await pool.query('SELECT energy, max_energy FROM characters WHERE id = $1', [charId]);
          
          await pool.query(
            'INSERT INTO activity_log (tick, character_id, action, message) VALUES ($1, $2, $3, $4)',
            [tick, charId, 'rest', `${me.name} rests at the ${o.name} and recovers energy`]
          );
          
          broadcast('rest', { id: charId, name: me.name, object: o.name, energyRestored, newEnergy: newEnergy.rows[0].energy });
          
          result = { 
            success: true, 
            object: o.name, 
            result: `You rest at the ${o.name} and feel refreshed. (+${energyRestored} energy)`,
            energy: newEnergy.rows[0].energy,
            maxEnergy: newEnergy.rows[0].max_energy,
            xp: await awardXp(charId, 2, 'rest')
          };
          break;
        }
        
        // XP for exploring
        const xpResult = await awardXp(charId, XP_REWARDS.interact, 'interact');
        
        await pool.query(
          'INSERT INTO activity_log (tick, character_id, action, message) VALUES ($1, $2, $3, $4)',
          [tick, charId, 'interact', `${me.name} interacts with the ${o.name}`]
        );
        
        broadcast('interact', { id: charId, name: me.name, object: o.name });
        
        result = { success: true, object: o.name, result: o.interact_result, xp: xpResult };
        break;
      }
      
      default:
        return res.status(400).json({ error: 'Unknown action. Use: move, talk, examine, interact' });
    }
    
    // Update last action tick
    await updateLastAction(charId);
    
    res.json(result);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// List all characters (public)
app.get('/api/characters', async (req, res) => {
  try {
    const chars = await pool.query(`
      SELECT id, name, emoji, x, y, hp, max_hp, xp, level, 
             turn_interval, is_active, created_at, last_seen_at,
             COALESCE(energy, 100) as energy, COALESCE(max_energy, 100) as max_energy
      FROM characters
      ORDER BY xp DESC
    `);
    res.json(chars.rows);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Update character settings
app.patch('/api/character/:charId', async (req, res) => {
  try {
    const { charId } = req.params;
    const token = req.headers.authorization?.replace('Bearer ', '');
    const { turn_interval, personality, is_active } = req.body;
    
    const char = await pool.query(
      'SELECT * FROM characters WHERE id = $1 AND token = $2',
      [charId, token]
    );
    
    if (char.rows.length === 0) {
      return res.status(401).json({ error: 'Invalid character or token' });
    }
    
    const updates = [];
    const values = [];
    let idx = 1;
    
    if (turn_interval !== undefined) {
      updates.push(`turn_interval = $${idx++}`);
      values.push(Math.max(1, Math.min(1000, turn_interval)));
    }
    if (personality !== undefined) {
      updates.push(`personality = $${idx++}`);
      values.push(personality.slice(0, 1000));
    }
    if (is_active !== undefined) {
      updates.push(`is_active = $${idx++}`);
      values.push(is_active);
    }
    
    if (updates.length === 0) {
      return res.status(400).json({ error: 'No updates provided' });
    }
    
    values.push(charId);
    await pool.query(
      `UPDATE characters SET ${updates.join(', ')} WHERE id = $${idx}`,
      values
    );
    
    res.json({ success: true, message: 'Character updated' });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// SSE stream for real-time updates
app.get('/api/stream', (req, res) => {
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  
  const clientId = Date.now();
  const client = { id: clientId, res };
  sseClients.push(client);
  
  console.log(`📡 Client ${clientId} connected. Total: ${sseClients.length}`);
  
  req.on('close', () => {
    sseClients = sseClients.filter(c => c.id !== clientId);
    console.log(`📡 Client ${clientId} disconnected. Total: ${sseClients.length}`);
  });
});

// Serve viewer
app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, '../public/index.html'));
});

// Serve character creation wizard
app.get('/create', (req, res) => {
  res.sendFile(path.join(__dirname, '../public/create.html'));
});

// Get character profile (public)
app.get('/api/profile/:charId', async (req, res) => {
  try {
    const { charId } = req.params;
    
    const char = await pool.query(`
      SELECT id, name, emoji, personality, origin_story, traits, life_story,
             x, y, hp, max_hp, xp, level, is_hatched, hatched_at,
             created_at, last_seen_at, total_actions
      FROM characters WHERE id = $1
    `, [charId]);
    
    if (char.rows.length === 0) {
      return res.status(404).json({ error: 'Character not found' });
    }
    
    const me = char.rows[0];
    
    // Get relationships
    const relationships = await pool.query(`
      SELECT r.*, c.name, c.emoji
      FROM relationships r
      JOIN characters c ON (c.id = r.char2_id)
      WHERE r.char1_id = $1
      ORDER BY r.interactions DESC
    `, [charId]);
    
    // Get recent memories
    const memories = await pool.query(`
      SELECT content, tick, created_at FROM memories
      WHERE character_id = $1
      ORDER BY tick DESC LIMIT 10
    `, [charId]);
    
    // Get significant moments
    const moments = me.significant_moments || [];
    
    res.json({
      character: {
        id: me.id,
        name: me.name,
        emoji: me.emoji,
        level: me.level,
        xp: me.xp,
        hp: me.hp,
        maxHp: me.max_hp,
        position: { x: me.x, y: me.y },
        isHatched: me.is_hatched,
        hatchedAt: me.hatched_at,
        createdAt: me.created_at,
        lastSeen: me.last_seen_at,
        totalActions: me.total_actions || 0
      },
      identity: {
        personality: me.personality,
        originStory: me.origin_story,
        traits: me.traits || []
      },
      evolution: {
        lifeStory: me.life_story,
        significantMoments: moments.slice(-20)
      },
      relationships: relationships.rows.map(r => ({
        character: { id: r.char2_id, name: r.name, emoji: r.emoji },
        sentiment: r.sentiment,
        interactions: r.interactions,
        firstMet: r.first_met_tick
      })),
      recentMemories: memories.rows
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ============== START SERVER ==============

const PORT = process.env.PORT || 3000;
const TICK_INTERVAL = parseInt(process.env.TICK_INTERVAL_MS) || 5000;

app.listen(PORT, () => {
  console.log(`\n⚔️  LLM RPG Server running on port ${PORT}`);
  console.log(`📺 Viewer: http://localhost:${PORT}`);
  console.log(`🔌 API: http://localhost:${PORT}/api`);
  console.log(`⏱️  Tick interval: ${TICK_INTERVAL}ms`);
  console.log(`\n📊 Leveling: ${xpForLevel(1)} XP for L2, ${xpForLevel(2)} XP for L3, etc.\n`);
  
  // Start tick system
  setInterval(worldTick, TICK_INTERVAL);
});
