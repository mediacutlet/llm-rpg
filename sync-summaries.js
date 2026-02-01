// Sync conversation summaries bidirectionally
// Run with: DATABASE_URL="..." node sync-summaries.js

const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.NODE_ENV === 'production' ? { rejectUnauthorized: false } : false
});

async function sync() {
  console.log('🔄 Syncing conversation summaries bidirectionally...\n');
  
  try {
    // Get all relationships with summaries
    const rels = await pool.query(`
      SELECT char1_id, char2_id, conversation_summaries
      FROM relationships
      WHERE conversation_summaries IS NOT NULL 
        AND jsonb_array_length(conversation_summaries) > 0
    `);
    
    console.log(`Found ${rels.rows.length} relationships with summaries\n`);
    
    // Group by character pairs (both directions)
    const pairs = new Map();
    
    for (const rel of rels.rows) {
      const key1 = `${rel.char1_id}|${rel.char2_id}`;
      const key2 = `${rel.char2_id}|${rel.char1_id}`;
      
      // Use sorted key for grouping
      const sortedKey = [rel.char1_id, rel.char2_id].sort().join('|');
      
      if (!pairs.has(sortedKey)) {
        pairs.set(sortedKey, { summaries: [], char1: rel.char1_id, char2: rel.char2_id });
      }
      
      const existing = pairs.get(sortedKey);
      const summaries = rel.conversation_summaries || [];
      
      // Add summaries with their ticks to dedupe
      for (const s of summaries) {
        const isDupe = existing.summaries.some(e => e.tick === s.tick);
        if (!isDupe) {
          existing.summaries.push(s);
        }
      }
    }
    
    console.log(`Found ${pairs.size} unique character pairs\n`);
    
    // Now sync each pair
    let synced = 0;
    for (const [key, data] of pairs) {
      const [id1, id2] = key.split('|');
      
      // Sort summaries by tick
      data.summaries.sort((a, b) => (a.tick || 0) - (b.tick || 0));
      
      // Keep last 20
      const summaries = data.summaries.slice(-20);
      
      console.log(`${id1} <-> ${id2}: ${summaries.length} summaries`);
      
      // Update both directions
      await pool.query(`
        INSERT INTO relationships (char1_id, char2_id, conversation_summaries, first_met_tick)
        VALUES ($1, $2, $3, 0)
        ON CONFLICT (char1_id, char2_id) 
        DO UPDATE SET conversation_summaries = $3
      `, [id1, id2, JSON.stringify(summaries)]);
      
      await pool.query(`
        INSERT INTO relationships (char1_id, char2_id, conversation_summaries, first_met_tick)
        VALUES ($1, $2, $3, 0)
        ON CONFLICT (char1_id, char2_id) 
        DO UPDATE SET conversation_summaries = $3
      `, [id2, id1, JSON.stringify(summaries)]);
      
      synced++;
    }
    
    console.log(`\n✅ Synced ${synced} character pairs bidirectionally`);
    
  } catch (err) {
    console.error('Error:', err);
  } finally {
    await pool.end();
  }
}

sync();
