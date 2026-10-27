// Cube: REST and GraphQL APIs over the gold and share tables, generated from the YAML model in ./model.
//
// Governance stays in Trino. Every request carries a signed token whose `trino_user` claim names the caller,
// and Cube runs the SQL in Trino *as that user*, so the row filters and column masks in
// infra/trino/etc/security/rules.json apply to these APIs exactly as to dashboards, SQL and the AI agent.
// Cube itself only checks that the identity is one it may serve.

const jwt = require('jsonwebtoken');
const { CubejsHandlerError } = require('@cubejs-backend/api-gateway');

const ALLOWED = (process.env.CUBE_TRINO_USERS || '').split(',').map((u) => u.trim()).filter(Boolean);

function forbidden(message) {
  return new CubejsHandlerError(403, 'Forbidden', message);
}

function trinoUser(securityContext) {
  const user = securityContext && securityContext.trino_user;
  if (!user || !ALLOWED.includes(user)) {
    throw forbidden(`identity not allowed: ${user || '(none)'}`);
  }
  return user;
}

module.exports = {
  // Always verify the token, also in dev mode (which otherwise accepts requests without one). In production,
  // verify tokens from your identity provider instead (CUBEJS_JWK_URL), not a shared secret.
  checkAuth: (req, auth) => {
    if (!auth) {
      throw forbidden('missing token');
    }
    let payload;
    try {
      payload = jwt.verify(auth.replace(/^Bearer /i, ''), process.env.CUBEJS_API_SECRET, { algorithms: ['HS256'] });
    } catch (e) {
      throw forbidden(`invalid token: ${e.message}`);
    }
    trinoUser(payload);
    req.securityContext = payload;
  },

  // one Trino connection per identity
  driverFactory: ({ securityContext }) => ({ type: 'trino', user: trinoUser(securityContext) }),

  // separate compiled models, caches and query queues per identity: a cached result for one caller must
  // never be served to another
  contextToAppId: ({ securityContext }) => `lakehouse_${trinoUser(securityContext)}`,
  contextToOrchestratorId: ({ securityContext }) => `lakehouse_${trinoUser(securityContext)}`,

  // reject unknown identities before any query is planned
  queryRewrite: (query, { securityContext }) => {
    trinoUser(securityContext);
    return query;
  },
};
