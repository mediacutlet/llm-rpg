// Database initialization script
// Run with: npm run db:init

const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.NODE_ENV === 'production' ? { rejectUnauthorized: false } : false
});

const schema = `
-- World metadata
CREATE TABLE IF NOT EXISTS world (
  id INTEGER PRIMARY KEY DEFAULT 1,
  name VARCHAR(100) DEFAULT 'The Meadow',
  width INTEGER DEFAULT 20,
  height INTEGER DEFAULT 15,
  tick BIGINT DEFAULT 0,
  tick_interval_ms INTEGER DEFAULT 5000,
  last_tick_at TIMESTAMP DEFAULT NOW(),
  created_at TIMESTAMP DEFAULT NOW(),
  CONSTRAINT single_world CHECK (id = 1)
);

-- Characters (agents)
CREATE TABLE IF NOT EXISTS characters (
  id VARCHAR(50) PRIMARY KEY,
  token VARCHAR(100) UNIQUE NOT NULL,
  name VARCHAR(100) NOT NULL,
  emoji VARCHAR(10) DEFAULT '🤖',
  
  -- PERMANENT: Set at creation, cannot be changed
  personality TEXT NOT NULL,
  origin_story TEXT,
  traits JSONB DEFAULT '[]',
  is_hatched BOOLEAN DEFAULT false,
  hatched_at TIMESTAMP,
  
  -- EVOLVING: Grows as character experiences the world
  life_story TEXT DEFAULT '',
  significant_moments JSONB DEFAULT '[]',
  
  -- Stats
  x INTEGER DEFAULT 10,
  y INTEGER DEFAULT 7,
  hp INTEGER DEFAULT 100,
  max_hp INTEGER DEFAULT 100,
  xp INTEGER DEFAULT 0,
  level INTEGER DEFAULT 1,
  
  -- Activity
  turn_interval INTEGER DEFAULT 1,
  last_action_tick BIGINT DEFAULT 0,
  is_active BOOLEAN DEFAULT true,
  total_actions INTEGER DEFAULT 0,
  
  -- Timestamps
  created_at TIMESTAMP DEFAULT NOW(),
  hatched_at TIMESTAMP,
  last_seen_at TIMESTAMP DEFAULT NOW()
);

-- Static objects in the world
CREATE TABLE IF NOT EXISTS objects (
  id VARCHAR(50) PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  emoji VARCHAR(10),
  x INTEGER NOT NULL,
  y INTEGER NOT NULL,
  description TEXT,
  can_interact BOOLEAN DEFAULT false,
  interact_result TEXT,
  blocking BOOLEAN DEFAULT false
);

-- Relationships between characters
CREATE TABLE IF NOT EXISTS relationships (
  char1_id VARCHAR(50) REFERENCES characters(id) ON DELETE CASCADE,
  char2_id VARCHAR(50) REFERENCES characters(id) ON DELETE CASCADE,
  sentiment INTEGER DEFAULT 0,
  interactions INTEGER DEFAULT 0,
  first_met_tick BIGINT,
  last_interaction_tick BIGINT,
  PRIMARY KEY (char1_id, char2_id)
);

-- Character memories
CREATE TABLE IF NOT EXISTS memories (
  id SERIAL PRIMARY KEY,
  character_id VARCHAR(50) REFERENCES characters(id) ON DELETE CASCADE,
  tick BIGINT,
  content TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);

-- Conversation log
CREATE TABLE IF NOT EXISTS conversations (
  id SERIAL PRIMARY KEY,
  tick BIGINT,
  speaker_id VARCHAR(50) REFERENCES characters(id) ON DELETE CASCADE,
  listener_id VARCHAR(50) REFERENCES characters(id) ON DELETE CASCADE,
  message TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);

-- Activity log (public)
CREATE TABLE IF NOT EXISTS activity_log (
  id SERIAL PRIMARY KEY,
  tick BIGINT,
  character_id VARCHAR(50),
  action VARCHAR(50),
  message TEXT,
  created_at TIMESTAMP DEFAULT NOW()
);

-- Create indexes for common queries
CREATE INDEX IF NOT EXISTS idx_characters_active ON characters(is_active);
CREATE INDEX IF NOT EXISTS idx_conversations_tick ON conversations(tick);
CREATE INDEX IF NOT EXISTS idx_activity_log_tick ON activity_log(tick);
CREATE INDEX IF NOT EXISTS idx_memories_character ON memories(character_id);

-- Initialize world if not exists
INSERT INTO world (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
`;

const defaultObjects = `
-- Default objects (only insert if objects table is empty)
INSERT INTO objects (id, name, emoji, x, y, description, can_interact, interact_result, blocking)
SELECT * FROM (VALUES
  ('tree1', 'Old Pine', '🌲', 3, 2, 'A towering pine tree with thick bark. Birds nest above.', true, 'You shake the tree. A pinecone falls.', false),
  ('tree2', 'Willow', '🌳', 15, 5, 'A graceful willow tree with drooping branches.', true, 'You rest under the shade. Peaceful.', false),
  ('rock1', 'Mossy Boulder', '🪨', 2, 8, 'An ancient boulder covered in soft moss. Strange markings underneath.', true, 'You push it but it wont budge. The markings seem to glow briefly.', false),
  ('rock2', 'Flat Stone', '🪨', 12, 11, 'A flat stone, perfect for sitting.', true, 'You sit and rest. +5 HP restored.', false),
  ('house1', 'Cottage', '🏠', 17, 3, 'A cozy stone cottage with smoke from the chimney.', true, 'You knock. No answer, but you hear movement inside.', false),
  ('house2', 'Old Mill', '🏚️', 5, 12, 'An abandoned mill. The waterwheel has stopped.', true, 'You peek inside. Dusty but salvageable.', false),
  ('flowers1', 'Wild Flowers', '🌸', 8, 4, 'Pink and purple wildflowers swaying in the breeze.', true, 'You pick a small bouquet. Sweet scent.', false),
  ('flowers2', 'Sunflowers', '🌻', 14, 9, 'Tall sunflowers following the sun.', true, 'You find some seeds. Could be useful.', false),
  ('pond', 'Pond', '💧', 10, 7, 'A small clear pond. Fish dart below the surface.', true, 'You drink the cool water. Refreshing!', false),
  ('river1', 'River', '〰️', 0, 10, 'A river flows from west to east.', false, NULL, true),
  ('river2', 'River', '〰️', 1, 10, 'A river flows from west to east.', false, NULL, true),
  ('river3', 'River', '〰️', 2, 10, 'A river flows from west to east.', false, NULL, true),
  ('river4', 'River', '〰️', 3, 10, 'A river flows from west to east.', false, NULL, true),
  ('bridge', 'Bridge', '🌉', 4, 10, 'A wooden bridge crossing the river.', false, NULL, false),
  ('river5', 'River', '〰️', 5, 10, 'A river flows from west to east.', false, NULL, true),
  ('river6', 'River', '〰️', 6, 10, 'A river flows from west to east.', false, NULL, true),
  ('campfire', 'Campfire', '🔥', 9, 2, 'A warm campfire. Good place to meet others.', true, 'You warm your hands. Cozy.', false),
  ('sign', 'Signpost', '🪧', 10, 5, 'A wooden signpost with directions.', true, 'It reads: North - Cottage, South - Mill, East - Unknown', false)
) AS v(id, name, emoji, x, y, description, can_interact, interact_result, blocking)
WHERE NOT EXISTS (SELECT 1 FROM objects LIMIT 1);
`;

async function init() {
  console.log('🗄️  Initializing database...');
  
  try {
    await pool.query(schema);
    console.log('✅ Schema created');
    
    await pool.query(defaultObjects);
    console.log('✅ Default objects added');
    
    // Check world state
    const world = await pool.query('SELECT * FROM world WHERE id = 1');
    console.log('🌍 World state:', world.rows[0]);
    
    const objects = await pool.query('SELECT COUNT(*) FROM objects');
    console.log(`📦 Objects in world: ${objects.rows[0].count}`);
    
    console.log('\n✨ Database ready!');
  } catch (err) {
    console.error('❌ Error:', err.message);
    process.exit(1);
  } finally {
    await pool.end();
  }
}

init();
