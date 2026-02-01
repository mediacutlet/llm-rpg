// Wipe all world data - run with: node wipe-world.js
const Database = require('better-sqlite3');
const path = require('path');

const dbPath = process.env.DATABASE_PATH || path.join(__dirname, 'data', 'game.db');
const db = new Database(dbPath);

console.log('⚠️  WIPING ALL WORLD DATA...\n');

// Delete in order to respect foreign keys
const tables = [
  'conversation_summaries',
  'conversations', 
  'conversation_fatigue',
  'milestones',
  'characters'
];

for (const table of tables) {
  try {
    const result = db.prepare(`DELETE FROM ${table}`).run();
    console.log(`  ✓ ${table}: deleted ${result.changes} rows`);
  } catch (e) {
    console.log(`  ✗ ${table}: ${e.message}`);
  }
}

// Reset world tick
try {
  db.prepare(`UPDATE world SET tick = 0`).run();
  console.log('  ✓ world tick reset to 0');
} catch (e) {
  console.log(`  ✗ world tick: ${e.message}`);
}

console.log('\n✅ World wiped! Restart the server to begin fresh.');
db.close();
