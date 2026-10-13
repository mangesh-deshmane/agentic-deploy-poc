'use strict';

const express = require('express');
const path = require('path');
const { Firestore } = require('@google-cloud/firestore');

// ---------------------------------------------------------------------------
// Configuration. Every value comes from the environment, which is what
// Terraform populates - this is how "deploy app1 for acme on gcp" becomes a
// running service that knows it belongs to acme.
// ---------------------------------------------------------------------------
const PORT = parseInt(process.env.PORT || '8080', 10);
const CUSTOMER = process.env.CUSTOMER || 'unknown';
const ENVIRONMENT = process.env.ENVIRONMENT || 'unknown';
const APP_NAME = process.env.APP_NAME || 'app1';
const PROJECT = process.env.GCP_PROJECT || '';
const REVISION = process.env.K_REVISION || 'local';
const STARTED_AT = new Date().toISOString();

// Firestore is lazy: the service must answer /health even if the database is
// unreachable, otherwise a transient data problem looks like a failed deploy.
let db = null;
let dbError = null;
try {
  db = new Firestore(PROJECT ? { projectId: PROJECT } : {});
} catch (err) {
  dbError = err.message;
}

// Each environment gets its own collection, so two demos never see each
// other's data even though they share one Firestore database.
const COLLECTION = `${APP_NAME}-${CUSTOMER}-${ENVIRONMENT}-items`;

const app = express();
app.use(express.json({ limit: '64kb' }));
app.use(express.static(path.join(__dirname, 'public')));

// Structured single-line logs. Cloud Logging parses JSON on stdout into
// queryable fields, so this costs nothing extra and is far easier to filter.
app.use((req, res, next) => {
  const start = Date.now();
  res.on('finish', () => {
    process.stdout.write(JSON.stringify({
      severity: res.statusCode >= 500 ? 'ERROR' : 'INFO',
      message: `${req.method} ${req.path} ${res.statusCode}`,
      method: req.method,
      path: req.path,
      status: res.statusCode,
      durationMs: Date.now() - start,
      customer: CUSTOMER,
      environment: ENVIRONMENT,
      revision: REVISION
    }) + '\n');
  });
  next();
});

// ---------------------------------------------------------------------------
// Health. Deliberately does NOT touch Firestore - a readiness probe that
// depends on a database restarts healthy containers during a database blip.
// ---------------------------------------------------------------------------
app.get('/health', (_req, res) => {
  res.json({
    status: 'ok',
    app: APP_NAME,
    revision: REVISION,
    uptimeSeconds: Math.floor(process.uptime())
  });
});

// ---------------------------------------------------------------------------
// Identity. The validation step asserts customer and environment match what
// was requested - this is what proves the agent parameterised the deployment
// rather than shipping a fixed artifact.
// ---------------------------------------------------------------------------
app.get('/api/info', (_req, res) => {
  res.json({
    app: APP_NAME,
    customer: CUSTOMER,
    environment: ENVIRONMENT,
    cloud: 'gcp',
    platform: 'cloud-run',
    runtime: `node ${process.versions.node}`,
    dataStore: 'firestore',
    region: process.env.GCP_REGION || 'us-central1',
    revision: REVISION,
    startedAt: STARTED_AT,
    deployedBy: 'agentic-deploy-poc'
  });
});

// ---------------------------------------------------------------------------
// Items: a small REST resource backed by Firestore. Enough to prove the
// service has real state and a real IAM path to its data store.
// ---------------------------------------------------------------------------
function requireDb(res) {
  if (!db) {
    res.status(503).json({ error: 'data store unavailable', detail: dbError });
    return false;
  }
  return true;
}

app.get('/api/items', async (_req, res) => {
  if (!requireDb(res)) return;
  try {
    const snap = await db.collection(COLLECTION).orderBy('createdAt', 'desc').limit(50).get();
    res.json({
      collection: COLLECTION,
      count: snap.size,
      items: snap.docs.map((d) => ({ id: d.id, ...d.data() }))
    });
  } catch (err) {
    res.status(500).json({ error: 'query failed', detail: err.message });
  }
});

app.post('/api/items', async (req, res) => {
  if (!requireDb(res)) return;

  const name = typeof req.body?.name === 'string' ? req.body.name.trim() : '';
  if (!name || name.length > 120) {
    return res.status(400).json({ error: 'name is required and must be 1-120 characters' });
  }
  const note = typeof req.body?.note === 'string' ? req.body.note.slice(0, 500) : '';

  try {
    const doc = {
      name,
      note,
      customer: CUSTOMER,
      environment: ENVIRONMENT,
      createdAt: new Date().toISOString()
    };
    const ref = await db.collection(COLLECTION).add(doc);
    res.status(201).json({ id: ref.id, ...doc });
  } catch (err) {
    res.status(500).json({ error: 'write failed', detail: err.message });
  }
});

app.delete('/api/items/:id', async (req, res) => {
  if (!requireDb(res)) return;
  try {
    await db.collection(COLLECTION).doc(req.params.id).delete();
    res.status(204).end();
  } catch (err) {
    res.status(500).json({ error: 'delete failed', detail: err.message });
  }
});

app.use((_req, res) => res.status(404).json({ error: 'not found' }));

// ---------------------------------------------------------------------------
// 0.0.0.0 is required: binding to localhost is the single most common reason
// a container that works locally never serves traffic on Cloud Run.
// ---------------------------------------------------------------------------
const server = app.listen(PORT, '0.0.0.0', () => {
  process.stdout.write(JSON.stringify({
    severity: 'INFO',
    message: `${APP_NAME} listening on ${PORT}`,
    customer: CUSTOMER,
    environment: ENVIRONMENT,
    collection: COLLECTION
  }) + '\n');
});

// Cloud Run sends SIGTERM before stopping an instance. Draining in-flight
// requests avoids 503s for whoever is mid-request during a scale-down.
process.on('SIGTERM', () => {
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 10000).unref();
});
