import { createServer } from 'node:http';
import { pipeline } from 'node:stream/promises';
import { timingSafeEqual } from 'node:crypto';
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { createMcpHandler, McpServer } from '@modelcontextprotocol/server';
import { serveStdio } from '@modelcontextprotocol/server/stdio';
import { z } from 'zod';

const contractPath = new URL('../protocol_contract.json', import.meta.url);
const contract = JSON.parse(readFileSync(contractPath, 'utf8'));
const coreFixturesPath = new URL('../core_fixtures.json', import.meta.url);
const coreFixtures = JSON.parse(readFileSync(coreFixturesPath, 'utf8'));
const maximumBodyBytes = contract.max_http_request_bytes;
const maximumCoreInputBytes = 131_072;
const maximumCoreOutputBytes = 131_072;
const coreTimeoutMs = 5_000;
const defaultMaxInflightProbes = 2;
const maximumAllowedInflightProbes = 8;

function parseMaxInflightProbes() {
  const raw = process.env.ATHENA_MCP_MAX_INFLIGHT;
  if (raw === undefined) return defaultMaxInflightProbes;
  if (!/^[0-9]+$/.test(raw)) throw new Error('invalid ATHENA_MCP_MAX_INFLIGHT');
  const parsed = Number(raw);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > maximumAllowedInflightProbes) {
    throw new Error('ATHENA_MCP_MAX_INFLIGHT out of range 1..=8');
  }
  return parsed;
}

let maxInflightProbes = defaultMaxInflightProbes;
let activeProbes = 0;

function tryAcquireProbePermit() {
  if (activeProbes >= maxInflightProbes) return null;
  activeProbes += 1;
  let released = false;
  return () => {
    if (!released) {
      released = true;
      activeProbes -= 1;
    }
  };
}

function expectedToken() {
  const token = process.env.ATHENA_L03_AUTH_TOKEN;
  if (!token) throw new Error('missing supervisor-scoped ATHENA_L03_AUTH_TOKEN');
  return Buffer.from(`Bearer ${token}`);
}

function sameSecret(actual, expected) {
  const bytes = Buffer.from(actual ?? '');
  return bytes.length === expected.length && timingSafeEqual(bytes, expected);
}

function coreProbeEnvironment() {
  const env = { RUST_LOG: 'off' };
  for (const key of ['PATH', 'Path', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL']) {
    if (process.env[key] !== undefined) env[key] = process.env[key];
  }
  return env;
}

function runCoreProbeInput(stage, fixture, signal) {
  const executable = process.env.ATHENA_CORE_PROBE_BIN;
  if (typeof executable !== 'string' || executable.length === 0 || !fixture) {
    return Promise.reject(new Error('CORE_PROBE_CONFIGURATION_INVALID'));
  }
  const input = Buffer.from(JSON.stringify(fixture));
  if (input.length > maximumCoreInputBytes) {
    return Promise.reject(new Error('CORE_FIXTURE_TOO_LARGE'));
  }
  return new Promise((resolve, reject) => {
    const child = spawn(executable, [stage, '-', '0'], {
      env: coreProbeEnvironment(),
      stdio: ['pipe', 'pipe', 'ignore'],
      windowsHide: true,
    });
    const output = [];
    let outputBytes = 0;
    let failure;
    let settled = false;
    let timer;
    const onAbort = () => stop('CORE_PROBE_CANCELLED');
    const finish = (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
      if (failure) {
        reject(failure);
        return;
      }
      if (code !== 0) {
        reject(new Error('CORE_PROBE_FAILED'));
        return;
      }
      let text;
      try {
        text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(Buffer.concat(output, outputBytes));
      } catch {
        reject(new Error('CORE_PROBE_OUTPUT_INVALID_UTF8'));
        return;
      }
      if (!text.endsWith('\n')) {
        reject(new Error('CORE_PROBE_OUTPUT_INVALID'));
        return;
      }
      resolve(text);
    };
    const stop = (message) => {
      if (!failure) failure = new Error(message);
      child.stdin.destroy();
      child.stdout.destroy();
      if (child.pid !== undefined && child.exitCode === null && child.signalCode === null) {
        child.kill('SIGKILL');
        return;
      }
      finish(child.exitCode);
    };
    timer = setTimeout(() => stop('CORE_PROBE_TIMEOUT'), coreTimeoutMs);
    child.once('error', () => stop('CORE_PROBE_START_FAILED'));
    child.once('exit', (code, signalCode) => {
      if (failure || code !== 0 || signalCode !== null) {
        stop('CORE_PROBE_FAILED');
      }
    });
    child.stdout.once('error', () => stop('CORE_PROBE_OUTPUT_FAILED'));
    child.stdout.on('data', (chunk) => {
      if (failure) return;
      if (chunk.length > maximumCoreOutputBytes - outputBytes) {
        stop('CORE_PROBE_OUTPUT_TOO_LARGE');
        return;
      }
      outputBytes += chunk.length;
      output.push(chunk);
    });
    child.once('close', finish);
    child.stdin.once('error', () => stop('CORE_PROBE_INPUT_FAILED'));
    signal?.addEventListener('abort', onAbort, { once: true });
    if (signal?.aborted) onAbort();
    if (!failure) child.stdin.end(input);
  });
}

function buildServer({ requestInfo } = {}) {
  const requestSignal = requestInfo?.signal;
  const server = new McpServer(
    { name: 'athena-mcp-fixture', version: '0.1.0' },
    { capabilities: { tools: {} } },
  );
  server.registerTool(
    contract.tool_name,
    {
      description: 'Run a bounded synthetic document through Athena Rust core and return AAE output.',
      inputSchema: z.object({ fixture_case: z.enum(['long', 'short']) }),
    },
    async ({ fixture_case: fixtureCase }, context) => {
      const releasePermit = tryAcquireProbePermit();
      if (releasePermit === null) {
        return { isError: true, content: [{ type: 'text', text: 'CORE_PROBE_ADMISSION_SATURATED' }] };
      }
      try {
        const progressToken = context.mcpReq._meta?.progressToken;
        if (progressToken !== undefined) {
          await context.mcpReq.notify({
            method: 'notifications/progress',
            params: { progressToken, progress: 0, message: 'Core operation started' },
          });
        }
        return { content: [{ type: 'text', text: await runCoreProbeInput('export', coreFixtures[fixtureCase]?.input, requestSignal) }] };
      } catch (error) {
        return { isError: true, content: [{ type: 'text', text: error.message }] };
      } finally {
        releasePermit();
      }
    },
  );
  server.registerTool(
    'athena_extract_document',
    {
      description: 'Extract a frozen research task document with supervisor-bound tenant, task and revision checks. No live network access.',
      inputSchema: z.object({ task_id: z.string(), source_id: z.string(), revision: z.string() }).strict(),
    },
    async ({ task_id: taskId, source_id: sourceId, revision }, context) => {
      const releasePermit = tryAcquireProbePermit();
      if (releasePermit === null) {
        return { isError: true, content: [{ type: 'text', text: 'CORE_PROBE_ADMISSION_SATURATED' }] };
      }
      try {
        const tenant = process.env.ATHENA_L03_TENANT;
        const boundTask = process.env.ATHENA_L03_BOUND_TASK;
        if (!tenant || !boundTask) throw new Error('SCOPED_TASK_CONTEXT_MISSING');
        const progressToken = context.mcpReq._meta?.progressToken;
        if (progressToken !== undefined) {
          await context.mcpReq.notify({
            method: 'notifications/progress',
            params: { progressToken, progress: 0, message: 'Core operation started' },
          });
        }
        const text = await runCoreProbeInput('task-export', {
          schema: 'athena-task-extract/1', principal_tenant: tenant, bound_task_id: boundTask,
          task_id: taskId, source_id: sourceId, expected_revision: revision,
        }, requestSignal);
        let denial;
        try { denial = JSON.parse(text); } catch { /* AAE is a multi-frame JSONL stream. */ }
        if (denial?.schema === 'athena-task-denial/1') throw new Error('SCOPED_TASK_DENIED');
        return { content: [{ type: 'text', text }] };
      } catch (error) {
        return { isError: true, content: [{ type: 'text', text: error.message }] };
      } finally {
        releasePermit();
      }
    },
  );
  server.registerTool(
    'athena_expand_handle',
    {
      description: 'Expand a compact evidence handle through Athena Rust core with supervisor-bound tenant, task, revision and handle checks.',
      inputSchema: z.object({ task_id: z.string(), source_id: z.string(), revision: z.string(), handle: z.string() }).strict(),
    },
    async ({ task_id: taskId, source_id: sourceId, revision, handle }, context) => {
      const releasePermit = tryAcquireProbePermit();
      if (releasePermit === null) {
        return { isError: true, content: [{ type: 'text', text: 'CORE_PROBE_ADMISSION_SATURATED' }] };
      }
      try {
        const tenant = process.env.ATHENA_L03_TENANT;
        const boundTask = process.env.ATHENA_L03_BOUND_TASK;
        if (!tenant || !boundTask) throw new Error('SCOPED_TASK_CONTEXT_MISSING');
        const progressToken = context.mcpReq._meta?.progressToken;
        if (progressToken !== undefined) {
          await context.mcpReq.notify({
            method: 'notifications/progress',
            params: { progressToken, progress: 0, message: 'Core operation started' },
          });
        }
        const text = await runCoreProbeInput('task-expand', {
          schema: 'athena-task-expand/1', principal_tenant: tenant, bound_task_id: boundTask,
          task_id: taskId, source_id: sourceId, expected_revision: revision, handle,
        }, requestSignal);
        let denial;
        try { denial = JSON.parse(text); } catch { /* AAE is a multi-frame JSONL stream. */ }
        if (denial?.schema === 'athena-task-denial/1') throw new Error('SCOPED_TASK_DENIED');
        return { content: [{ type: 'text', text }] };
      } catch (error) {
        return { isError: true, content: [{ type: 'text', text: error.message }] };
      } finally {
        releasePermit();
      }
    },
  );
  server.registerTool(
    'athena_search_task',
    {
      description: 'Search frozen research task candidates through Athena Rust core with supervisor-bound tenant, task and query checks.',
      inputSchema: z.object({ task_id: z.string(), query: z.string() }).strict(),
    },
    async ({ task_id: taskId, query }, context) => {
      const releasePermit = tryAcquireProbePermit();
      if (releasePermit === null) {
        return { isError: true, content: [{ type: 'text', text: 'CORE_PROBE_ADMISSION_SATURATED' }] };
      }
      try {
        const tenant = process.env.ATHENA_L03_TENANT;
        const boundTask = process.env.ATHENA_L03_BOUND_TASK;
        if (!tenant || !boundTask) throw new Error('SCOPED_TASK_CONTEXT_MISSING');
        const progressToken = context.mcpReq._meta?.progressToken;
        if (progressToken !== undefined) {
          await context.mcpReq.notify({
            method: 'notifications/progress',
            params: { progressToken, progress: 0, message: 'Core operation started' },
          });
        }
        const text = await runCoreProbeInput('task-search', {
          schema: 'athena-task-search/1', principal_tenant: tenant, bound_task_id: boundTask,
          task_id: taskId, query,
        }, requestSignal);
        let denial;
        try { denial = JSON.parse(text); } catch { /* Ignore non-JSON or multi-frame output. */ }
        if (denial?.schema === 'athena-task-denial/1') throw new Error('SCOPED_TASK_DENIED');
        return { content: [{ type: 'text', text }] };
      } catch (error) {
        return { isError: true, content: [{ type: 'text', text: error.message }] };
      } finally {
        releasePermit();
      }
    },
  );
  return server;
}

function writeError(response, status, message) {
  response.writeHead(status, { 'content-type': 'text/plain; charset=utf-8', connection: 'close' });
  response.end(message);
}

async function readBody(request) {
  const chunks = [];
  let total = 0;
  for await (const chunk of request) {
    total += chunk.length;
    if (total > maximumBodyBytes) throw new RangeError('REQUEST_BODY_TOO_LARGE');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks, total);
}

async function runHttp(port) {
  const expected = expectedToken();
  const origin = `http://127.0.0.1:${port}`;
  const handler = createMcpHandler(buildServer, { legacy: 'reject', keepAliveMs: 0 });
  const server = createServer(async (incoming, outgoing) => {
    if (incoming.method !== 'POST' || incoming.url !== '/mcp') {
      writeError(outgoing, 404, 'NOT_FOUND');
      return;
    }
    if (incoming.headers.host !== `127.0.0.1:${port}`) {
      writeError(outgoing, 403, 'INVALID_HOST');
      return;
    }
    if (incoming.headers.origin && incoming.headers.origin !== origin) {
      writeError(outgoing, 403, 'INVALID_ORIGIN');
      return;
    }
    if (!sameSecret(incoming.headers.authorization, expected)) {
      writeError(outgoing, 401, 'UNAUTHORIZED');
      return;
    }
    let body;
    try {
      body = await readBody(incoming);
    } catch (error) {
      writeError(outgoing, error instanceof RangeError ? 413 : 400,
        error instanceof RangeError ? 'REQUEST_BODY_TOO_LARGE' : 'INVALID_BODY');
      return;
    }
    const requestCancellation = new AbortController();
    const abortOnDisconnect = () => {
      if (!outgoing.writableEnded && !requestCancellation.signal.aborted) {
        requestCancellation.abort(new Error('HTTP_CLIENT_DISCONNECTED'));
      }
    };
    outgoing.once('close', abortOnDisconnect);
    if (outgoing.destroyed) abortOnDisconnect();
    try {
      const headers = new Headers();
      for (const [name, value] of Object.entries(incoming.headers)) {
        if (typeof value === 'string') headers.set(name, value);
      }
      const request = new Request(new URL(incoming.url, origin), {
        method: 'POST', headers, body, duplex: 'half', signal: requestCancellation.signal,
      });
      const response = await handler.fetch(request, {
        authInfo: { token: 'redacted-research-token', clientId: 'l03-loopback-driver', scopes: ['mcp'] },
      });
      const responseHeaders = {};
      response.headers.forEach((value, name) => { responseHeaders[name] = value; });
      outgoing.writeHead(response.status, responseHeaders);
      if (response.body === null) outgoing.end();
      else await pipeline(response.body, outgoing);
    } catch {
      if (!outgoing.destroyed && !outgoing.writableEnded) {
        writeError(outgoing, 500, 'MCP_HANDLER_FAILURE');
      }
    } finally {
      outgoing.off('close', abortOnDisconnect);
    }
  });
  server.headersTimeout = 5_000;
  server.requestTimeout = 10_000;
  server.setTimeout(coreTimeoutMs + 2_000);
  server.listen(port, '127.0.0.1', () => console.error(`MCP_READY ${port}`));
  await new Promise((resolve) => server.once('close', resolve));
}

const [mode, portText] = process.argv.slice(2);
try {
  maxInflightProbes = parseMaxInflightProbes();
  if (mode === 'stdio') {
    expectedToken();
    serveStdio(buildServer, { legacy: 'reject' });
  } else if (mode === 'http' && portText && process.argv.length === 4) {
    const port = Number(portText);
    if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('invalid loopback port');
    await runHttp(port);
  } else {
    throw new Error('usage: node server.mjs stdio | http <port>');
  }
} catch (error) {
  console.error(error instanceof Error ? error.message : 'MCP_SERVER_FAILURE');
  process.exitCode = 2;
}
