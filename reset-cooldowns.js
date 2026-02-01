// Quick script to reset all conversation cooldowns
// Run with: DATABASE_URL="..." node reset-cooldowns.js

const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.NODE_ENV === 'production' ? { rejectUnauthorized: false } : false
});

async function reset() {
  console.log('🔄 Resetting all conversation cooldowns...\n');
  
  try {
    // Show current state
    const before = await pool.query('SELECT * FROM relationships');
    console.log('Current relationships:');
    for (const r of before.rows) {
      console.log(`  ${r.char1_id} -> ${r.char2_id}: exchanges=${r.recent_exchanges}, cooldown=${r.cooldown_until_tick}`);
    }
    
    // Reset
    await pool.query('UPDATE relationships SET recent_exchanges = 0, cooldown_until_tick = 0');
    console.log('\n✅ All cooldowns reset to 0!');
    
    // Show current tick for reference
    const world = await pool.query('SELECT tick FROM world WHERE id = 1');
    console.log(`Current world tick: ${world.rows[0]?.tick}`);
    
  } catch (err) {
    console.error('❌ Error:', err.message);
  } finally {
    await pool.end();
  }
}

reset();
