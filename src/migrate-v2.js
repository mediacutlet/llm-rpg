// Migration: Add energy system and conversation fatigue
// Run with: DATABASE_URL="..." node src/migrate-v2.js

const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.NODE_ENV === 'production' ? { rejectUnauthorized: false } : false
});

async function migrate() {
  console.log('🔄 Running migration v2: Energy & Conversation Fatigue...\n');
  
  try {
    // Add energy to characters
    await pool.query(`
      ALTER TABLE characters 
      ADD COLUMN IF NOT EXISTS energy INTEGER DEFAULT 100,
      ADD COLUMN IF NOT EXISTS max_energy INTEGER DEFAULT 100
    `);
    console.log('✅ Added energy columns to characters');
    
    // Add conversation tracking to relationships
    await pool.query(`
      ALTER TABLE relationships
      ADD COLUMN IF NOT EXISTS recent_exchanges INTEGER DEFAULT 0,
      ADD COLUMN IF NOT EXISTS cooldown_until_tick BIGINT DEFAULT 0
    `);
    console.log('✅ Added conversation tracking to relationships');
    
    // Add day/night cycle to world
    await pool.query(`
      ALTER TABLE world
      ADD COLUMN IF NOT EXISTS is_night BOOLEAN DEFAULT false,
      ADD COLUMN IF NOT EXISTS day_length INTEGER DEFAULT 200,
      ADD COLUMN IF NOT EXISTS night_length INTEGER DEFAULT 50
    `);
    console.log('✅ Added day/night cycle to world');
    
    // Fix any NULL energy values (IMPORTANT: This handles the NULL arithmetic bug)
    const nullCount = await pool.query('SELECT COUNT(*) FROM characters WHERE energy IS NULL');
    if (nullCount.rows[0].count > 0) {
      console.log(`⚠️ Found ${nullCount.rows[0].count} characters with NULL energy, fixing...`);
    }
    
    // Reset/fix everyone's energy (handles NULL values)
    await pool.query('UPDATE characters SET energy = COALESCE(energy, 100), max_energy = COALESCE(max_energy, 100)');
    console.log('✅ Fixed all character energy values (NULL → 100)');
    
    // Reset conversation exchanges
    await pool.query('UPDATE relationships SET recent_exchanges = COALESCE(recent_exchanges, 0), cooldown_until_tick = COALESCE(cooldown_until_tick, 0)');
    console.log('✅ Reset all conversation exchanges');
    
    console.log('\n✨ Migration complete!');
  } catch (err) {
    console.error('❌ Migration error:', err.message);
  } finally {
    await pool.end();
  }
}

migrate();
