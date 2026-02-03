// Migration v4: Add server-side protection columns
// Run with: node src/migrate-v4-protections.js

const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.NODE_ENV === 'production' ? { rejectUnauthorized: false } : false
});

async function migrate() {
  console.log('🔄 Running v4 migration: Server-side protections...');
  
  try {
    // Add last_speaker_id to relationships to track who spoke last
    await pool.query(`
      ALTER TABLE relationships 
      ADD COLUMN IF NOT EXISTS last_speaker_id VARCHAR(50),
      ADD COLUMN IF NOT EXISTS consecutive_count INTEGER DEFAULT 0,
      ADD COLUMN IF NOT EXISTS last_message_tick BIGINT DEFAULT 0
    `);
    console.log('✅ Added conversation tracking columns to relationships');
    
    // Add goodbye cooldown tracking (separate from conversation fatigue)
    await pool.query(`
      ALTER TABLE relationships 
      ADD COLUMN IF NOT EXISTS goodbye_cooldown_until BIGINT DEFAULT 0
    `);
    console.log('✅ Added goodbye cooldown column');
    
    // Create rate limiting table (for tracking recent actions)
    await pool.query(`
      CREATE TABLE IF NOT EXISTS rate_limits (
        character_id VARCHAR(50) REFERENCES characters(id) ON DELETE CASCADE,
        action_type VARCHAR(20),
        action_tick BIGINT,
        PRIMARY KEY (character_id, action_type, action_tick)
      )
    `);
    console.log('✅ Created rate_limits table');
    
    // Create index for efficient rate limit queries
    await pool.query(`
      CREATE INDEX IF NOT EXISTS idx_rate_limits_char_action 
      ON rate_limits(character_id, action_type, action_tick)
    `);
    console.log('✅ Created rate_limits index');
    
    console.log('\n✨ Migration v4 complete!');
  } catch (err) {
    console.error('❌ Migration error:', err.message);
    process.exit(1);
  } finally {
    await pool.end();
  }
}

migrate();
