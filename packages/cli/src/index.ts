#!/usr/bin/env node

import { Command } from 'commander';
import { registerSearchCommand } from './commands/search.js';
import { registerFetchCommand } from './commands/fetch.js';
import { registerCrawlCommand } from './commands/crawl.js';
import { registerWaybackCommand } from './commands/wayback.js';

const program = new Command();

program
  .name('web-tools')
  .description('CLI for web search, scraping, and archival tools')
  .version('0.1.0')
  .option('--json', 'Output raw JSON (default: pretty-printed)');

registerSearchCommand(program);
registerFetchCommand(program);
registerCrawlCommand(program);
registerWaybackCommand(program);

program.parseAsync(process.argv).catch((err) => {
  console.error('Error:', err instanceof Error ? err.message : String(err));
  process.exit(1);
});
