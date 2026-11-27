const GrowwAPI = require('growwapi');
const fs = require('fs');
const path = require('path');

async function main() {
  const groww = new GrowwAPI({
    username: process.env.GROWW_USERNAME,
    password: process.env.GROWW_PASSWORD,
    twoFA: process.env.GROWW_2FA_CODE,
  });

  await groww.init();
  const holdings = await groww.holdings.list();

  const folder = process.env.HOLDINGS_FOLDER || 'holdings';
  fs.mkdirSync(folder, { recursive: true });
  const ts = new Date().toISOString().replace(/[:.]/g, '-');
  const outPath = path.join(folder, `holdings_${ts}.json`);
  fs.writeFileSync(outPath, JSON.stringify(holdings, null, 2));
  console.log(`Saved holdings to ${outPath}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
