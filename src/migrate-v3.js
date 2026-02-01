// Migration: Add hunger system and market
// Run with: DATABASE_URL="..." node src/migrate-v3.js

const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.NODE_ENV === 'production' ? { rejectUnauthorized: false } : false
});

async function migrate() {
  console.log('🔄 Running migration v3: Hunger System & Market...\n');
  
  try {
    // Add hunger to characters
    await pool.query(`
      ALTER TABLE characters 
      ADD COLUMN IF NOT EXISTS hunger INTEGER DEFAULT 100,
      ADD COLUMN IF NOT EXISTS max_hunger INTEGER DEFAULT 100
    `);
    console.log('✅ Added hunger columns to characters');
    
    // Fix any NULL hunger values
    await pool.query('UPDATE characters SET hunger = COALESCE(hunger, 100), max_hunger = COALESCE(max_hunger, 100)');
    console.log('✅ Fixed all character hunger values (NULL → 100)');
    
    // Check if market already exists
    const existingMarket = await pool.query("SELECT * FROM objects WHERE id = 'market'");
    
    if (existingMarket.rows.length === 0) {
      // Add the market - central location for food and socializing
      await pool.query(`
        INSERT INTO objects (id, name, emoji, x, y, description, can_interact, interact_result, blocking)
        VALUES (
          'market',
          'Market',
          '🏪',
          10,
          7,
          'A bustling market stall with fresh food and warm drinks. Characters gather here to eat and socialize.',
          true,
          'eat',
          false
        )
      `);
      console.log('✅ Added Market at (10, 7)');
    } else {
      console.log('ℹ️ Market already exists');
    }
    
    // Show current objects
    const objects = await pool.query('SELECT id, name, emoji, x, y FROM objects ORDER BY name');
    console.log('\n📍 Current world objects:');
    for (const obj of objects.rows) {
      console.log(`  ${obj.emoji} ${obj.name} at (${obj.x}, ${obj.y})`);
    }
    
    console.log('\n✨ Migration v3 complete!');
  } catch (err) {
    console.error('❌ Migration error:', err.message);
  } finally {
    await pool.end();
  }
}

migrate();
