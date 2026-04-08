import type { Command } from 'commander';
import { web_snapshots, web_archive } from '@web-tools/toolkit';

export function registerWaybackCommand(program: Command) {
  program
    .command('snapshots')
    .description('List Wayback Machine snapshots for a URL')
    .argument('<url>', 'URL to check')
    .option('--from <date>', 'Start date (YYYYMMDD)')
    .option('--to <date>', 'End date (YYYYMMDD)')
    .option('-l, --limit <n>', 'Max snapshots (default: 100)', '100')
    .option('--match <type>', 'Match type: exact, prefix, host, domain', 'exact')
    .action(
      async (
        url: string,
        opts: { from?: string; to?: string; limit: string; match: string },
      ) => {
        const snapshots = await web_snapshots({
          url,
          from: opts.from,
          to: opts.to,
          limit: parseInt(opts.limit, 10),
          match_type: opts.match as 'exact' | 'prefix' | 'host' | 'domain',
        });

        if (program.opts().json) {
          console.log(JSON.stringify(snapshots, null, 2));
          return;
        }

        if (snapshots.length === 0) {
          console.log('No snapshots found.');
          return;
        }

        console.log(`Found ${snapshots.length} snapshot(s):\n`);
        for (const s of snapshots) {
          console.log(`  ${s.formattedDate}  ${s.statusCode}  ${s.mimetype}`);
          console.log(`  ${s.archiveUrl}\n`);
        }
      },
    );

  program
    .command('archive')
    .description('Retrieve an archived page from the Wayback Machine')
    .argument('<url>', 'URL to retrieve')
    .option('-t, --timestamp <ts>', 'Timestamp (YYYYMMDDHHMMSS)', '')
    .option('--original', 'Get original content without Wayback banner')
    .action(async (url: string, opts: { timestamp: string; original?: boolean }) => {
      if (!opts.timestamp) {
        console.error('--timestamp is required');
        process.exit(1);
      }

      const result = await web_archive({
        url,
        timestamp: opts.timestamp,
        original: opts.original,
      });

      if (program.opts().json) {
        console.log(JSON.stringify(result, null, 2));
        return;
      }

      console.log(`Wayback URL: ${result.waybackUrl}`);
      console.log(`Content length: ${result.contentLength} characters\n`);
      console.log(result.content);
    });
}
