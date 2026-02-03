// Migration v5: Multi-Zone World System
// Run with: node src/migrate-v5-zones.js

const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.NODE_ENV === 'production' ? { rejectUnauthorized: false } : false
});

async function migrate() {
  console.log('🔄 Running v5 migration: Multi-Zone World System...');
  
  try {
    // Create zones table
    await pool.query(`
      CREATE TABLE IF NOT EXISTS zones (
        id VARCHAR(50) PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        description TEXT,
        width INTEGER DEFAULT 20,
        height INTEGER DEFAULT 15,
        is_safe BOOLEAN DEFAULT false,
        danger_level INTEGER DEFAULT 0,
        ambient_description TEXT,
        created_at TIMESTAMP DEFAULT NOW()
      )
    `);
    console.log('✅ Created zones table');

    // Create zone connections (portals/paths between zones)
    await pool.query(`
      CREATE TABLE IF NOT EXISTS zone_connections (
        id SERIAL PRIMARY KEY,
        from_zone VARCHAR(50) REFERENCES zones(id) ON DELETE CASCADE,
        to_zone VARCHAR(50) REFERENCES zones(id) ON DELETE CASCADE,
        from_x INTEGER NOT NULL,
        from_y INTEGER NOT NULL,
        to_x INTEGER NOT NULL,
        to_y INTEGER NOT NULL,
        name VARCHAR(100),
        emoji VARCHAR(10) DEFAULT '🚪',
        description TEXT,
        UNIQUE(from_zone, from_x, from_y)
      )
    `);
    console.log('✅ Created zone_connections table');

    // Add zone column to characters
    await pool.query(`
      ALTER TABLE characters 
      ADD COLUMN IF NOT EXISTS current_zone VARCHAR(50) DEFAULT 'meadow'
    `);
    console.log('✅ Added current_zone to characters');

    // Add zone column to objects
    await pool.query(`
      ALTER TABLE objects 
      ADD COLUMN IF NOT EXISTS zone_id VARCHAR(50) DEFAULT 'meadow'
    `);
    console.log('✅ Added zone_id to objects');

    // Insert default zones
    await pool.query(`
      INSERT INTO zones (id, name, description, width, height, is_safe, danger_level, ambient_description)
      VALUES 
        ('meadow', 'The Meadow', 'A peaceful meadow with tall grass swaying in the breeze. A safe place to rest and socialize.', 20, 15, true, 0, 'Butterflies flutter between wildflowers. The air smells of fresh grass.'),
        ('dark_forest', 'The Dark Forest', 'Ancient trees block out most sunlight. Strange sounds echo between the trunks.', 25, 20, false, 2, 'Shadows move between the trees. Twigs snap in the distance.'),
        ('caves', 'The Crystal Caves', 'A network of underground caverns lit by glowing crystals. Dangerous creatures lurk in the depths.', 20, 25, false, 4, 'Crystals hum with faint energy. Water drips somewhere in the darkness.'),
        ('ruins', 'The Ancient Ruins', 'Crumbling stone structures from a forgotten civilization. The air feels heavy with old magic.', 18, 18, false, 5, 'Whispers seem to come from the walls. The stones are cold to the touch.')
      ON CONFLICT (id) DO UPDATE SET
        name = EXCLUDED.name,
        description = EXCLUDED.description,
        width = EXCLUDED.width,
        height = EXCLUDED.height,
        is_safe = EXCLUDED.is_safe,
        danger_level = EXCLUDED.danger_level,
        ambient_description = EXCLUDED.ambient_description
    `);
    console.log('✅ Inserted default zones');

    // Insert zone connections
    await pool.query(`
      INSERT INTO zone_connections (from_zone, to_zone, from_x, from_y, to_x, to_y, name, emoji, description)
      VALUES 
        -- Meadow exits
        ('meadow', 'dark_forest', 19, 7, 0, 10, 'Forest Path', '🌲', 'A winding path leads into the dark forest'),
        ('meadow', 'caves', 10, 14, 10, 0, 'Cave Entrance', '🕳️', 'A dark opening in the hillside'),
        
        -- Dark Forest exits
        ('dark_forest', 'meadow', 0, 10, 19, 7, 'Meadow Path', '🌸', 'The path back to the peaceful meadow'),
        ('dark_forest', 'ruins', 24, 10, 0, 9, 'Overgrown Trail', '🏚️', 'An ancient trail leads to crumbling ruins'),
        
        -- Caves exits  
        ('caves', 'meadow', 10, 0, 10, 14, 'Cave Exit', '☀️', 'Daylight filters in from above'),
        ('caves', 'ruins', 10, 24, 9, 17, 'Underground Passage', '🚪', 'A carved passage leads deeper'),
        
        -- Ruins exits
        ('ruins', 'dark_forest', 0, 9, 24, 10, 'Forest Edge', '🌲', 'The forest encroaches on the ruins'),
        ('ruins', 'caves', 9, 17, 10, 24, 'Crypt Stairs', '🕳️', 'Stairs descend into darkness')
      ON CONFLICT (from_zone, from_x, from_y) DO UPDATE SET
        to_zone = EXCLUDED.to_zone,
        to_x = EXCLUDED.to_x,
        to_y = EXCLUDED.to_y,
        name = EXCLUDED.name,
        emoji = EXCLUDED.emoji,
        description = EXCLUDED.description
    `);
    console.log('✅ Inserted zone connections');

    // Update existing objects to be in meadow zone
    await pool.query(`UPDATE objects SET zone_id = 'meadow' WHERE zone_id IS NULL`);
    console.log('✅ Updated existing objects to meadow zone');

    // Add objects to other zones
    await pool.query(`
      INSERT INTO objects (id, name, emoji, x, y, description, can_interact, interact_result, blocking, zone_id)
      VALUES
        -- Dark Forest objects
        ('df_tree1', 'Twisted Oak', '🌳', 5, 5, 'A massive oak tree with gnarled branches.', true, 'The bark feels ancient and rough.', false, 'dark_forest'),
        ('df_tree2', 'Dead Tree', '🥀', 12, 8, 'A lifeless tree, blackened by some unknown force.', true, 'It crumbles slightly at your touch.', false, 'dark_forest'),
        ('df_shrine', 'Forest Shrine', '⛩️', 20, 15, 'A small shrine covered in moss. Offerings lay scattered.', true, 'You feel a brief sense of peace.', false, 'dark_forest'),
        ('df_pond', 'Dark Pond', '🌊', 8, 12, 'Still, black water reflects nothing.', true, 'The water is ice cold.', false, 'dark_forest'),
        ('df_campfire', 'Abandoned Campfire', '🔥', 15, 5, 'Someone camped here recently. The embers are still warm.', true, 'You warm yourself by the fire. Energy restored!', false, 'dark_forest'),
        
        -- Cave objects
        ('cave_crystal1', 'Blue Crystal', '💎', 5, 10, 'A large blue crystal pulses with inner light.', true, 'The crystal hums when touched.', false, 'caves'),
        ('cave_crystal2', 'Red Crystal', '🔴', 15, 15, 'A deep red crystal radiates warmth.', true, 'Heat emanates from within.', false, 'caves'),
        ('cave_pool', 'Underground Pool', '💧', 10, 12, 'Crystal-clear water in a stone basin.', true, 'The water tastes pure and refreshing.', false, 'caves'),
        ('cave_bones', 'Old Bones', '🦴', 3, 20, 'Scattered bones of unknown creatures.', true, 'Best not to linger here.', false, 'caves'),
        ('cave_campfire', 'Miner''s Camp', '🏕️', 8, 5, 'An old mining camp with supplies.', true, 'You rest and recover your strength.', false, 'caves'),
        
        -- Ruins objects
        ('ruins_pillar1', 'Broken Pillar', '🏛️', 5, 5, 'A crumbling stone pillar with faded inscriptions.', true, 'The writing is in an unknown language.', false, 'ruins'),
        ('ruins_pillar2', 'Intact Pillar', '🏛️', 12, 5, 'One of the few standing pillars.', true, 'It depicts scenes of an ancient battle.', false, 'ruins'),
        ('ruins_altar', 'Ancient Altar', '⚱️', 9, 9, 'A stone altar stained with age.', true, 'You feel watched.', false, 'ruins'),
        ('ruins_fountain', 'Dry Fountain', '⛲', 9, 14, 'A fountain that hasn''t flowed in centuries.', true, 'Coins from another era line the bottom.', false, 'ruins'),
        ('ruins_statue', 'Weathered Statue', '🗿', 15, 12, 'A statue of a forgotten deity.', true, 'Its eyes seem to follow you.', false, 'ruins'),
        ('ruins_campfire', 'Sheltered Corner', '🔥', 3, 15, 'A relatively safe spot to rest.', true, 'You catch your breath.', false, 'ruins')
      ON CONFLICT (id) DO UPDATE SET
        zone_id = EXCLUDED.zone_id,
        x = EXCLUDED.x,
        y = EXCLUDED.y
    `);
    console.log('✅ Added objects to other zones');

    // Create indexes for efficient zone queries
    await pool.query(`
      CREATE INDEX IF NOT EXISTS idx_characters_zone ON characters(current_zone);
      CREATE INDEX IF NOT EXISTS idx_objects_zone ON objects(zone_id);
      CREATE INDEX IF NOT EXISTS idx_zone_connections_from ON zone_connections(from_zone);
    `);
    console.log('✅ Created indexes');

    console.log('\n✨ Migration v5 complete!');
    console.log('\n📍 Zones created:');
    const zones = await pool.query('SELECT id, name, is_safe, danger_level FROM zones ORDER BY danger_level');
    zones.rows.forEach(z => {
      console.log(`   ${z.is_safe ? '🏠' : '⚔️'} ${z.name} (danger: ${z.danger_level})`);
    });

  } catch (err) {
    console.error('❌ Migration error:', err.message);
    process.exit(1);
  } finally {
    await pool.end();
  }
}

migrate();
