// Script to clean up duplicate "first meeting" milestones
// Run with: DATABASE_URL="..." node cleanup-milestones.js

const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.NODE_ENV === 'production' ? { rejectUnauthorized: false } : false
});

async function cleanup() {
  console.log('🔄 Cleaning up duplicate milestones...\n');
  
  try {
    // Get all characters
    const chars = await pool.query('SELECT id, name, significant_moments FROM characters');
    
    for (const char of chars.rows) {
      let moments = char.significant_moments || [];
      const originalCount = moments.length;
      
      // Keep only the first occurrence of each "first meeting" moment
      const seenMeetings = new Set();
      moments = moments.filter(m => {
        if (m.moment && m.moment.includes('for the first time')) {
          if (seenMeetings.has(m.moment)) {
            return false; // Skip duplicate
          }
          seenMeetings.add(m.moment);
        }
        return true;
      });
      
      const removedCount = originalCount - moments.length;
      
      if (removedCount > 0) {
        await pool.query(
          'UPDATE characters SET significant_moments = $1 WHERE id = $2',
          [JSON.stringify(moments), char.id]
        );
        console.log(`${char.name}: Removed ${removedCount} duplicate milestone(s)`);
      } else {
        console.log(`${char.name}: No duplicates found`);
      }
    }
    
    console.log('\n✨ Cleanup complete!');
  } catch (err) {
    console.error('❌ Error:', err.message);
  } finally {
    await pool.end();
  }
}

cleanup();
