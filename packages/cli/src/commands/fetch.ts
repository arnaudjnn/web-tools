import type { Command } from 'commander';
import { web_fetch, web_screenshot, web_pdf, web_execute_js } from '@web-tools/toolkit';

export function registerFetchCommand(program: Command) {
  program
    .command('fetch')
    .description('Fetch a URL and return its content as markdown')
    .argument('<url>', 'URL to fetch')
    .option('-f, --filter <strategy>', 'Content filter: raw, fit, bm25, llm (default: fit)')
    .option('-q, --query <query>', 'Query for BM25/LLM filter')
    .action(async (url: string, opts: { filter?: string; query?: string }) => {
      const result = await web_fetch({ url, f: opts.filter, q: opts.query });

      if (result.isError) {
        console.error(result.content[0]?.text ?? 'Unknown error');
        process.exit(1);
      }

      for (const c of result.content) {
        if (c.text) console.log(c.text);
      }
    });

  program
    .command('screenshot')
    .description('Capture a full-page PNG screenshot of a URL')
    .argument('<url>', 'URL to screenshot')
    .option('-w, --wait <seconds>', 'Seconds to wait before capture', '2')
    .action(async (url: string, opts: { wait: string }) => {
      const result = await web_screenshot({
        url,
        screenshot_wait_for: parseFloat(opts.wait),
      });

      if (result.isError) {
        console.error(result.content[0]?.text ?? 'Unknown error');
        process.exit(1);
      }

      for (const c of result.content) {
        if (c.text) console.log(c.text);
      }
    });

  program
    .command('pdf')
    .description('Generate a PDF of a URL')
    .argument('<url>', 'URL to convert to PDF')
    .action(async (url: string) => {
      const result = await web_pdf({ url });

      if (result.isError) {
        console.error(result.content[0]?.text ?? 'Unknown error');
        process.exit(1);
      }

      for (const c of result.content) {
        if (c.text) console.log(c.text);
      }
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

      const result = await web_execute_js({ url, scripts: opts.script });

      if (result.isError) {
        console.error(result.content[0]?.text ?? 'Unknown error');
        process.exit(1);
      }

      for (const c of result.content) {
        if (c.text) console.log(c.text);
      }
    });
}

function collect(value: string, previous: string[]): string[] {
  return [...previous, value];
}
