// View and clean conversation summaries
// Run with: DATABASE_URL="..." node cleanup-summaries.js [--clean]

const { Pool } = require('pg');

const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  ssl: process.env.NODE_ENV === 'production' ? { rejectUnauthorized: false } : false
});

const shouldClean = process.argv.includes('--clean');

async function run() {
  console.log('📚 Conversation Summaries Report\n');
  
  try {
    // Get all relationships with summaries
    const rels = await pool.query(`
      SELECT r.char1_id, r.char2_id, r.conversation_summaries,
             c1.name as char1_name, c2.name as char2_name
      FROM relationships r
      JOIN characters c1 ON r.char1_id = c1.id
      JOIN characters c2 ON r.char2_id = c2.id
      WHERE r.conversation_summaries IS NOT NULL 
        AND jsonb_array_length(r.conversation_summaries) > 0
    `);
    
    console.log(`Found ${rels.rows.length} relationships with summaries:\n`);
    
    for (const rel of rels.rows) {
      const summaries = rel.conversation_summaries || [];
      console.log(`\n${rel.char1_name} → ${rel.char2_name}: ${summaries.length} summaries`);
      console.log('─'.repeat(50));
      
      let goodSummaries = [];
      let badCount = 0;
      
      for (const s of summaries) {
        const title = s.title || 'No title';
        const summary = s.summary || '';
        const topics = s.topics || [];
        
        // Check if it's a "bad" summary (too short, generic title, etc)
        const isBad = !summary || 
                     summary.length < 30 || 
                     title.toLowerCase() === 'conversation' ||
                     title.toLowerCase().includes('no title');
        
        if (isBad) {
          console.log(`  ❌ [BAD] "${title}"`);
          console.log(`     Summary: "${summary.slice(0, 100)}..."`);
          badCount++;
        } else {
          console.log(`  ✅ "${title}"`);
          console.log(`     ${summary.slice(0, 150)}...`);
          if (topics.length) console.log(`     Topics: ${topics.join(', ')}`);
          goodSummaries.push(s);
        }
      }
      
      if (shouldClean && badCount > 0) {
        // Keep only good summaries
        await pool.query(`
          UPDATE relationships 
          SET conversation_summaries = $1
          WHERE char1_id = $2 AND char2_id = $3
        `, [JSON.stringify(goodSummaries), rel.char1_id, rel.char2_id]);
        console.log(`\n  🧹 Cleaned: Removed ${badCount} bad summaries, kept ${goodSummaries.length}`);
      }
    }
    
    if (!shouldClean) {
      console.log('\n\n💡 Run with --clean flag to remove bad summaries');
    }
    
    // Also show the old memories table stats
    const memCount = await pool.query('SELECT COUNT(*) FROM memories');
    console.log(`\n📝 Old memories table has ${memCount.rows[0].count} entries`);
    
  } catch (err) {
    console.error('❌ Error:', err.message);
  } finally {
    await pool.end();
  }
}

run();
