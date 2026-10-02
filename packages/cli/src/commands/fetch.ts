import type { Command } from 'commander';
import { printResult, runTool } from '../run.js';

export function registerFetchCommand(program: Command) {
  program
    .command('fetch')
    .description('Fetch a URL and return its content as markdown')
    .argument('<url>', 'URL to fetch')
    .option('-f, --filter <strategy>', 'Content filter: raw, fit (default: fit)')
    .action(async (url: string, opts: { filter?: string }) => {
      printResult(await runTool('web_fetch', { url, ...(opts.filter ? { f: opts.filter } : {}) }));
    });

  program
    .command('screenshot')
    .description('Capture a full-page PNG screenshot of a URL')
    .argument('<url>', 'URL to screenshot')
    .option('-w, --wait <seconds>', 'Seconds to wait before capture', '2')
    .action(async (url: string, opts: { wait: string }) => {
      printResult(await runTool('web_screenshot', {
        url,
        screenshot_wait_for: parseFloat(opts.wait),
      }));
    });

  program
    .command('pdf')
    .description('Generate a PDF of a URL')
    .argument('<url>', 'URL to convert to PDF')
    .action(async (url: string) => {
      printResult(await runTool('web_pdf', { url }));
    });

  program
    .command('execute-js')
    .description('Execute JavaScript on a URL')
    .argument('<url>', 'URL to execute scripts on')
    .option('-s, --script <code>', 'JavaScript snippet to execute (repeatable)', collect, [])
    .action(async (url: string, opts: { script: string[] }) => {
      if (opts.script.length === 0) {
        console.error('At least one --script is required');
        process.exit(1);
      }

      printResult(await runTool('web_execute_js', { url, scripts: opts.script }));
    });
}

function collect(value: string, previous: string[]): string[] {
  return [...previous, value];
}
